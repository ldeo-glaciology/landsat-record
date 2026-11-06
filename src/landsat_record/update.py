"""Bring a published site up to date with the USGS catalogue, working from the published site alone.

A full run keeps its state on CryoCloud scratch, which deletes files a week after they're written, and which only the
hub can reach. An update needs none of it: the published index.json holds the outline, and the published tables say
which scenes are done. New scenes, and any that failed before, are cropped, converted and rendered in a temporary
folder, merged into the tables and index, and published. That takes minutes, and runs anywhere with AWS credentials.

The result is what a full run would publish: scenes USGS has withdrawn (usually to reprocess them, after which they
come back under new ids) are dropped from the index and tables too. Their files stay in the bucket.
"""
import io
import json
import tempfile
from typing import NamedTuple

import pandas as pd
from fsspec.implementations.local import LocalFileSystem

from . import publish as publish_step
from .browse import viewer_html
from .keys import require_key
from .progress import say
from .record import LandsatRecord
from .search import summarise
from .site import Site, SiteError

# The fields of an index.json entry, as browse.build_viewer writes them. A site published with other fields was made
# by a version whose entries can't be mixed with this one's.
ENTRY_FIELDS = {"id", "datetime", "platform", "path", "row", "level", "cloud", "sun", "images", "composite", "default"}

# USGS withdraws a few scenes at a time (in November 2026, 8 of Flask Glacier's 1,191: Landsat 9 scenes from late
# 2023). Many more than this going missing points at a search that went wrong, so nothing is changed.
MAX_WITHDRAWN = 0.1


class Changes(NamedTuple):
    """Scene ids: new in the catalogue, published with errors (and retried), and withdrawn from the catalogue."""

    new: list
    failed: list
    withdrawn: list


def read_published(dest_fs, dest_root, name):
    """The published site's outline, index and tables. Returns (site, index, scenes table, bands table)."""
    try:
        index = json.loads(dest_fs.cat(f"{dest_root}/index.json"))
    except FileNotFoundError:
        raise SiteError(
            f"nothing is published at {dest_root}. Build the site first with `landsat-record run {name} ... --publish`"
        ) from None
    old = [entry["id"] for entry in index["scenes"] if set(entry) != ENTRY_FIELDS]
    if old:
        raise SiteError(
            f"{dest_root} was published by an older version, whose index entries can't be mixed with this one's "
            f"(e.g. {old[0]}). Rebuild it with `landsat-record run {name} --publish`"
        )
    tables = [pd.read_parquet(io.BytesIO(dest_fs.cat(f"{dest_root}/{table}"))) for table in ("scenes.parquet", "bands.parquet")]
    title = index["title"].removesuffix(" Landsat record")
    site = Site(name, title, index["aoi_vertices"], f"published, {dest_root}/index.json")
    return site, index, *tables


def compare(items, scenes):
    """The catalogue scenes that aren't published yet, the published ones that failed, and the ids of published
    scenes that are no longer in the catalogue."""
    published = dict(zip(scenes.scene_id, scenes.scene_status))
    new = [item for item in items if item.id not in published]
    failed = [item for item in items if published.get(item.id) == "error"]
    withdrawn = sorted(set(published) - {item.id for item in items})
    if len(withdrawn) > max(25, MAX_WITHDRAWN * len(published)):
        raise SiteError(
            f"{len(withdrawn)} of the {len(published)} published scenes aren't in the USGS catalogue any more, which "
            "looks like a problem with the search rather than scenes USGS has withdrawn. Nothing was changed: try again later."
        )
    return new, failed, withdrawn


def cog_folder(bands, fallback):
    """The COG folder the published bands table's keys are in, so new rows can follow the same convention."""
    for row in bands[bands.status == "ok"].dropna(subset=["key"]).itertuples():
        marker = f"/{row.scene_id}/"
        if marker in row.key:
            return row.key[: row.key.index(marker)]
    return fallback


def process(record, cog_prefix, probe):
    """Crop, convert and render record.run_items, after checking access by reading the probe scene. Returns their
    index entries and their scenes and bands rows, with keys in the published site's folders."""
    record.check_access(probe)
    record.crop()
    record.cogs()
    entries = record.browse()["scenes"]
    scenes = record.scenes()
    scenes = scenes[scenes.scene_id.isin({item.id for item in record.run_items})]
    bands = record.bands(cog=True)

    # Keys of converted files are in the COG folder; the rest keep the crop folder's, as in a full run
    moves = [(f"{record.cog_root}/", f"{cog_prefix}/"), (f"{record.out_root}/", f"{cog_prefix.removesuffix('_cog')}/")]

    def rehome(key):
        for old, new in moves:
            if key.startswith(old):
                return new + key[len(old):]
        return key

    bands["key"] = bands.key.map(rehome, na_action="ignore")
    return entries, scenes, bands


