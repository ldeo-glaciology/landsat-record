# landsat-record

Build a browsable record of **every Landsat scene over an Antarctic site** with one command.

`landsat-record` finds every Landsat Collection 2 Level 1 scene (1973 onwards) that covers your site. It then:

- crops each band at native resolution, with no resampling;
- converts the crops to Cloud Optimized GeoTIFFs;
- builds a web page for flicking through the whole record: an RGB view, a band menu, flags, GeoTIFF downloads and links to the full USGS scenes.

It can then publish the page and data to a public bucket, so anyone can open it with no login.

It is made for [CryoCloud](https://cryointhecloud.com), which can read the requester-pays USGS Landsat bucket and
gives you a scratch folder to work in. **Antarctica only:** every Antarctic scene is delivered in EPSG:3031, which the
cropping relies on.

## Install

In a CryoCloud terminal:

```bash
pip install --user git+https://github.com/ldeo-glaciology/landsat-record
```

**If `landsat-record` isn't found afterwards:** `~/.local/bin` isn't on your `PATH`. Either use
`python -m landsat_record` in its place, or run:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

**To update:** add `--upgrade` to the install command.

**Why no Homebrew:** CryoCloud has no root access. Everything this needs, such as rasterio and s3fs, is already in
the CryoCloud image, so pip adds only this package.

## Quick start

```bash
# 1. A test run: about a dozen scenes covering every sensor, in separate _test folders
landsat-record run fossil_bluff --bbox -68.6 -71.5 -67.9 -71.2 --title "Fossil Bluff" --test

# 2. The whole record (the outline is remembered)
landsat-record run fossil_bluff

# 3. Publish it (once you've stored a key, below)
landsat-record run fossil_bluff --from publish --publish
```

Step 2 can also publish in the same go: `landsat-record run fossil_bluff --publish`.

The test run ends with a projection of the full run's time, size and request cost. `--dry-run` searches the
catalogue and shows what would happen, without writing anything.

Each step prints a progress bar:

```
Site     fossil_bluff "Fossil Bluff", 24 × 31 km (bbox -68.6 -71.5 -67.9 -71.2)
Scratch  s3://nasa-cryo-scratch/<you>/landsat/fossil_bluff
Publish  gs://ldeo-glaciology/landsat/fossil_bluff as publisher@….iam.gserviceaccount.com

[2/5] Crop every band to the site
Crop:  59%|█████▉    | 612/1043 scenes [41:10<29:30, 2 with errors]
```

At the end it prints the viewer's address, e.g.
`https://storage.googleapis.com/ldeo-glaciology/landsat/fossil_bluff/index.html`.

## The site outline

You give the outline on a site's first run only. Later runs reuse it. They refuse a different one, because the crops
already made would no longer match it. Use a new site name instead.

| Option | Outline |
|---|---|
| `--bbox W S E N` | A box in lon/lat degrees. Its northern and southern edges follow their parallels. If W > E, the box crosses 180°. |
| `--bbox-3031 XMIN YMIN XMAX YMAX` | A box in polar-stereographic metres (EPSG:3031). |
| `--geojson FILE` | One polygon in lon/lat. Draw it at [geojson.io](https://geojson.io), or export it from QGIS as GeoJSON in EPSG:4326. |

Scenes are cropped to the outline's bounding box. The viewer draws the outline itself. The site name becomes the
folder name, e.g. `fossil_bluff`, so use lower-case letters, digits, `_` or `-`.

`sites/flask_region.geojson` is an example.

## Long runs

A full run over ~1,200 scenes takes hours.

**Stopping and resuming:**
- Every stage is resumable. Stop it with Ctrl-C, or let the server shut down, then run the same command again: finished files are skipped and failed ones are retried.
- `--from` and `--to` run a range of stages: `search`, `crop`, `cogs`, `browse` and `publish`.

**Running in the background.** To keep a run going with no terminal attached:

```bash
nohup landsat-record run fossil_bluff --publish > fossil_bluff.log 2>&1 &
tail -f fossil_bluff.log
```

When the output goes to a file, progress is written as one plain line a minute, rather than a bar.

**Idle shutdown:** CryoCloud may stop your server after a period with no browser activity, so keep a JupyterLab tab
open during long runs.

## Publishing, and keeping the key secret

Your CryoCloud scratch folder isn't public. `--publish` copies the finished site to a public Google Cloud Storage
folder. The default is `gs://ldeo-glaciology/landsat/<site>`.

That needs a Google **service-account key** (a JSON file) with permission to write to the bucket. Store it once:

```bash
landsat-record login ~/ldeo-glaciology-key.json --remove-original
```

**What `login` does:**
- Checks the file is a service-account key.
- Copies it to `~/.config/landsat-record/gcs-key.json`. The file is readable only by you (mode 0600, in a 0700 folder).
- Checks the key can create and replace files in the bucket. This uses Google's read-only permission test, so nothing is written.
- With `--remove-original`, deletes the file you gave it.
- Warns you if the key file sits inside a git repository.

**How the key stays secret:**
- The key's contents are never accepted on the command line, where they would be kept in your shell history.
- The contents are never printed, logged or written anywhere else. Only the service account's email address is shown.
- `landsat-record logout` deletes the stored copy.

**Using a different key or bucket:**
- `--key-file PATH` uses another key for one run. `GOOGLE_APPLICATION_CREDENTIALS` is used if no key is stored.
- To publish somewhere else, pass `--publish-to gs://your-bucket/folder`, or set `LANDSAT_RECORD_PUBLISH_TO`. The bucket must allow public reads for the viewer link to work.

**Before publishing, note:**
- Everything published is readable by anyone. Landsat data is public domain.
- Downloads are billed to the bucket's owner.
- After `landsat-record update-viewer <site>` replaces a published page, people may see the old page for up to an hour, because of the bucket's caching.

## What gets written

```
s3://nasa-cryo-scratch/<you>/landsat/<site>/          cropped GeoTIFFs, as read from USGS
    site.json                    the outline and title
    items.json                   the STAC catalogue entries
    scenes.parquet               one row per catalogue scene, with its status (ok, no_data, error, not_run)
    bands.parquet                one row per band of each scene, with its grid, valid fraction and size
    <scene_id>/<scene_id>_<band>.tif, <scene_id>_MTL.json, <scene_id>_record.json
s3://nasa-cryo-scratch/<you>/landsat/<site>_cog/      what gets published
    <scene_id>/<scene_id>_<band>.tif                  Cloud Optimized GeoTIFFs, pixels identical to the crops
    browse/<band or rgb>/<scene_id>.webp              browse images, stretched per scene
    index.json, index.html                            the viewer's data and page
```

**Things to know about the data:**
- Values are Level 1 DNs. Each scene's `MTL.json` holds the calibration.
- Every Antarctic scene is Tier 2, and L1GS scenes aren't terrain-corrected, so scenes are misaligned to some degree. Co-register them before any change or velocity analysis.
- Browse images and RGB composites are for looking, not measuring. Use the COGs for anything quantitative.
- Reads from USGS cost about $0.0004 per 1,000 requests. The test run's projection estimates the total.
- Scratch isn't permanent: copy anything you need to keep.

## From Python

The same steps, one at a time. As on the command line, a site name keeps its first outline: give a new outline a new
name, or `LandsatRecord` refuses it.

```python
from landsat_record import LandsatRecord, Site

site = Site.from_geojson("flask_region", "sites/flask_region.geojson", title="Flask Glacier")
record = LandsatRecord(site, test=True)
record.search()
record.crop()
record.project_full_run()
record.cogs()
record.browse()
record.scenes()          # pandas tables of what was done
record.bands(cog=True)
record.quicklook()       # needs matplotlib
record.publish()         # returns the viewer's address
```

`examples/landsat_record_examples.ipynb` has more.

## Development

```bash
git clone https://github.com/ldeo-glaciology/landsat-record && cd landsat-record
pip install -e ".[test]"
pytest
```

The tests run offline. They use saved STAC entries, synthetic rasters and local files in place of S3.

The viewer is a single page, `src/landsat_record/viewer.html`, with no build step.

## License

GPL-3.0. See `LICENSE`.
