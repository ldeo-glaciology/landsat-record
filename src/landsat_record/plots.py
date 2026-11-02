"""Quick looks at a record. Needs matplotlib, which is only imported here."""
import math

import numpy as np
from rasterio.io import MemoryFile
from rasterio.plot import plotting_extent

from .browse import BROWSE_BANDS


def quicklook(record, max_scenes=40):
    """One panel per processed scene (the first max_scenes), using the finest band with data. Check that the site
    falls inside the outline (red), that SLC-off stripes show up on later ETM+ scenes, and how night scenes behave."""
    import matplotlib.pyplot as plt

    scenes = record.scenes()
    bands = record.bands()
    shown = scenes[scenes.scene_status != "not_run"].sort_values("datetime").head(max_scenes)
    xs, ys = zip(*record.site.vertices)

    n_rows = max(1, math.ceil(len(shown) / 5))
    fig, axes = plt.subplots(n_rows, 5, figsize=(18, 3.7 * n_rows), constrained_layout=True, squeeze=False)
    axes = axes.ravel()

    for ax, info in zip(axes, shown.itertuples()):
        rows = bands[(bands.scene_id == info.scene_id) & (bands.status == "ok")].set_index("band")
        title = f"{info.prefix} {info.datetime:%Y-%m-%d}\nsun {info.sun_elevation:.0f}°, {info.scene_status}"
        band = next((b for b in BROWSE_BANDS if b in rows.index), None)
        if band is None:
            ax.text(0.5, 0.5, "no raster", ha="center", transform=ax.transAxes)
        else:
            with MemoryFile(record.fs.cat(rows.loc[band, "key"])) as mem, mem.open() as src:
                data = src.read(1, masked=True).astype(float).filled(np.nan)
                extent = plotting_extent(src)
            if np.isfinite(data).any():
                low, high = np.nanpercentile(data, [2, 98])
                ax.imshow(data, extent=extent, cmap="gray", vmin=low, vmax=high)
            title += f"\n{band} ({rows.loc[band, 'res']:.0f} m, {rows.loc[band, 'valid_frac']:.0%} valid)"
        ax.plot(xs, ys, color="red", linewidth=1)
        ax.set_title(title, fontsize=9)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_aspect("equal")

    for ax in axes[len(shown):]:
        ax.axis("off")
    return fig
