"""Browse images, index.json and the viewer page.

Each scene gets an 8-bit WebP of every data band at its native resolution, plus an RGB composite that the viewer
opens on. MSS has no blue band, so its composite is NIR/red/green, which the viewer labels as false colour. Everything
is stretched per scene, so brightness and colour can't be compared between scenes. Composites are for looking at, not
measuring: bands often saturate over snow, and clipped channels are filled in from the others (see render_composite).
index.json lists every scene, including empty ones, for the viewer page.
"""
import io
import json
from importlib.resources import files

import numpy as np
import pandas as pd
from PIL import Image, features
from rasterio.io import MemoryFile

from .cogs import bare, existing_sizes
from .crop import MTL_ASSET, QA_BANDS
from .progress import pool_map, progress_bar, say

BROWSE_BANDS = ["pan", "red", "lwir11", "lwir"]
COMPOSITES = [("red", "green", "blue"), ("nir08", "red", "green")]
GRID_COLUMNS = ["res", "width", "height", "x_origin", "y_origin"]
BROWSE_QUALITY = 80


def viewer_html():
    """The viewer page, as published at <site>/index.html."""
    return files("landsat_record").joinpath("viewer.html").read_bytes()


def read_band(cog_bytes):
    """Read a band from COG bytes. Returns it with a mask of its valid pixels."""
    with MemoryFile(cog_bytes) as mem, mem.open() as src:
        data = src.read(1)
        nodata = src.nodata if src.nodata is not None else 0
    return data, data != nodata


def encode_webp(red, green, blue, valid):
    buffer = io.BytesIO()
    rgba = np.dstack([red, green, blue, valid.astype("uint8") * 255])
    Image.fromarray(rgba, "RGBA").save(buffer, "WEBP", quality=BROWSE_QUALITY, method=4)
    return buffer.getvalue()


def render_browse(cog_bytes):
    """Turn COG bytes into greyscale WebP bytes, with nodata pixels transparent. A fully saturated band comes out white."""
    data, valid = read_band(cog_bytes)
    low, high = np.percentile(data[valid], [2, 98])
    gray = np.where(data >= high, 255, np.clip((data.astype("float32") - low) / max(high - low, 1) * 255, 0, 255)).astype("uint8")
    return encode_webp(gray, gray, gray, valid)


def render_composite(red_bytes, green_bytes, blue_bytes):
    """Turn three COGs into colour WebP bytes, with pixels transparent unless all three are valid.

    A separate stretch per channel tints the ice, because bands saturate over snow at different levels: in real scenes
    up to 83% of a channel sits at its maximum. So channels are balanced on the pixels where none is clipped, and a
    clipped value is replaced by the mean of the channels that aren't, or white if all three are clipped.
    """
    bands, masks = zip(*map(read_band, (red_bytes, green_bytes, blue_bytes)))
    valid = np.logical_and.reduce(masks)
    clipped = [band == band[valid].max() for band in bands]
    usable = valid & ~np.logical_or.reduce(clipped)
    if usable.sum() < 0.01 * valid.sum():
        usable = valid

    scaled = []
    for band in bands:
        low, middle = np.percentile(band[usable], [2, 50])
        scaled.append((band.astype("float32") - low) / max(middle - low, 1))

    n_unclipped = np.sum([~c for c in clipped], axis=0)
    total = np.sum([np.where(c, 0, s) for s, c in zip(scaled, clipped)], axis=0)
    fill = np.full(valid.shape, np.inf, dtype="float32")
    np.divide(total, n_unclipped, out=fill, where=n_unclipped > 0)
    filled = [np.where(c, fill, s) for s, c in zip(scaled, clipped)]

    brightness = np.mean(filled, axis=0)[valid]
    finite = brightness[np.isfinite(brightness)]
    high = max(np.percentile(finite, 98), 1) if finite.size else 1
    return encode_webp(*[(np.clip(f / high, 0, 1) * 255).astype("uint8") for f in filled], valid)


def data_bands(scene_rows):
    """A scene's bands with data, indexed by band name."""
    ok = scene_rows[(scene_rows.status == "ok") & ~scene_rows.band.isin(QA_BANDS | {MTL_ASSET})]
    return ok.set_index("band", drop=False)


def browse_band(ok):
    """The row of the single band to show when there's no composite: the first of BROWSE_BANDS, then any, or None."""
    preferred = [band for band in BROWSE_BANDS if band in ok.index]
    if preferred:
        return ok.loc[preferred[0]]
    return ok.iloc[0] if len(ok) else None


