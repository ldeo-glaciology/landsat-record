import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from fsspec.implementations.local import LocalFileSystem

import landsat_record.crop as crop
from landsat_record import LandsatRecord, Site
from landsat_record.browse import viewer_html
from landsat_record.publish import ALWAYS_COPY, CONTENT_TYPES
from landsat_record.site import SiteError


class RecordingFS(LocalFileSystem):
    """A local destination that remembers the content type each file was published with."""

    def __init__(self):
        super().__init__(auto_mkdir=True)
        self.content_types = {}

    def pipe_file(self, path, value, content_type=None, **kwargs):
        self.content_types[path] = content_type
        return super().pipe_file(path, value, **kwargs)


def build(record):
    record.search()
    record.check_access()
    record.crop()
    record.cogs()
    return record.browse()


def test_crops_match_their_source_windows(record, sources):
    record.search()
    bands = record.crop()
    tifs = bands[(bands.status == "ok") & (bands.band != crop.MTL_ASSET)]
    items = {item.id: item for item in record.run_items}
    assert len(tifs) > 50
    for row in tifs.itertuples():
        with rasterio.open(sources.paths[items[row.scene_id].assets[row.band].href]) as src, rasterio.open(row.key) as out:
            window = crop.aoi_window(src, record.site.bounds)
            assert out.crs == src.crs
            assert out.transform == src.window_transform(window)
            assert (out.height, out.width) == (window.height, window.width)
            assert out.dtypes[0] == src.dtypes[0] and out.nodata == src.nodata
            assert np.array_equal(out.read(1), src.read(1, window=window))


def test_every_band_is_accounted_for(record, sources):
    record.search()
    bands = record.crop()
    expected = {(item.id, band) for item in record.run_items for band in [*crop.bands_for(item), crop.MTL_ASSET]}
    assert set(zip(bands.scene_id, bands.band)) == expected
    assert not (bands.status == "error").any(), "the simulated failure should be retried and succeed"
    status = record.scenes().set_index("scene_id").scene_status
    by_kind = {kind: {status[s] for s, k in sources.scenario.items() if k == kind} for kind in set(sources.scenario.values())}
    assert by_kind["outside"] == {"no_data"} and by_kind["all_nodata"] == {"no_data"}
    assert by_kind["night_thermal_only"] == {"ok"} and by_kind["normal"] == {"ok"}


def test_resume_reads_nothing_twice(record, sources):
    record.search()
    bands = record.crop()
    _, reads = crop.run_all(record, record.run_items)
    assert reads == 0
    victim = bands[(bands.status == "ok") & (bands.band == "red")].key.iloc[0]
    Path(victim).unlink()
    _, reads = crop.run_all(record, record.run_items)
    assert reads == 1 and Path(victim).exists()


def test_interrupted_run_finishes_on_rerun(record, sources, monkeypatch):
    record.search()
    real = crop.safe_crop_scene
    calls = {"n": 0}

    def interrupted(rec, item, existing):
        calls["n"] += 1
        if calls["n"] == 5:
            raise KeyboardInterrupt
        return real(rec, item, existing)

    monkeypatch.setattr(crop, "safe_crop_scene", interrupted)
    with pytest.raises(KeyboardInterrupt):
        record.crop()
    monkeypatch.setattr(crop, "safe_crop_scene", real)
    bands = record.crop()
    assert set(bands.scene_id) == {item.id for item in record.run_items}
    assert not (bands.status == "error").any()


