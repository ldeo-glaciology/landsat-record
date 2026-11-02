"""Crop every band of every scene to the site at native resolution, and keep a catalogue of the results.

Reads come from the requester-pays usgs-landsat bucket, so this has to run where there are AWS credentials, such as
CryoCloud. Windowed rasterio reads are used rather than xr.concat: the sensors have different bands and resolutions,
and concat would pad every scene to the union of all the grids.
"""
import json
import math
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import boto3
import pandas as pd
import rasterio
from rasterio.session import AWSSession
from rasterio.windows import Window, from_bounds

from .progress import pool_map, progress_bar, say

S3_REGION = "us-west-2"  # region of the usgs-landsat bucket
MAX_PASSES = 3  # scenes with failed bands are retried up to this many times per run

# Selected by name and file type, because the STAC roles are unreliable here: VZA and SZA are labelled "data",
# while qa_pixel and qa_radsat are not.
ANGLE_BANDS = {"VAA", "VZA", "SAA", "SZA"}
QA_BANDS = {"qa_pixel", "qa_radsat"}
MTL_ASSET = "MTL.json"


class AccessError(RuntimeError):
    """The requester-pays Landsat bucket can't be read from here."""


def s3_href(asset):
    return asset.extra_fields["alternate"]["s3"]["href"]


def bands_for(item):
    """Band asset names for a scene: data bands first, then QA, because the QA bands depend on the data-band results."""
    names = [
        name for name, asset in item.assets.items()
        if asset.media_type and "geotiff" in asset.media_type and name not in ANGLE_BANDS
    ]
    return sorted(names, key=lambda name: name in QA_BANDS)


def rio_env():
    # A new boto3 session for each use, because sessions are not thread-safe and hub credentials expire
    return rasterio.Env(
        AWSSession(boto3.Session(region_name=S3_REGION), requester_pays=True),
        AWS_REQUEST_PAYER="requester",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".TIF",
        GDAL_HTTP_MAX_RETRY="5",
        GDAL_HTTP_RETRY_DELAY="2",
    )


def aoi_window(src, bounds):
    """Whole-pixel window around bounds, clipped to the raster. None if they don't overlap."""
    window = from_bounds(*bounds, transform=src.transform)
    col_start = max(0, math.floor(window.col_off))
    row_start = max(0, math.floor(window.row_off))
    col_stop = min(src.width, math.ceil(window.col_off + window.width))
    row_stop = min(src.height, math.ceil(window.row_off + window.height))
    if col_stop <= col_start or row_stop <= row_start:
        return None
    return Window(col_start, row_start, col_stop - col_start, row_stop - row_start)


def blocks_touched(src, window):
    """Number of internal COG blocks a window overlaps, used to estimate S3 requests."""
    block_h, block_w = src.block_shapes[0]
    n_cols = (window.col_off + window.width - 1) // block_w - window.col_off // block_w + 1
    n_rows = (window.row_off + window.height - 1) // block_h - window.row_off // block_h + 1
    return int(n_cols * n_rows)


def crop_band(href, out_path, keep_empty, bounds):
    """
    Crop one band to out_path with no resampling. Returns a record dict.

    If the crop has no valid pixels, no file is written and the status is no_data, unless keep_empty is set. QA bands
    use keep_empty, because a qa_radsat value of 0 means "not saturated" rather than "no data".
    """
    with rasterio.open(href) as src:
        window = aoi_window(src, bounds)
        if window is None:
            return {"status": "no_data", "reason": "outside_raster", "res": src.res[0]}

        data = src.read(1, window=window)
        nodata = src.nodata if src.nodata is not None else 0
        transform = src.window_transform(window)
        record = {
            "status": "ok",
            "reason": None,
            "valid_frac": float((data != nodata).mean()),
            "width": window.width,
            "height": window.height,
            "res": src.res[0],
            "x_origin": transform.c,
            "y_origin": transform.f,
            "crs": str(src.crs),
            "dtype": src.dtypes[0],
            "nodata": src.nodata,
            "blocks": blocks_touched(src, window),
        }
        if record["valid_frac"] == 0 and not keep_empty:
            return {**record, "status": "no_data", "reason": "all_nodata"}

        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            width=window.width,
            height=window.height,
            transform=transform,
            compress="deflate",
            predictor=2,
            tiled=True,
            blockxsize=256,
            blockysize=256,
        )
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)
            dst.update_tags(**src.tags(), source=href)

    record["bytes"] = Path(out_path).stat().st_size
    return record


def fetch_mtl(href, out_path):
    url = urlparse(href)
    client = boto3.Session(region_name=S3_REGION).client("s3")
    response = client.get_object(Bucket=url.netloc, Key=url.path.lstrip("/"), RequestPayer="requester")
    Path(out_path).write_bytes(response["Body"].read())