def composite_bands(ok):
    """The first of COMPOSITES whose three bands all have data on one pixel grid, or None."""
    for names in COMPOSITES:
        if set(names) <= set(ok.index) and len(ok.loc[list(names), GRID_COLUMNS].drop_duplicates()) == 1:
            return list(names)
    return None


def number_or_none(value):
    return None if pd.isna(value) else float(value)  # NaN isn't valid JSON, and would stop the viewer loading


def image_info(rows):
    """The index.json record of an image made from these rows, which share a pixel grid."""
    row = rows[0]
    res = float(row.res)
    return {
        "res": res,
        "valid": round(min(float(r.valid_frac) for r in rows), 3),
        "bounds": [
            float(row.x_origin),
            float(row.y_origin) - float(row.height) * res,
            float(row.x_origin) + float(row.width) * res,
            float(row.y_origin),
        ],
    }


def browse_entry(record, scene, scene_rows, existing):
    """Write one scene's browse images if needed. Returns its index.json entry and a list of errors."""
    fs = record.fs
    entry = {
        "id": scene.scene_id,
        "datetime": scene.datetime.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": scene.platform,
        "path": scene.wrs_path,
        "row": scene.wrs_row,
        "level": scene.processing_level,
        "cloud": number_or_none(scene.cloud_cover),
        "sun": number_or_none(scene.sun_elevation),
        "images": {},
        "composite": None,
        "default": None,
    }
    if scene_rows is None:
        return entry, []
    ok = data_bands(scene_rows)
    errors = []

    def add_image(name, rows, render):
        key = f"{record.cog_root}/browse/{name}/{scene.scene_id}.webp"
        try:
            if bare(key) not in existing:
                fs.pipe_file(key, render(*[fs.cat(row.key) for row in rows]))
            entry["images"][name] = image_info(rows)
        except Exception as error:
            errors.append(f"{scene.scene_id} {name}: {type(error).__name__}: {error}"[:500])

    names = composite_bands(ok)
    if names:
        add_image("rgb", [ok.loc[name] for name in names], render_composite)
        entry["composite"] = names if "rgb" in entry["images"] else None
    for row in ok.itertuples():
        add_image(row.band, [row], render_browse)

    fallback = browse_band(ok[ok.band.isin(list(entry["images"]))])
    entry["default"] = None if fallback is None else fallback.band
    return entry, errors


def build_viewer(record, processed, cog_bands):
    """Write the browse images, index.json and the viewer page. Returns the index."""
    if not features.check("webp"):
        raise RuntimeError("this Pillow build can't write WebP")
    fs = record.fs
    site = record.site
    existing = existing_sizes(fs, record.cog_root)

    # An earlier version of the notebook wrote one image per scene directly in browse/
    browse_prefix = bare(f"{record.cog_root}/browse/")
    old_layout = [path for path in existing if path.startswith(browse_prefix) and "/" not in path[len(browse_prefix):]]
    if old_layout:
        fs.rm(old_layout)
        say(f"Removed {len(old_layout)} browse images in the old layout")

    rows_by_scene = dict(tuple(cog_bands.groupby("scene_id")))
    scenes = list(processed.itertuples())
    with progress_bar(len(scenes), "Browse", "scenes") as bar:
        results = pool_map(lambda scene: browse_entry(record, scene, rows_by_scene.get(scene.scene_id), existing), scenes, record.workers, bar)

    index = {
        "title": f"{site.title} Landsat record",
        "product": "Landsat Collection 2 Level 1 (DN, stretched per scene)",
        "crs": "EPSG:3031",
        "aoi_bounds": list(site.bounds),
        "aoi_vertices": [list(vertex) for vertex in site.vertices],
        "cogs_available": True,
        "scenes": [entry for entry, _ in results],
    }
    with fs.open(f"{record.cog_root}/index.json", "w") as f:
        json.dump(index, f, separators=(",", ":"), allow_nan=False)
    fs.pipe_file(f"{record.cog_root}/index.html", viewer_html())

    errors = [error for _, scene_errors in results for error in scene_errors]
    entries = index["scenes"]
    say(f"{sum(bool(e['images']) for e in entries)} of {len(entries)} scenes have images, {sum('rgb' in e['images'] for e in entries)} with a composite")
    say(f"{sum(len(e['images']) for e in entries)} browse images in all")
    if errors:
        say("FAILED (re-run to retry):\n  " + "\n  ".join(errors))

    sizes = existing_sizes(fs, record.cog_root)
    browse_mb = sum(size for path, size in sizes.items() if path.endswith(".webp")) / 1e6
    say(f"Browse images {browse_mb:.0f} MB. Everything in {record.cog_root}: {sum(sizes.values()) / 1e9:.2f} GB")
    return index
