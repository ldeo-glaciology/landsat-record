"""One site's Landsat record: where it's kept on scratch, and the steps that build it."""
import json
import os
import tempfile

import pandas as pd

from . import browse as browse_step
from . import cogs as cogs_step
from . import crop as crop_step
from . import publish as publish_step
from .progress import say
from .search import search_catalogue, select_test_items, summarise
from .site import Site, SiteError

STAGES = ["search", "crop", "cogs", "browse", "publish"]


def default_scratch():
    user = os.environ.get("JUPYTERHUB_USER")
    if not user:
        raise RuntimeError(
            "JUPYTERHUB_USER isn't set, so this doesn't look like CryoCloud. "
            "Give an S3 folder to write to with --scratch (scratch= in Python)."
        )
    return f"s3://nasa-cryo-scratch/{user}/landsat"


def scratch_filesystem(scratch):
    """S3 for an s3:// scratch folder, which is what CryoCloud gives you, otherwise the local disk."""
    if scratch.startswith("s3://"):
        import s3fs

        return s3fs.S3FileSystem()
    from fsspec.implementations.local import LocalFileSystem

    return LocalFileSystem(auto_mkdir=True)


def stored_site(name, scratch, fs):
    """The site as recorded by earlier runs on scratch, or None.

    Looks for site.json in the full and test folders, and for the index.json that runs made with the notebook left
    in their _cog folders. The first found is returned, but they must all have the same outline.
    """
    fs.invalidate_cache()
    found = {}
    for folder in (name, f"{name}_test"):
        path = f"{scratch}/{folder}/site.json"
        if fs.exists(path):
            found[path] = Site.from_json(json.loads(fs.cat(path)))
    for folder in (f"{name}_cog", f"{name}_test_cog"):
        path = f"{scratch}/{folder}/index.json"
        if fs.exists(path):
            index = json.loads(fs.cat(path))
            title = index["title"].removesuffix(" Landsat record")
            found[path] = Site(name, title, index["aoi_vertices"], f"earlier run, {folder}/index.json")
    if not found:
        return None
    first, *others = found.values()
    if any(not site.same_outline(first) for site in others):
        listing = "\n".join(f"  {path}: bounds {[round(v) for v in site.bounds]}" for path, site in found.items())
        raise SiteError(
            f"earlier runs of {name} used different outlines:\n{listing}\n"
            "Delete the folders made with the wrong one (the _test folders are quick to rebuild)."
        )
    return first


def outline_clash(name, stored, advice):
    return SiteError(
        f"{name} already exists on scratch with a different outline ({stored.source}, bounds "
        f"{[round(v) for v in stored.bounds]}). {advice}"
    )


def resolve_site(name, given=None, stored=None, title=None):
    """The site to run: the outline given now or the stored one, with a check that they agree. The title is the one
    given now, else the stored one, else one made from the name.

    A different outline for an existing site is refused, because the crops already made would no longer match it.
    """
    if given and stored and not given.same_outline(stored):
        raise outline_clash(name, stored, "Use a new site name, or leave the outline out to reuse it.")
    site = given or stored
    if site is None:
        raise SiteError(f"{name} hasn't been run before, so it needs an outline: --bbox, --bbox-3031 or --geojson")
    title = title or (stored.title if stored else site.title)
    return Site(site.name, title, site.vertices, site.source)