def test_cogs_browse_and_viewer(record, sources):
    index = build(record)
    cog_root = Path(record.cog_root)
    assert index["title"] == "Flask test Landsat record"
    assert index["aoi_bounds"] == list(record.site.bounds)
    assert len(index["scenes"]) == 13
    with_images = [scene for scene in index["scenes"] if scene["images"]]
    assert len(with_images) == 11  # all but the all-nodata and outside scenes
    night = next(scene for scene in with_images if scene["sun"] <= 0)
    assert night["default"] == "lwir11" and night["composite"] is None
    for scene in with_images:
        for name in scene["images"]:
            assert (cog_root / "browse" / name / f"{scene['id']}.webp").stat().st_size > 0
    assert (cog_root / "index.html").read_bytes() == viewer_html()
    for tif in cog_root.rglob("*.tif"):
        with rasterio.open(tif) as src:
            assert src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
    json.loads((cog_root / "index.json").read_text())  # valid JSON, with no NaN


def test_publish_copies_only_what_is_new(record, sources, tmp_path):
    build(record)
    dest = RecordingFS()
    url = record.publish(publish_to=str(tmp_path / "bucket"), dest_fs=dest)
    published = tmp_path / "bucket" / "flask_tiny_test"
    assert url.endswith("/flask_tiny_test/index.html")
    local = {str(p.relative_to(record.cog_root)) for p in Path(record.cog_root).rglob("*") if p.is_file()}
    assert {str(p.relative_to(published)) for p in published.rglob("*") if p.is_file()} == local
    types = {Path(path).suffix: kind for path, kind in dest.content_types.items()}
    for suffix in (".html", ".json", ".webp", ".tif"):
        assert types[suffix] == CONTENT_TYPES[suffix]

    dest.content_types.clear()
    record.publish(publish_to=str(tmp_path / "bucket"), dest_fs=dest)
    assert {Path(path).name for path in dest.content_types} == ALWAYS_COPY


def test_update_viewer_replaces_only_the_page(record, sources, tmp_path):
    build(record)
    dest = RecordingFS()
    record.publish(publish_to=str(tmp_path / "bucket"), dest_fs=dest)
    dest.content_types.clear()
    (Path(record.cog_root) / "index.html").write_text("old page")
    record.update_viewer(publish_to=str(tmp_path / "bucket"), dest_fs=dest)
    assert list(dest.content_types) == [f"{tmp_path}/bucket/flask_tiny_test/index.html"]
    assert (Path(record.cog_root) / "index.html").read_bytes() == viewer_html()


def test_projection_and_quicklook(record, sources):
    import matplotlib

    matplotlib.use("Agg")
    record.search()
    record.crop()
    table = record.project_full_run()
    assert table.n_scenes.sum() == 13
    figure = record.quicklook()
    assert len([ax for ax in figure.axes if ax.has_data()]) >= 11


def test_full_and_test_runs_use_separate_folders(site, sources, local_fs, scratch):
    full = LandsatRecord(site, test=False, scratch=scratch, fs=local_fs)
    test = LandsatRecord(site, test=True, scratch=scratch, fs=local_fs)
    assert full.out_root == f"{scratch}/flask_tiny" and test.out_root == f"{scratch}/flask_tiny_test"
    assert test.cog_root == f"{scratch}/flask_tiny_test_cog"
    assert test.publish_root("gs://bucket/landsat/") == "gs://bucket/landsat/flask_tiny_test"


def test_a_new_outline_under_an_existing_name_is_refused(record, sources, local_fs, scratch):
    """Otherwise the finished crops for the old outline are skipped over and kept, and the new outline only changes the
    search and the outline drawn on them."""
    record.crop()
    x0, y0, x1, y1 = record.site.bounds
    moved = Site.from_bbox_3031(record.site.name, x0 - 2000, y0 + 2000, x0, y1 + 2000, title="Somewhere nearby")
    for test in (True, False):  # one outline per site name, for the test and the full record
        with pytest.raises(SiteError, match="different outline"):
            LandsatRecord(moved, test=test, scratch=scratch, fs=local_fs)
    assert LandsatRecord(record.site, test=True, scratch=scratch, fs=local_fs).out_root == record.out_root
    assert LandsatRecord(Site.from_bbox_3031("nearby", *moved.bounds), scratch=scratch, fs=local_fs).out_root.endswith("/nearby")
