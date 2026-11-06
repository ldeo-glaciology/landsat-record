import json
from pathlib import Path

import pytest
from fsspec.implementations.local import LocalFileSystem

from landsat_record import Site, SiteError, stored_site
from landsat_record.record import resolve_site
from landsat_record.site import TO_3031, TO_LONLAT

FLASK = Path(__file__).parents[1] / "sites" / "flask_region.geojson"


def test_geojson_outline():
    site = Site.from_geojson("flask_region", FLASK, title="Flask Glacier")
    x0, y0, x1, y1 = site.bounds
    assert (round(x0), round(y0), round(x1), round(y1)) == (-2383338, 1215305, -2359830, 1239459)
    assert site.search_geometry()["type"] == "Polygon"


@pytest.mark.parametrize("wrap", ["feature", "geometry"])
def test_geojson_accepts_a_feature_or_bare_geometry(tmp_path, wrap):
    geometry = json.loads(FLASK.read_text())["features"][0]["geometry"]
    document = {"type": "Feature", "properties": {}, "geometry": geometry} if wrap == "feature" else geometry
    path = tmp_path / "outline.geojson"
    path.write_text(json.dumps(document))
    assert Site.from_geojson("flask_region", path).bounds == Site.from_geojson("flask_region", FLASK).bounds


def test_geojson_errors(tmp_path):
    polygon = {"type": "Polygon", "coordinates": [[[-66, -70], [-65, -70], [-65, -71], [-66, -70]]]}
    two = tmp_path / "two.geojson"
    two.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": polygon}] * 2}))
    with pytest.raises(SiteError, match="exactly one polygon"):
        Site.from_geojson("x", two)
    projected = tmp_path / "projected.geojson"
    projected.write_text(json.dumps({"type": "Polygon", "coordinates": [[[-2.37e6, 1.23e6], [-2.36e6, 1.23e6], [-2.36e6, 1.22e6], [-2.37e6, 1.23e6]]]}))
    with pytest.raises(SiteError, match="EPSG:3031"):
        Site.from_geojson("x", projected)
    north = tmp_path / "north.geojson"
    north.write_text(json.dumps({"type": "Polygon", "coordinates": [[[-66, -50], [-65, -50], [-65, -51], [-66, -50]]]}))
    with pytest.raises(SiteError, match="south of 60"):
        Site.from_geojson("x", north)


def test_lonlat_box_follows_its_parallels():
    site = Site.from_bbox("fossil_bluff", -68.6, -71.5, -67.9, -71.2)
    assert site.title == "Fossil Bluff"
    lats = {round(TO_LONLAT.transform(x, y)[1], 6) for x, y in site.vertices}
    assert lats == {-71.5, -71.2}
    assert len(site.vertices) > 20
    # The middle of the arc along a parallel bulges outside the straight line between the corners: it must be inside
    x0, y0, x1, y1 = site.bounds
    for lat in (-71.5, -71.2):
        x, y = TO_3031.transform(-68.25, lat)
        assert x0 <= x <= x1 and y0 <= y <= y1


def test_box_across_180_degrees_is_searched_in_two_parts():
    site = Site.from_bbox("ross", 175, -78, -175, -77)
    geometry = site.search_geometry()
    assert geometry["type"] == "MultiPolygon" and len(geometry["coordinates"]) == 2
    spans = sorted((min(p[0] for p in part[0]), max(p[0] for p in part[0])) for part in geometry["coordinates"])
    assert spans[0][0] == pytest.approx(-180) and spans[0][1] == pytest.approx(-175)
    assert spans[1][0] == pytest.approx(175) and spans[1][1] == pytest.approx(180)


def test_box_errors():
    with pytest.raises(SiteError, match="--bbox-3031"):
        Site.from_bbox("x", -2373000, 1226000, -2370000, 1229000)
    with pytest.raises(SiteError, match="south of 60"):
        Site.from_bbox("x", -68, -59, -67, -58)
    with pytest.raises(SiteError, match="less than north"):
        Site.from_bbox("x", -68, -71, -67, -72)
    with pytest.raises(SiteError, match="--bbox"):
        Site.from_bbox_3031("x", -68.6, -71.5, -67.9, -71.2)
    with pytest.raises(SiteError, match="min < max"):
        Site.from_bbox_3031("x", -2370000, 1226000, -2373000, 1229000)


def test_3031_box():
    site = Site.from_bbox_3031("flask_tiny", -2373000, 1226000, -2370000, 1229000)
    assert site.bounds == (-2373000, 1226000, -2370000, 1229000)
    assert site.search_geometry()["type"] == "Polygon"


@pytest.mark.parametrize("name", ["Fossil Bluff", "fossil/bluff", "_x", ""])
def test_bad_names(name):
    with pytest.raises(SiteError, match="site name"):
        Site.from_bbox(name, -68.6, -71.5, -67.9, -71.2)


def test_json_round_trip():
    site = Site.from_bbox("fossil_bluff", -68.6, -71.5, -67.9, -71.2, title="Fossil Bluff")
    assert Site.from_json(json.loads(json.dumps(site.to_json()))) == site


def test_resolve_site():
    box = Site.from_bbox("fb", -68.6, -71.5, -67.9, -71.2, title="Stored Title")
    other = Site.from_bbox("fb", -68.6, -71.5, -67.8, -71.2)
    assert resolve_site("fb", None, box).title == "Stored Title"
    assert resolve_site("fb", box, None, "New").title == "New"
    assert resolve_site("fb", Site.from_bbox("fb", -68.6, -71.5, -67.9, -71.2), box).title == "Stored Title"
    with pytest.raises(SiteError, match="different outline"):
        resolve_site("fb", other, box)
    with pytest.raises(SiteError, match="needs an outline"):
        resolve_site("fb", None, None)


def test_stored_site_from_site_json_and_notebook_index(tmp_path):
    fs = LocalFileSystem(auto_mkdir=True)
    scratch = str(tmp_path)
    assert stored_site("fb", scratch, fs) is None
    site = Site.from_bbox("fb", -68.6, -71.5, -67.9, -71.2, title="Fossil Bluff")

    # A site run only with the notebook has no site.json, just the index.json in its _cog folder
    index = {"title": "Fossil Bluff Landsat record", "aoi_vertices": [list(v) for v in site.vertices]}
    fs.pipe_file(f"{scratch}/fb_cog/index.json", json.dumps(index).encode())
    adopted = stored_site("fb", scratch, fs)
    assert adopted.title == "Fossil Bluff" and adopted.same_outline(site)

    # The test folder's site.json is found for a full run, and wins over an index.json
    fs.pipe_file(f"{scratch}/fb_test/site.json", json.dumps(site.to_json()).encode())
    assert stored_site("fb", scratch, fs) == site

    # Records that disagree are an error, rather than whichever is found first
    other = Site.from_bbox("fb", -68.6, -71.5, -68.2, -71.3)
    fs.pipe_file(f"{scratch}/fb_test/site.json", json.dumps(other.to_json()).encode())
    with pytest.raises(SiteError, match=r"different outlines:\n.*fb_test/site.json.*\n.*fb_cog/index.json"):
        stored_site("fb", scratch, fs)