class LandsatRecord:
    """Every Landsat Collection 2 Level 1 scene over a site, cropped at native resolution, with a viewer to browse it.

    Each step is resumable: re-running it skips finished files and retries failed ones. The steps, in order:

        record.search()    every scene in the USGS STAC catalogue over the site
        record.crop()      crop each band to the site, as GeoTIFFs on scratch, with tables of what was done
        record.cogs()      convert them to Cloud Optimized GeoTIFFs in a separate _cog folder
        record.browse()    browse images, index.json and the viewer page, in the _cog folder
        record.publish()   copy the _cog folder to a public bucket

    With test=True only a sample of about a dozen scenes covering every sensor is processed, into separate _test
    folders.

    A site name keeps the outline it was first run with, for the test and the full record: a different outline under
    the same name is refused, because finished crops are never redone and would still be for the old one.
    """

    def __init__(self, site, test=False, scratch=None, workers=16, fs=None):
        self.site = site
        self.test = test
        self.scratch = (scratch or default_scratch()).rstrip("/")
        self.workers = workers
        self.fs = fs or scratch_filesystem(self.scratch)
        stored = stored_site(site.name, self.scratch, self.fs)
        if stored and not site.same_outline(stored):
            raise outline_clash(site.name, stored, "Give this outline a new site name.")
        self.item_collection = None
        self.items = None  # every catalogue scene, oldest first
        self.run_items = None  # the scenes this run processes

    def __repr__(self):
        return f"LandsatRecord({self.site.name!r}, test={self.test}, out_root={self.out_root!r})"

    @property
    def out_root(self):
        return f"{self.scratch}/{self.site.name}" + ("_test" if self.test else "")

    @property
    def cog_root(self):
        return f"{self.out_root}_cog"

    def publish_root(self, publish_to=publish_step.DEFAULT_PUBLISH_TO):
        return f"{publish_to.rstrip('/')}/{self.site.name}" + ("_test" if self.test else "")

    def search(self):
        """Every scene over the site, and the ones this run will process."""
        self.item_collection, self.items = search_catalogue(self.site.search_geometry())
        if not self.items:
            raise SiteError(f"no Landsat scenes cover {self.site.describe()}")
        self.run_items = select_test_items(self.items) if self.test else self.items
        if self.test and {i.id[:4] for i in self.run_items} != {i.id[:4] for i in self.items}:
            raise RuntimeError("the test sample should cover every sensor")
        say(f"Catalogue: {summarise(self.items)}")
        if self.test:
            say(f"Test sample: {len(self.run_items)} scenes")
        return self.run_items

    def check_access(self):
        """Read one window from the requester-pays bucket, so a run fails now rather than hours in."""
        if self.run_items is None:
            self.search()
        say(crop_step.check_access(self.run_items[-1], self.site.bounds))

    def save_site(self):
        with self.fs.open(f"{self.out_root}/site.json", "w") as f:
            json.dump(self.site.to_json(), f, indent=1)

    def crop(self):
        """Crop every band of every scene. Returns the bands table."""
        if self.run_items is None:
            self.search()
        self.save_site()
        with tempfile.TemporaryDirectory() as tmp:
            self.item_collection.save_object(f"{tmp}/items.json")
            self.fs.put_file(f"{tmp}/items.json", f"{self.out_root}/items.json")
        bands, _ = crop_step.run_all(self, self.run_items)
        return bands

    def read_table(self, root, name):
        with self.fs.open(f"{root}/{name}", "rb") as f:
            return pd.read_parquet(f)

    def scenes(self):
        """One row per catalogue scene, with scene_status ok, no_data, error or not_run."""
        return self.read_table(self.out_root, "scenes.parquet")

    def bands(self, cog=False):
        """One row per band of each processed scene. With cog=True, keys point at the COGs rather than the GeoTIFFs."""
        return self.read_table(self.cog_root if cog else self.out_root, "bands.parquet")

    def cogs(self):
        """Convert every cropped GeoTIFF to a COG. Returns (bands table pointing at the COGs, failures)."""
        self.fs.invalidate_cache()
        scenes, bands = self.scenes(), self.bands()
        processed = scenes[scenes.scene_status != "not_run"]
        say(f"{len(processed)} of {len(scenes)} catalogue scenes processed: {processed.scene_status.value_counts().to_dict()}")
        if (bands.status == "error").any():
            say("WARNING: some bands failed during the crop and won't be included:")
            say(bands[bands.status == "error"][["scene_id", "band", "reason"]].to_string())

        cog_bands, failures = cogs_step.convert_all(self, bands)
        was_raster = (bands.status == "ok") & (bands.band != crop_step.MTL_ASSET)
        say(f"GeoTIFFs {bands[was_raster].bytes.sum() / 1e9:.2f} GB -> COGs {cog_bands[was_raster].bytes.sum() / 1e9:.2f} GB")
        if failures:
            say("FAILED (re-run to retry):\n  " + "\n  ".join(f"{key} {error}" for key, error in failures.items()))
        return cog_bands, failures

    def browse(self):
        """Write the browse images, index.json and the viewer page. Returns the index."""
        self.fs.invalidate_cache()
        scenes = self.scenes()
        processed = scenes[scenes.scene_status != "not_run"]
        return browse_step.build_viewer(self, processed, self.bands(cog=True))

    def publish(self, publish_to=publish_step.DEFAULT_PUBLISH_TO, key_file=None, dest_fs=None, workers=None):
        """Copy the COG folder to <publish_to>/<site>, skipping files already there. Returns the viewer's address.

        The key is found as described in landsat_record.keys.find_key, unless dest_fs is given.
        """
        dest_root = self.publish_root(publish_to)
        if dest_fs is None:
            dest_fs = publish_step.gcs_filesystem(self._key(key_file))
        copied = publish_step.publish(self, dest_fs, dest_root, workers or 2 * self.workers)
        say(f"Copied {copied} files to {dest_root}")
        return publish_step.public_url(dest_root)

    def update_viewer(self, publish_to=publish_step.DEFAULT_PUBLISH_TO, key_file=None, dest_fs=None):
        """Replace only the published index.html with this package's viewer page."""
        dest_root = self.publish_root(publish_to)
        if dest_fs is None:
            dest_fs = publish_step.gcs_filesystem(self._key(key_file))
        publish_step.update_viewer(self, dest_fs, dest_root)
        return publish_step.public_url(dest_root)

    def _key(self, key_file):
        from .keys import KeyFileError, find_key

        key = find_key(key_file)
        if key is None:
            raise KeyFileError("no key to publish with. Run `landsat-record login KEYFILE` once, or pass --key-file")
        return key

    def project_full_run(self):
        """After a test run: the time, size and request count of the full run, scaled from the test."""
        return crop_step.project_full_run(self.bands(), self.scenes(), self.workers)

    def quicklook(self):
        """One panel per processed scene, using the finest band with data, with the site outline in red."""
        from .plots import quicklook

        return quicklook(self)
