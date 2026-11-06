import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fsspec.implementations.local import LocalFileSystem
from pystac import ItemCollection

import landsat_record.cli as cli
import landsat_record.crop as crop
import landsat_record.publish as publish
import landsat_record.record as record_module
from landsat_record import LandsatRecord, SiteError, update_site
from landsat_record.keys import store_key
from landsat_record.publish import ALWAYS_COPY
from landsat_record.update import Changes, compare

from .conftest import FakeGCS
from .test_keys import SECRET_FRAGMENT

PUBLISH_TO = "gs://test-bucket/landsat"
DEST = f"{PUBLISH_TO}/flask_tiny"


@pytest.fixture
def bucket(tmp_path):
    return FakeGCS(tmp_path / "gcs")


def catalogue(monkeypatch, items):
    """Make the USGS catalogue hold just these scenes."""
    monkeypatch.setattr(record_module, "search_catalogue", lambda geometry: (ItemCollection(items), list(items)))


def build(site, items, scratch, monkeypatch):
    """A full run over a catalogue of these scenes."""
    catalogue(monkeypatch, items)
    record = LandsatRecord(site, scratch=scratch, workers=4, fs=LocalFileSystem(auto_mkdir=True))
    record.search()
    record.crop()
    record.cogs()
    record.browse()
    return record


def published(bucket):
    return bucket.root / DEST.removeprefix("gs://")


def files_under(root):
    return {str(path.relative_to(root)): path for path in Path(root).rglob("*") if path.is_file()}


def without_folders(bands, record):
    """The bands table with keys relative to the record's COG or crop folder."""
    keys = bands.key.str.replace(f"{record.cog_root}/", "cog:", n=1, regex=False).str.replace(f"{record.out_root}/", "crop:", n=1, regex=False)
    return bands.drop(columns="seconds").assign(key=keys).sort_values(["scene_id", "band"]).reset_index(drop=True)


def assert_same_site(bucket, first, full):
    """The published site (first published from the record first) is the one the full record makes: the same index,
    tables and files, apart from the files of withdrawn scenes, which stay in the bucket. Returns the index entries."""
    site_files, full_files = files_under(published(bucket)), files_under(full.cog_root)
    assert full_files.keys() <= site_files.keys()
    for name, path in full_files.items():
        if not name.endswith((".parquet", "index.json")):
            assert site_files[name].read_bytes() == path.read_bytes(), name
    index, full_index = (json.loads(files["index.json"].read_text()) for files in (site_files, full_files))
    assert index.keys() == full_index.keys() and all(index[k] == full_index[k] for k in index if k != "scenes")
    entries = {entry["id"]: entry for entry in index["scenes"]}
    assert entries == {entry["id"]: entry for entry in full_index["scenes"]}
    assert [entry["datetime"] for entry in index["scenes"]] == sorted(entry["datetime"] for entry in index["scenes"])

    scenes, full_scenes = (pd.read_parquet(files["scenes.parquet"]) for files in (site_files, full_files))
    pd.testing.assert_frame_equal(
        scenes.sort_values("scene_id").reset_index(drop=True), full_scenes.sort_values("scene_id").reset_index(drop=True)
    )
    bands, full_bands = (pd.read_parquet(files["bands.parquet"]) for files in (site_files, full_files))
    pd.testing.assert_frame_equal(  # column order follows whichever scene finished first, in any run
        without_folders(bands, first), without_folders(full_bands, full), check_dtype=False, check_like=True
    )
    return entries


def test_update_adds_new_scenes_as_a_full_run_would(site, items, sources, tmp_path, bucket, monkeypatch):
    first = build(site, items[:-3], str(tmp_path / "first"), monkeypatch)
    first.publish(PUBLISH_TO, dest_fs=bucket)
    entries_before = {entry["id"]: entry for entry in json.loads((published(bucket) / "index.json").read_text())["scenes"]}

    catalogue(monkeypatch, items)
    bucket.content_types.clear()
    reads = sources.reads
    assert update_site("flask_tiny", publish_to=PUBLISH_TO, dest_fs=bucket, workers=4) == Changes([i.id for i in items[-3:]], [], [])
    assert sources.reads - reads < 3 * 20  # only the new scenes were read from USGS

    # Only the new scenes' files were uploaded, and the index and tables after them
    names = [path.removeprefix(f"{DEST}/") for path in bucket.content_types]
    data_files = [name for name in names if name not in ALWAYS_COPY]
    assert data_files and all(any(item.id in name for item in items[-3:]) for name in data_files)
    assert names[: len(data_files)] == data_files and set(names[len(data_files):]) == ALWAYS_COPY
    assert {name: bucket.cache_control[f"{DEST}/{name}"] for name in ALWAYS_COPY} == dict.fromkeys(ALWAYS_COPY, "no-cache")
    assert not any(bucket.cache_control[f"{DEST}/{name}"] for name in data_files)

    # The site is what a full run over the whole catalogue makes
    full = build(site, items, str(tmp_path / "full"), monkeypatch)
    assert files_under(published(bucket)).keys() == files_under(full.cog_root).keys()
    entries = assert_same_site(bucket, first, full)
    assert all(entries[scene_id] == entry for scene_id, entry in entries_before.items())

    # Again, with nothing new: nothing is read or written
    bucket.content_types.clear()
    reads = sources.reads
    assert update_site("flask_tiny", publish_to=PUBLISH_TO, dest_fs=bucket, workers=4) == Changes([], [], [])
    assert not bucket.content_types and sources.reads == reads


