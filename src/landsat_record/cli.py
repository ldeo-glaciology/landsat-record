"""landsat-record: build a browsable Landsat record for an Antarctic site with one command."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from tqdm import tqdm

from . import __version__
from .crop import AccessError
from .keys import (
    KeyFileError,
    bucket_of,
    client_email,
    find_key,
    inside_git_repo,
    missing_permissions,
    remove_stored_key,
    store_key,
    stored_key_path,
)
from .publish import DEFAULT_PUBLISH_TO
from .record import STAGES, LandsatRecord, default_scratch, resolve_site, scratch_filesystem, stored_site
from .site import Site, SiteError, check_name

STAGE_TITLES = {
    "search": "Search the USGS catalogue",
    "crop": "Crop every band to the site",
    "cogs": "Convert to Cloud Optimized GeoTIFFs",
    "browse": "Browse images and viewer",
    "publish": "Publish",
}

EXAMPLES = """\
examples:
  landsat-record login ~/ldeo-glaciology-key.json --remove-original
  landsat-record run fossil_bluff --bbox -68.6 -71.5 -67.9 -71.2 --title "Fossil Bluff" --test
  landsat-record run fossil_bluff --publish
  landsat-record run flask_region --geojson sites/flask_region.geojson --title "Flask Glacier" --publish

A run can be stopped (Ctrl-C) and restarted: finished files are kept and failed ones are tried again.
Run "landsat-record run --help" for every option.
"""

RUN_DESCRIPTION = """\
Crops every Landsat Collection 2 Level 1 scene (1973 onwards) over a site at native resolution, converts the crops
to Cloud Optimized GeoTIFFs, and builds a viewer page to browse them. Everything is written to your CryoCloud
scratch folder; --publish copies the result to a public bucket so anyone can open the viewer.

The outline is needed on a site's first run only: later runs reuse it, and refuse a different one."""


