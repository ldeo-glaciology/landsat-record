"""Offline fixtures: 13 real STAC entries over Flask Glacier (one per sensor, plus the awkward cases), synthetic rasters
in place of the USGS bucket, and local files in place of S3."""
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from fsspec.implementations.local import LocalFileSystem
from pystac import ItemCollection
from rasterio.transform import from_origin

import landsat_record.crop as crop
import landsat_record.record as record_module
from landsat_record import LandsatRecord, Site

DATA = Path(__file__).parent / "data"
BOUNDS = (-2373000.0, 1226000.0, -2370000.0, 1229000.0)  # 3 km square on Flask Glacier
MARGIN = 1500
RES = {"pan": 15, "lwir11": 100, "lwir": 120}
FAKE_PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nNOT-A-REAL-KEY-c2VjcmV0LW1hdGVyaWFs\n-----END PRIVATE KEY-----\n"


class FakeGCS:
    """A bucket in a local folder that names its files the way gcsfs does ("bucket/path"), and remembers the content
    type and cache control each was written with."""

    def __init__(self, root):
        self.root = Path(root)
        self.content_types = {}
        self.cache_control = {}

    def local(self, path):
        return self.root / path.removeprefix("gs://")

    def find(self, path):
        base = self.local(path)
        return [str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file()] if base.exists() else []

    def pipe_file(self, path, value, content_type=None, fixed_key_metadata=None):
        target = self.local(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
        self.content_types[path] = content_type
        self.cache_control[path] = (fixed_key_metadata or {}).get("cache_control")

    def cat(self, path):
        return self.local(path).read_bytes()

    def invalidate_cache(self, path=None):
        pass


@pytest.fixture
def items():
    return sorted(ItemCollection.from_file(str(DATA / "flask_test_items.json")), key=lambda item: item.datetime)


@pytest.fixture
def site():
    return Site.from_bbox_3031("flask_tiny", *BOUNDS, title="Flask test")


class Sources:
    """Synthetic rasters for every band of every scene, each scene with a scenario that exercises one branch."""

    def __init__(self, folder, items):
        self.paths = {}  # asset href -> local file
        self.fail_once = set()
        self.reads = 0
        self.scenario = {}
        x0, y0, x1, y1 = BOUNDS
        for n, item in enumerate(items):
            prefix = item.id[:4]
            kind = "normal"
            if item.properties["view:sun_elevation"] <= 0:
                kind = "night_thermal_only"
            elif prefix == "LM02":
                kind = "all_nodata"
            elif prefix == "LM03":
                kind = "outside"
            elif prefix == "LT04":
                kind = "partial"
            elif prefix == "LO08":
                kind = "fail_once"
            self.scenario[item.id] = kind
            for band in crop.bands_for(item):
                res = 60 if prefix.startswith("LM") else RES.get(band, 30)
                left = x0 - MARGIN + 7.5 * (n % 2)  # some scenes are off-lattice
                top = y1 + MARGIN
                width = int((x1 - x0 + 2 * MARGIN) / res)
                height = int((y1 - y0 + 2 * MARGIN) / res)
                if kind == "outside":
                    left -= 1_000_000
                if kind == "partial":
                    width //= 2
                data = np.random.default_rng(n).integers(100, 60000, (height, width), dtype="uint16")
                is_qa = band in crop.QA_BANDS
                if band == "qa_radsat":
                    data[:] = 0  # 0 is a real value here and the band must still be kept
                if kind == "all_nodata" and not is_qa:
                    data[:] = 0
                if kind == "night_thermal_only" and not band.startswith("lwir") and not is_qa:
                    data[:] = 0
                path = folder / f"{item.id}_{band}.tif"
                with rasterio.open(
                    path, "w", driver="GTiff", width=width, height=height, count=1, dtype="uint16",
                    nodata=None if band == "qa_radsat" else 0, crs="EPSG:3031",
                    transform=from_origin(left, top, res, res), tiled=True, blockxsize=256, blockysize=256,
                ) as dst:
                    dst.write(data, 1)
                self.paths[item.assets[band].href] = str(path)
                if kind == "fail_once" and band == "green":
                    self.fail_once.add(str(path))
            mtl = folder / f"{item.id}_MTL.json"
            mtl.write_text(json.dumps({"id": item.id}))
            self.paths[item.assets["MTL.json"].href] = str(mtl)


@pytest.fixture
def sources(tmp_path, items, monkeypatch):
    """Point the crop step at synthetic rasters, and the search at the saved entries."""
    folder = tmp_path / "usgs"
    folder.mkdir()
    built = Sources(folder, items)
    real_crop_band = crop.crop_band

    def crop_band(href, out_path, keep_empty, bounds):
        built.reads += 1
        if href in built.fail_once:
            built.fail_once.discard(href)
            raise OSError("simulated S3 failure")
        return real_crop_band(href, out_path, keep_empty, bounds)

    def fetch_mtl(href, out_path):
        built.reads += 1
        Path(out_path).write_bytes(Path(href).read_bytes())

    collection = ItemCollection(items)
    monkeypatch.setattr(crop, "s3_href", lambda asset: built.paths[asset.href])
    monkeypatch.setattr(crop, "crop_band", crop_band)
    monkeypatch.setattr(crop, "fetch_mtl", fetch_mtl)
    monkeypatch.setattr(crop, "rio_env", lambda: rasterio.Env())
    monkeypatch.setattr(crop.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(record_module, "search_catalogue", lambda geometry: (collection, list(items)))
    return built


@pytest.fixture
def local_fs():
    return LocalFileSystem(auto_mkdir=True)


@pytest.fixture
def scratch(tmp_path):
    return str(tmp_path / "scratch")


@pytest.fixture
def record(site, sources, local_fs, scratch):
    return LandsatRecord(site, test=True, scratch=scratch, workers=4, fs=local_fs)


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """A private config folder, so tests never touch a real stored key."""
    folder = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(folder))
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    return folder


@pytest.fixture
def fake_key(tmp_path):
    path = tmp_path / "downloads" / "my-service-key.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "type": "service_account",
        "project_id": "example",
        "private_key_id": "0123456789abcdef",
        "private_key": FAKE_PRIVATE_KEY,
        "client_email": "publisher@example.iam.gserviceaccount.com",
    }))
    return path