def test_update_retries_scenes_that_failed(site, items, sources, tmp_path, bucket, monkeypatch):
    broken = next(item.id for item in items if item.id.startswith("LC08"))
    working = crop.crop_band

    def outage(href, out_path, keep_empty, bounds):
        if broken in href:
            raise OSError("simulated outage")
        return working(href, out_path, keep_empty, bounds)

    monkeypatch.setattr(crop, "crop_band", outage)
    build(site, items, str(tmp_path / "first"), monkeypatch).publish(PUBLISH_TO, dest_fs=bucket)
    status = lambda: pd.read_parquet(published(bucket) / "scenes.parquet").set_index("scene_id").scene_status[broken]
    assert status() == "error"

    monkeypatch.setattr(crop, "crop_band", working)
    assert update_site("flask_tiny", publish_to=PUBLISH_TO, dest_fs=bucket, workers=4) == Changes([], [broken], [])
    assert status() == "ok"
    entry = next(e for e in json.loads((published(bucket) / "index.json").read_text())["scenes"] if e["id"] == broken)
    assert "rgb" in entry["images"]


def test_update_follows_scenes_usgs_withdraws_or_reprocesses(site, items, sources, tmp_path, bucket, monkeypatch):
    first = build(site, items, str(tmp_path / "first"), monkeypatch)
    first.publish(PUBLISH_TO, dest_fs=bucket)

    # USGS reprocesses the newest scene, which comes back under an id with a new processing date
    old = items[-1]
    reprocessed = old.clone()
    parts = old.id.split("_")
    parts[4] = "20261101"
    reprocessed.id = "_".join(parts)
    catalogue_now = items[:-1] + [reprocessed]
    catalogue(monkeypatch, catalogue_now)
    assert update_site("flask_tiny", publish_to=PUBLISH_TO, dest_fs=bucket, workers=4) == Changes([reprocessed.id], [], [old.id])

    # Then withdraws another, with nothing new: no scene is read from USGS
    gone = items[3]
    catalogue_now = [item for item in catalogue_now if item is not gone]
    catalogue(monkeypatch, catalogue_now)
    reads = sources.reads
    assert update_site("flask_tiny", publish_to=PUBLISH_TO, dest_fs=bucket, workers=4) == Changes([], [], [gone.id])
    assert sources.reads == reads

    full = build(site, catalogue_now, str(tmp_path / "full"), monkeypatch)
    entries = assert_same_site(bucket, first, full)
    assert old.id not in entries and gone.id not in entries and reprocessed.id in entries
    assert (published(bucket) / old.id).is_dir()  # the withdrawn scenes' files stay


def test_a_search_missing_many_published_scenes_changes_nothing():
    scenes = pd.DataFrame({"scene_id": [f"S{n:03d}" for n in range(100)], "scene_status": "ok"})
    items = [SimpleNamespace(id=f"S{n:03d}") for n in range(100)]
    assert compare(items[10:], scenes)[2] == [f"S{n:03d}" for n in range(10)]
    with pytest.raises(SiteError, match="Nothing was changed"):
        compare(items[40:], scenes)


def test_update_from_the_command_line(site, items, sources, tmp_path, bucket, monkeypatch, config_home, fake_key, capsys):
    build(site, items[:-3], str(tmp_path / "first"), monkeypatch).publish(PUBLISH_TO, dest_fs=bucket)
    catalogue(monkeypatch, items)
    keys_used = []
    monkeypatch.setattr(publish, "gcs_filesystem", lambda key: keys_used.append(key) or bucket)
    monkeypatch.setattr(cli, "missing_permissions", lambda key, bucket: [])
    capsys.readouterr()

    # A dry run reads the public site anonymously, and writes nothing
    bucket.content_types.clear()
    assert cli.main(["update", "flask_tiny", "--dry-run", "--publish-to", PUBLISH_TO]) == 0
    out = capsys.readouterr().out
    assert "New since publishing: 3 scenes" in out and "Dry run: nothing was written" in out
    assert keys_used == [None] and not bucket.content_types

    # For real, it needs the key
    assert cli.main(["update", "flask_tiny", "--publish-to", PUBLISH_TO]) == 2
    assert "landsat-record login" in capsys.readouterr().err
    store_key(fake_key)
    assert cli.main(["update", "flask_tiny", "--publish-to", PUBLISH_TO, "--workers", "4"]) == 0
    output = capsys.readouterr()
    assert f"Site     {DEST} as publisher@example.iam.gserviceaccount.com" in output.out
    assert "Viewer: https://storage.googleapis.com/test-bucket/landsat/flask_tiny/index.html" in output.out
    assert keys_used[-1] == cli.stored_key_path()
    assert SECRET_FRAGMENT not in output.out + output.err

    # A site that was never published
    assert cli.main(["update", "elsewhere", "--dry-run", "--publish-to", PUBLISH_TO]) == 2
    assert "landsat-record run elsewhere" in capsys.readouterr().err


def test_sites_published_by_an_older_version_are_refused(bucket):
    index = {"title": "Old Landsat record", "aoi_vertices": [[0, 0], [1, 0], [1, 1]], "scenes": [{"id": "LC08_X", "datetime": "2020-01-01T00:00:00Z"}]}
    bucket.pipe_file(f"{DEST}/index.json", json.dumps(index).encode())
    with pytest.raises(SiteError, match="older version"):
        update_site("flask_tiny", publish_to=PUBLISH_TO, dest_fs=bucket, dry_run=True)