def add_common(parser, publishing=True):
    parser.add_argument("--scratch", help="folder holding each site's folders, on S3 or local (default: s3://nasa-cryo-scratch/$JUPYTERHUB_USER/landsat)")
    if publishing:
        parser.add_argument(
            "--publish-to",
            default=os.environ.get("LANDSAT_RECORD_PUBLISH_TO", DEFAULT_PUBLISH_TO),
            help=f"public gs:// folder; each site goes in <folder>/<site> (default: $LANDSAT_RECORD_PUBLISH_TO or {DEFAULT_PUBLISH_TO})",
        )
        parser.add_argument("--key-file", help="service-account key to publish with (default: the one stored by login)")
        parser.add_argument("--skip-key-check", action="store_true", help="don't check the key's permissions on the bucket first")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="landsat-record",
        description="Build a browsable record of every Landsat scene over an Antarctic site.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser(
        "run", help="build or resume a site's record and viewer", description=RUN_DESCRIPTION,
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument("site", help="short name for the site's folders, e.g. fossil_bluff")
    outline = run.add_argument_group("outline (first run only)").add_mutually_exclusive_group()
    outline.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), help="box in lon/lat degrees, south of 60°S; W > E crosses 180°")
    outline.add_argument("--bbox-3031", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"), help="box in EPSG:3031 metres")
    outline.add_argument("--geojson", metavar="FILE", help="one polygon in lon/lat, e.g. drawn at geojson.io")
    run.add_argument("--title", help="name the viewer shows (default: the stored title, or the site name)")
    run.add_argument("--test", action="store_true", help="about a dozen scenes covering every sensor, in <site>_test folders, then a projection of the full run")
    run.add_argument("--dry-run", action="store_true", help="search the catalogue and show what would happen, writing nothing")
    run.add_argument("--publish", action="store_true", help="copy the result to the public bucket at the end")
    run.add_argument("--from", dest="first", choices=STAGES, default=STAGES[0], help="first stage to run (default: search)")
    run.add_argument("--to", dest="last", choices=STAGES, default=STAGES[-1], help="last stage to run (default: publish if --publish, else browse)")
    run.add_argument("--workers", type=int, default=16, help="threads (default: 16; publishing uses twice this)")
    add_common(run)
    run.set_defaults(func=cmd_run)

    login = commands.add_parser("login", help="store the key used to publish, readable only by you")
    login.add_argument("key_file", metavar="KEYFILE", help="Google service-account key (JSON) with write access to the bucket")
    login.add_argument("--remove-original", action="store_true", help="delete KEYFILE once it's stored")
    login.add_argument(
        "--publish-to",
        default=os.environ.get("LANDSAT_RECORD_PUBLISH_TO", DEFAULT_PUBLISH_TO),
        help="bucket folder to check the key against (default: %(default)s)",
    )
    login.add_argument("--skip-key-check", action="store_true", help="don't check the key's permissions on the bucket")
    login.set_defaults(func=cmd_login)

    logout = commands.add_parser("logout", help="delete the stored key")
    logout.set_defaults(func=cmd_logout)

    viewer = commands.add_parser("update-viewer", help="replace only a published site's viewer page with this version's")
    viewer.add_argument("site", help="site name")
    viewer.add_argument("--test", action="store_true", help="the <site>_test copy")
    add_common(viewer)
    viewer.set_defaults(func=cmd_update_viewer)
    return parser


def choose_stages(args):
    first, last = STAGES.index(args.first), STAGES.index(args.last)
    if first > last:
        raise SiteError(f"--from {args.first} comes after --to {args.last}")
    stages = STAGES[first:last + 1]
    if "publish" in stages and not args.publish:
        if stages == ["publish"]:
            raise SiteError("--from publish needs --publish")
        stages.remove("publish")
    return stages


def outline_from(args):
    if args.bbox:
        return Site.from_bbox(args.site, *args.bbox)
    if args.bbox_3031:
        return Site.from_bbox_3031(args.site, *args.bbox_3031)
    if args.geojson:
        return Site.from_geojson(args.site, args.geojson)
    return None


def check_publish_to(publish_to):
    if not publish_to.startswith("gs://"):
        raise SiteError(f"--publish-to must be a gs:// folder, not {publish_to}")


def publishing_key(args, dest_root):
    """The key to publish with, after checking it can write to the bucket."""
    key = find_key(args.key_file)
    if key is None:
        raise KeyFileError("no key to publish with. Run `landsat-record login KEYFILE` once, or pass --key-file")
    if not args.skip_key_check:
        bucket = bucket_of(dest_root)
        try:
            missing = missing_permissions(key, bucket)
        except KeyFileError:
            raise
        except Exception as error:
            raise KeyFileError(f"couldn't check what the key can do in gs://{bucket} ({type(error).__name__}: {error}). "
                               "Check the network, or pass --skip-key-check") from None
        if missing:
            raise KeyFileError(f"the key for {client_email(key)} can't publish to gs://{bucket}: it lacks {', '.join(missing)}")
    return key


def cmd_run(args):
    check_name(args.site)
    stages = choose_stages(args)
    check_publish_to(args.publish_to)
    scratch = (args.scratch or default_scratch()).rstrip("/")
    fs = scratch_filesystem(scratch)
    site = resolve_site(args.site, outline_from(args), stored_site(args.site, scratch, fs), args.title)
    record = LandsatRecord(site, test=args.test, scratch=scratch, workers=args.workers, fs=fs)
    dest_root = record.publish_root(args.publish_to)

    key = publishing_key(args, dest_root) if "publish" in stages and not args.dry_run else None
    print(f"Site     {site.describe()}")
    print(f"Scratch  {record.out_root}")
    if key:
        print(f"Publish  {dest_root} as {client_email(key)}")
    elif args.publish:
        print(f"Publish  {dest_root}")
    else:
        print(f"Publish  no (add --publish to copy to {dest_root}, which is public)")
    sys.stdout.flush()

    if args.dry_run:
        record.search()
        print("Dry run: nothing was written.")
        return 0

    url = None
    for number, stage in enumerate(stages, start=1):
        print(f"\n[{number}/{len(stages)}] {STAGE_TITLES[stage]}", flush=True)
        start = time.perf_counter()
        if stage == "search":
            record.search()
        elif stage == "crop":
            record.check_access()
            record.crop()
            if args.test:
                record.project_full_run()
        elif stage == "cogs":
            record.cogs()
        elif stage == "browse":
            record.browse()
        elif stage == "publish":
            url = record.publish(args.publish_to, key_file=key)
        print(f"Done in {tqdm.format_interval(time.perf_counter() - start)}", flush=True)

    if url:
        print(f"\nViewer: {url}")
    elif "browse" in stages:
        print(f"\nNot published. Add --publish (with --from publish to skip the finished stages) to copy it to {dest_root}")
    return 0


def cmd_login(args):
    source = Path(args.key_file).expanduser()
    email = client_email(source)  # checks it's a service-account key
    target = stored_key_path()
    if source.resolve() == target.resolve():
        print(f"The key for {email} is already stored at {target}.")
    else:
        repo = inside_git_repo(source)
        store_key(source)
        print(f"Stored the key for {email} at {target}, readable only by you.")
        if args.remove_original:
            source.unlink()
            print(f"Removed {source}.")
        elif repo:
            print(f"WARNING: {source} is inside the git repo {repo}. Delete it, or re-run with --remove-original, so it can't be committed.")

    if args.skip_key_check:
        return 0
    check_publish_to(args.publish_to)
    bucket = bucket_of(args.publish_to)
    try:
        missing = missing_permissions(target, bucket)
    except KeyFileError:
        raise
    except Exception as error:
        print(f"Couldn't check what it can do in gs://{bucket} ({type(error).__name__}: {error}).")
        return 1
    if missing:
        print(f"WARNING: it can't publish to gs://{bucket}: it lacks {', '.join(missing)}.")
        return 1
    print(f"It can publish to gs://{bucket}.")
    return 0


def cmd_logout(args):
    if remove_stored_key():
        print(f"Deleted {stored_key_path()}.")
    else:
        print("No key was stored.")
    return 0


def cmd_update_viewer(args):
    check_name(args.site)
    check_publish_to(args.publish_to)
    scratch = (args.scratch or default_scratch()).rstrip("/")
    fs = scratch_filesystem(scratch)
    site = stored_site(args.site, scratch, fs)
    if site is None:
        raise SiteError(f"there's no record of {args.site} in {scratch}")
    record = LandsatRecord(site, test=args.test, scratch=scratch, fs=fs)
    key = publishing_key(args, record.publish_root(args.publish_to))
    url = record.update_viewer(args.publish_to, key_file=key)
    print(f"Viewer updated: {url}")
    print("Anyone who opened the old page may keep seeing it for up to an hour, because of the bucket's caching.")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SiteError, KeyFileError, AccessError, RuntimeError, FileNotFoundError, json.JSONDecodeError) as error:
        print(f"landsat-record: error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped. Finished files are kept: re-run the same command to continue.", file=sys.stderr)
        return 130
