"""Convert the cropped GeoTIFFs to Cloud Optimized GeoTIFFs in a separate <out_root>_cog folder.

Deflate-compressed with 256 px tiles and overviews. QA bands use nearest-neighbour overviews, because averaging would
corrupt their bit flags. Full-resolution pixels are unchanged, and a file is only uploaded after that's been checked.
Nothing is read from USGS, and the original GeoTIFFs aren't changed.
"""
import tempfile
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.shutil import copy as rio_copy

from .crop import MTL_ASSET, QA_BANDS
from .progress import pool_map, progress_bar, say


def bare(path):
    return path.removeprefix("s3://")


def existing_sizes(fs, root):
    fs.invalidate_cache()
    return {path: info["size"] for path, info in fs.find(root, detail=True).items()}


def cog_problems(plain_path, cog_path):
    """Differences between a source GeoTIFF and its COG, plus anything that makes it not a valid COG."""
    problems = []
    with rasterio.open(plain_path) as plain, rasterio.open(cog_path) as cog:
        if cog.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG":
            problems.append("not a COG")
        if min(cog.width, cog.height) > 512 and not cog.overviews(1):
            problems.append("no overviews")
        for name in ("crs", "transform", "width", "height", "dtypes", "nodata"):
            if getattr(plain, name) != getattr(cog, name):
                problems.append(name)
        if not np.array_equal(plain.read(1), cog.read(1)):
            problems.append("pixels")
    return problems


def convert_band(record, row, existing):
    """Convert one raster unless it's already done. Returns (dest key, size in bytes, error message or None)."""
    fs = record.fs
    dest = row.key.replace(record.out_root, record.cog_root, 1)
    if bare(dest) in existing:
        return dest, existing[bare(dest)], None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            plain_path, cog_path = f"{tmp}/plain.tif", f"{tmp}/cog.tif"
            Path(plain_path).write_bytes(fs.cat(row.key))
            rio_copy(
                plain_path,
                cog_path,
                driver="COG",
                compress="DEFLATE",
                predictor="YES",
                blocksize=256,
                overview_resampling="nearest" if row.band in QA_BANDS else "average",
            )
            problems = cog_problems(plain_path, cog_path)
            if problems:
                raise ValueError(f"COG differs from source: {problems}")
            size = Path(cog_path).stat().st_size
            fs.put_file(cog_path, dest)
        return dest, size, None
    except Exception as error:
        return dest, None, f"{type(error).__name__}: {error}"[:500]


def convert_all(record, bands):
    """Convert every raster and copy the MTL files. Returns the bands table pointing at the new files, and the failures."""
    fs = record.fs
    existing = existing_sizes(fs, record.cog_root)
    rasters = bands[(bands.status == "ok") & (bands.band != MTL_ASSET)]
    start = time.perf_counter()

    with progress_bar(len(rasters), "COGs", "rasters") as bar:
        converted = pool_map(lambda row: convert_band(record, row, existing), list(rasters.itertuples()), record.workers, bar)
    results = dict(zip(rasters.index, converted))

    # Copy metadata as is
    mtl_rows = bands[(bands.status == "ok") & (bands.band == MTL_ASSET)]

    def copy_mtl(key):
        dest = key.replace(record.out_root, record.cog_root, 1)
        if bare(dest) not in existing:
            fs.cp_file(key, dest)
        return dest

    with progress_bar(len(mtl_rows), "Metadata", "files") as bar:
        mtl_dests = dict(zip(mtl_rows.index, pool_map(copy_mtl, list(mtl_rows.key), record.workers, bar)))

    updated = bands.copy()
    for index, (dest, size, error) in results.items():
        updated.loc[index, "key"] = dest
        updated.loc[index, "bytes"] = size
        if error:
            updated.loc[index, "status"] = "error"
            updated.loc[index, "reason"] = error
    for index, dest in mtl_dests.items():
        updated.loc[index, "key"] = dest

    with fs.open(f"{record.cog_root}/bands.parquet", "wb") as f:
        updated.to_parquet(f)
    for name in ("scenes.parquet", "items.json"):
        if fs.exists(f"{record.out_root}/{name}"):
            fs.cp_file(f"{record.out_root}/{name}", f"{record.cog_root}/{name}")

    failures = {bands.loc[i, "key"]: error for i, (_, _, error) in results.items() if error}
    say(f"{len(results) - len(failures)} of {len(results)} rasters are COGs, in {(time.perf_counter() - start) / 60:.1f} min")
    return updated, failures