def write_site(record, index, scenes, bands, replaced, entries, fresh_scenes, fresh_bands):
    """Write everything a publish always copies to the record's COG folder: the published index and tables with the
    replaced scenes' rows and entries swapped for the fresh ones, the catalogue, and the viewer page. Returns the
    scenes table."""
    fs = record.fs
    keep = lambda table: table[~table.scene_id.isin(replaced)]
    merged_scenes = pd.concat([keep(scenes), fresh_scenes]).sort_values("datetime", kind="stable").reset_index(drop=True)
    merged_bands = pd.concat([keep(bands), fresh_bands]).reset_index(drop=True)
    merged_entries = [entry for entry in index["scenes"] if entry["id"] not in replaced] + entries
    merged_index = {**index, "scenes": sorted(merged_entries, key=lambda entry: entry["datetime"])}  # keeps the published order

    fs.makedirs(record.cog_root, exist_ok=True)
    for name, table in (("scenes.parquet", merged_scenes), ("bands.parquet", merged_bands)):
        with fs.open(f"{record.cog_root}/{name}", "wb") as f:
            table.to_parquet(f)
    with fs.open(f"{record.cog_root}/index.json", "w") as f:
        json.dump(merged_index, f, separators=(",", ":"), allow_nan=False)
    record.item_collection.save_object(f"{record.cog_root}/items.json")
    fs.pipe_file(f"{record.cog_root}/index.html", viewer_html())
    return merged_scenes


def update_site(name, test=False, publish_to=publish_step.DEFAULT_PUBLISH_TO, key_file=None, dry_run=False, workers=16, dest_fs=None):
    """Add the scenes released since a site was published, retry any that failed, and drop any USGS has withdrawn.
    Returns the Changes, by scene id.

    Works from the published site, so it needs no scratch: just AWS credentials to read the USGS bucket (as CryoCloud
    has), and the key to publish with (found as described in landsat_record.keys.find_key). A dry run lists the
    changes and writes nothing; it needs no key, because the published site is public.
    """
    dest_root = publish_step.site_root(publish_to, name, test)
    if dest_fs is None:
        dest_fs = publish_step.gcs_filesystem(None if dry_run else require_key(key_file))

    say("[1/4] Read the published site")
    site, index, scenes, bands = read_published(dest_fs, dest_root, name)
    say(f"{site.describe()}: {len(index['scenes'])} scenes, the newest from {scenes.datetime.max():%Y-%m-%d}")

    with tempfile.TemporaryDirectory(prefix="landsat-record-") as work:
        record = LandsatRecord(site, scratch=work, workers=workers, fs=LocalFileSystem(auto_mkdir=True))
        say("\n[2/4] Search the USGS catalogue")
        record.search()
        new, failed, withdrawn = compare(record.items, scenes)
        changes = Changes([item.id for item in new], [item.id for item in failed], withdrawn)
        say(f"New since publishing: {summarise(new) if new else 'none'}")
        if failed:
            say(f"Published with errors, to retry: {len(failed)}")
        if withdrawn:
            say(f"Withdrawn by USGS since publishing, to drop from the viewer (their files stay in the bucket): {len(withdrawn)}")
        if not any(changes):
            say("Nothing to do: the site is up to date.")
            return changes
        if dry_run:
            say("Dry run: nothing was written.")
            return changes

        record.run_items = new + failed
        say(f"\n[3/4] Crop, convert and render the new and failed scenes: {len(record.run_items)}")
        if record.run_items:
            # Not a scene that failed before: some can never be read (USGS denies access to every file of two Flask
            # scenes), which would look like missing credentials
            done_ok = set(scenes.scene_id[scenes.scene_status == "ok"])
            probe = (new or [item for item in record.items if item.id in done_ok] or record.run_items)[-1]
            fresh = process(record, cog_folder(bands, dest_root), probe)
        else:
            fresh = [], scenes.iloc[:0], bands.iloc[:0]
        replaced = {item.id for item in record.run_items} | set(withdrawn)
        merged = write_site(record, index, scenes, bands, replaced, *fresh)

        say("\n[4/4] Publish")
        publish_step.publish(record, dest_fs, dest_root, 2 * workers)
        done = merged[merged.scene_id.isin(changes.new + changes.failed)]
        say(f"Processed: {len(done)} {done.scene_status.value_counts().to_dict()}. Dropped as withdrawn: {len(withdrawn)}")
    return changes