def check_access(item, bounds):
    """Read one window of one scene from the requester-pays bucket, so a run fails now rather than hours in."""
    band = bands_for(item)[0]
    start = time.perf_counter()
    try:
        with rio_env(), rasterio.open(s3_href(item.assets[band])) as src:
            window = aoi_window(src, bounds)
            data = src.read(1, window=window) if window else None
    except Exception as error:
        raise AccessError(
            f"can't read the requester-pays usgs-landsat bucket ({type(error).__name__}: {error}). "
            "Run this on CryoCloud, where AWS credentials are set up, in us-west-2."
        ) from error
    shape = "nothing (outside)" if data is None else f"{data.shape[1]} × {data.shape[0]} px"
    return f"AWS access OK: read {shape} of {item.id} {band} in {time.perf_counter() - start:.1f} s"


def strip_scheme(path):
    return path.removeprefix("s3://")


def is_done(previous, key, existing):
    """Should a band be skipped on this run? no_data results are final; ok results also need the file to still exist."""
    if previous is None:
        return False
    return previous["status"] == "no_data" or (previous["status"] == "ok" and strip_scheme(key) in existing)


def crop_scene(record, item, existing):
    """
    Crop all bands of a scene and upload them with its MTL.json, skipping finished work.

    A <scene_id>_record.json next to the rasters holds the result for each band and is what makes runs resumable.
    Returns (band records, number of network reads made).
    """
    fs = record.fs
    bounds = record.site.bounds
    scene_id = item.id
    prefix = f"{record.out_root}/{scene_id}"
    record_key = f"{prefix}/{scene_id}_record.json"
    previous = {}
    if strip_scheme(record_key) in existing:
        previous = {row["band"]: row for row in json.loads(fs.cat(record_key))}

    rows = []
    n_reads = 0
    data_ok = False  # has any data band got valid pixels?
    data_error = False

    def attempt(band, key, work):
        """Run work() for one band, recording the time taken and any failure rather than stopping the scene."""
        nonlocal n_reads
        n_reads += 1
        start = time.perf_counter()
        try:
            result = work()
        except Exception as error:
            result = {"status": "error", "reason": f"{type(error).__name__}: {error}"[:500]}
        rows.append({"scene_id": scene_id, "band": band, "key": key, "seconds": time.perf_counter() - start, **result})
        return rows[-1]

    with tempfile.TemporaryDirectory() as tmp, rio_env():
        for band in bands_for(item):
            key = f"{prefix}/{scene_id}_{band}.tif"
            local = f"{tmp}/{scene_id}_{band}.tif"
            is_qa = band in QA_BANDS

            if is_done(previous.get(band), key, existing):
                row = previous[band]
                rows.append(row)
            elif is_qa and not data_ok:
                # QA bands are only worth keeping if the scene has data here. Don't record no_data as final while
                # a data-band failure could still turn out to be the reason.
                row = {
                    "scene_id": scene_id,
                    "band": band,
                    "key": key,
                    "seconds": 0.0,
                    "status": "error" if data_error else "no_data",
                    "reason": "data_band_failed" if data_error else "no_valid_data_bands",
                }
                rows.append(row)
            else:
                def work():
                    result = crop_band(s3_href(item.assets[band]), local, keep_empty=is_qa, bounds=bounds)
                    if result["status"] == "ok":
                        fs.put_file(local, key)
                    return result

                row = attempt(band, key, work)

            if not is_qa:
                data_ok = data_ok or row["status"] == "ok"
                data_error = data_error or row["status"] == "error"

        # MTL.json is kept for every scene, since Level 1 DNs can't be calibrated without it
        mtl_key = f"{prefix}/{scene_id}_MTL.json"
        if is_done(previous.get(MTL_ASSET), mtl_key, existing):
            rows.append(previous[MTL_ASSET])
        else:
            def work():
                local = f"{tmp}/{scene_id}_MTL.json"
                fetch_mtl(s3_href(item.assets[MTL_ASSET]), local)
                fs.put_file(local, mtl_key)
                return {"status": "ok", "bytes": Path(local).stat().st_size}

            attempt(MTL_ASSET, mtl_key, work)

    if n_reads or len(previous) != len(rows):
        with fs.open(record_key, "w") as f:
            json.dump(rows, f)
    return rows, n_reads


def safe_crop_scene(record, item, existing):
    """Keep one scene's unexpected failure from stopping the whole run."""
    try:
        return crop_scene(record, item, existing)
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"[:500]
        return [{"scene_id": item.id, "band": "*", "status": "error", "reason": reason, "seconds": 0.0}], 1


def scene_status(rows):
    if any(row["status"] == "error" for row in rows):
        return "error"
    data_rows = [row for row in rows if row["band"] not in QA_BANDS and row["band"] != MTL_ASSET]
    return "ok" if any(row["status"] == "ok" for row in data_rows) else "no_data"


