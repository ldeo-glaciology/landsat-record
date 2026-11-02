"""Find every Landsat Collection 2 Level 1 scene over a site, and pick a small sample for a test run."""
from collections import Counter

import pystac_client
from pystac_client.stac_api_io import StacApiIO
from urllib3 import Retry

STAC_URL = "https://landsatlook.usgs.gov/stac-server"
COLLECTION = "landsat-c2l1"


def search_catalogue(geometry):
    """Every scene that intersects a lon/lat geometry. Returns (item collection, items sorted by date).

    No cloud, date or sun-angle filter: the aim is the complete record. limit=100 because the API returned a 502 at
    limit=500.
    """
    retry = Retry(total=6, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=None)
    catalog = pystac_client.Client.open(STAC_URL, stac_io=StacApiIO(max_retries=retry))
    search = catalog.search(collections=[COLLECTION], intersects=geometry, limit=100)
    item_collection = search.item_collection()
    items = sorted(item_collection, key=lambda item: item.datetime)

    # Stop here rather than silently working from a partial listing
    matched = search.matched()
    if matched is not None and matched != len(items):
        raise RuntimeError(f"the STAC API matched {matched} scenes but returned {len(items)}; try again")
    if len({item.id for item in items}) != len(items):
        raise RuntimeError("the STAC API returned duplicate scene ids; try again")
    return item_collection, items


def select_test_items(items):
    """One scene per sensor/platform ID prefix, plus the awkward cases where the site has them: ETM+ before and after
    the SLC failure, and a night scene."""
    selected = {}
    for item in items:  # items are sorted by date, so this takes the earliest of each
        prefix = item.id[:4]
        if prefix != "LE07":
            selected.setdefault(prefix, item)
    awkward = {
        "LE07 SLC-on": lambda i: i.id[:4] == "LE07" and i.datetime.year < 2003,
        "LE07 SLC-off": lambda i: i.id[:4] == "LE07" and i.datetime.year > 2003,
        "night": lambda i: i.properties["view:sun_elevation"] <= 0,
    }
    for name, wanted in awkward.items():
        match = next((i for i in items if wanted(i) and i not in selected.values()), None)
        if match:
            selected[name] = match
    etm = next((i for i in items if i.id[:4] == "LE07"), None)
    if etm and not any(i.id[:4] == "LE07" for i in selected.values()):  # ETM+ scenes only from 2003
        selected["LE07"] = etm
    return sorted(selected.values(), key=lambda item: item.datetime)


def summarise(items):
    if not items:
        return "no scenes"
    counts = dict(sorted(Counter(item.id[:4] for item in items).items()))
    return f"{len(items):,} scenes, {items[0].datetime:%Y-%m-%d} to {items[-1].datetime:%Y-%m-%d} {counts}"
