"""Copy a finished record to a public bucket, where the viewer link needs no login.

nasa-cryo-scratch isn't public, so colleagues can't reach anything there. Everything copied to a public bucket is
readable by anyone (Landsat data is public domain, so that's fine for these files), and public downloads are billed
to the bucket owner.
"""
from pathlib import Path

from .browse import viewer_html
from .cogs import bare
from .progress import pool_map, progress_bar, say

DEFAULT_PUBLISH_TO = "gs://ldeo-glaciology/landsat"

# Without these the viewer page would download rather than open
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
    ".webp": "image/webp",
    ".tif": "image/tiff; application=geotiff; profile=cloud-optimized",
}
ALWAYS_COPY = {"index.html", "index.json", "bands.parquet", "scenes.parquet", "items.json"}
# These change with every publish, so browsers and Google's caches must check for a newer copy rather than keep one
# for the default hour. The data files never change once written.
NO_CACHE = {"cache_control": "no-cache"}


def gcs_filesystem(key_path):
    """The bucket, written to with the key at key_path, or read anonymously when it's None."""
    import gcsfs

    return gcsfs.GCSFileSystem(token=str(key_path) if key_path else "anon")  # a path, so the key's contents are never held here


def site_root(publish_to, name, test=False):
    return f"{publish_to.rstrip('/')}/{name}" + ("_test" if test else "")


def public_url(dest_root):
    return f"https://storage.googleapis.com/{dest_root.removeprefix('gs://')}/index.html"


def publish(record, dest_fs, dest_root, workers):
    """Copy everything under the COG folder to dest_root, skipping files already there. Returns the number copied.

    The index and tables go last, so the published page never lists a scene whose files aren't there yet.
    """
    fs = record.fs
    fs.invalidate_cache()
    dest_fs.invalidate_cache()
    prefix = bare(record.cog_root) + "/"
    relative = [path[len(prefix):] for path in fs.find(record.cog_root)]
    already = set(dest_fs.find(dest_root))
    bare_dest = dest_root.removeprefix("gs://")
    todo = [rel for rel in relative if rel in ALWAYS_COPY or f"{bare_dest}/{rel}" not in already]
    say(f"{len(relative) - len(todo)} of {len(relative)} files are already there; copying {len(todo)}")

    copied_bytes = 0

    def copy_one(rel):
        content_type = CONTENT_TYPES.get(Path(rel).suffix, "application/octet-stream")
        data = fs.cat(f"{record.cog_root}/{rel}")
        metadata = NO_CACHE if rel in ALWAYS_COPY else None
        dest_fs.pipe_file(f"{dest_root}/{rel}", data, content_type=content_type, fixed_key_metadata=metadata)
        return len(data)

    def on_result(rel, size):
        nonlocal copied_bytes
        copied_bytes += size
        bar.set_postfix_str(f"{copied_bytes / 1e9:.2f} GB")

    with progress_bar(len(todo), "Publish", "files") as bar:
        pool_map(copy_one, [rel for rel in todo if rel not in ALWAYS_COPY], workers, bar, on_result)
        pool_map(copy_one, [rel for rel in todo if rel in ALWAYS_COPY], workers, bar, on_result)
    return len(todo)


def update_viewer(record, dest_fs, dest_root):
    """Replace only index.html, in the bucket and on scratch, so a later publish doesn't put the old page back.

    Anyone who opened the old page may keep seeing it for up to an hour, because of the bucket's default caching.
    """
    html = viewer_html()
    dest_fs.pipe_file(f"{dest_root}/index.html", html, content_type=CONTENT_TYPES[".html"], fixed_key_metadata=NO_CACHE)
    record.fs.pipe_file(f"{record.cog_root}/index.html", html)