def scenes_table(items, records):
    """One row per catalogue scene. scene_status is not_run for scenes this run didn't include."""
    table = pd.DataFrame(
        [
            {
                "scene_id": item.id,
                "datetime": pd.Timestamp(item.datetime),
                "prefix": item.id[:4],
                "platform": item.properties["platform"],
                "wrs_path": item.properties["landsat:wrs_path"],
                "wrs_row": item.properties["landsat:wrs_row"],
                "processing_level": item.id.split("_")[1],
                "collection_category": item.properties.get("landsat:collection_category"),
                "cloud_cover": item.properties.get("eo:cloud_cover"),
                "sun_elevation": item.properties.get("view:sun_elevation"),
                "sun_azimuth": item.properties.get("view:sun_azimuth"),
                "scene_status": scene_status(records[item.id]) if item.id in records else "not_run",
            }
            for item in items
        ]
    )
    return table


def write_tables(record, records):
    fs = record.fs
    with fs.open(f"{record.out_root}/scenes.parquet", "wb") as f:
        scenes_table(record.items, records).to_parquet(f)
    bands = pd.DataFrame([row for rows in records.values() for row in rows])
    with fs.open(f"{record.out_root}/bands.parquet", "wb") as f:
        bands.to_parquet(f)
    return bands


def run_all(record, items, max_passes=MAX_PASSES):
    """Process scenes in a thread pool. Returns (bands table, number of network reads across all passes)."""
    fs = record.fs
    records = {}
    total_reads = 0
    todo = list(items)
    start = time.perf_counter()

    for attempt_number in range(max_passes):
        if attempt_number:
            say(f"Retrying {len(todo)} scenes with failures (pass {attempt_number + 1} of {max_passes})")
            time.sleep(30 * attempt_number)

        fs.invalidate_cache()
        existing = {strip_scheme(path) for path in fs.find(record.out_root)}
        n_done = n_errors = 0

        def on_result(item, result):
            nonlocal total_reads, n_done, n_errors
            rows, n_reads = result
            records[item.id] = rows
            total_reads += n_reads
            n_done += 1
            if scene_status(rows) == "error":
                n_errors += 1
                bar.set_postfix_str(f"{n_errors} with errors")
            if n_done % 25 == 0:
                write_tables(record, records)  # keep the catalogue current in case the run stops

        desc = "Crop" if not attempt_number else f"Crop, pass {attempt_number + 1}"
        with progress_bar(len(todo), desc, "scenes") as bar:
            try:
                pool_map(lambda item: safe_crop_scene(record, item, existing), todo, record.workers, bar, on_result)
            except KeyboardInterrupt:
                write_tables(record, records)
                raise

        todo = [item for item in todo if scene_status(records[item.id]) == "error"]
        if not todo:
            break

    bands = write_tables(record, records)
    statuses = Counter(scene_status(rows) for rows in records.values())
    say(f"{dict(statuses)} in {(time.perf_counter() - start) / 60:.1f} min, {total_reads} network reads")
    if todo:
        say(f"Scenes still failing (re-run to retry): {[item.id for item in todo]}")
    return bands, total_reads


def project_full_run(bands, scenes, workers):
    """Scale a test run up to the whole catalogue.

    Time and size per scene are averaged by ID prefix, then scaled by the number of scenes of each prefix in the full
    catalogue. The request count is an upper bound: two header reads per band plus one per COG block touched. It
    assumes S3 GET requests at $0.0004 per 1,000.
    """
    tif_rows = bands[(bands.status == "ok") & (bands.band != MTL_ASSET)]
    per_scene = (
        bands.groupby("scene_id")  # every band counts for time, since an empty crop still takes a read
        .agg(seconds=("seconds", "sum"), bytes=("bytes", "sum"))
        .join(tif_rows.groupby("scene_id").agg(blocks=("blocks", "sum"), n_bands=("band", "size")))
    )
    per_scene["prefix"] = per_scene.index.str[:4]
    mean_by_prefix = per_scene.groupby("prefix")[["seconds", "bytes", "blocks", "n_bands"]].mean()

    counts = scenes.prefix.value_counts().rename("n_scenes")
    table = mean_by_prefix.join(counts, how="outer")
    if table.isna().any().any():
        say(f"WARNING: some prefixes were not measured, so these totals are too low: {table[table.isna().any(axis=1)].index.tolist()}")

    total_seconds = (table.seconds * table.n_scenes).sum()
    total_gb = (table.bytes * table.n_scenes).sum() / 1e9
    requests = ((table.blocks + 2 * table.n_bands) * table.n_scenes).sum()
    hours = total_seconds / workers / 3600

    say("Full run, projected from this test:")
    say(f"  Scenes:           {table.n_scenes.sum():.0f}")
    say(f"  Output size:      {total_gb:.1f} GB (compressed)")
    say(f"  Time:             {hours:.1f} h on {workers} threads, if throughput scales from this test")
    say(f"  S3 GET requests:  at most {requests:,.0f}, about ${requests / 1000 * 0.0004:.2f}")
    return table
