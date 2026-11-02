"""A site: a name for its folders, a title for the viewer, and an outline in EPSG:3031.

Antarctica only: every Antarctic Landsat scene is delivered in EPSG:3031, which the cropping relies on.
"""
import json
import math
import re
from dataclasses import dataclass

from pyproj import Transformer

TO_3031 = Transformer.from_crs("EPSG:4326", "EPSG:3031", always_xy=True)
TO_LONLAT = Transformer.from_crs("EPSG:3031", "EPSG:4326", always_xy=True)

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")
PARALLEL_STEP = 0.05  # degrees between points along a lon/lat box's northern and southern edges
EDGE_STEP = 1000.0  # metres between points along an EPSG:3031 box's edges


class SiteError(ValueError):
    """A site name or outline that can't be used."""


def check_name(name):
    if not NAME_PATTERN.fullmatch(name):
        raise SiteError(f"site name {name!r} must be lower-case letters, digits, '_' or '-', e.g. fossil_bluff")
    return name


def default_title(name):
    return name.replace("_", " ").replace("-", " ").title()


def read_outline(path):
    """The one polygon in a lon/lat GeoJSON file, as EPSG:3031 vertices."""
    with open(path) as f:
        geojson = json.load(f)
    if geojson["type"] == "FeatureCollection":
        geometries = [feature["geometry"] for feature in geojson["features"]]
    else:
        geometries = [geojson.get("geometry", geojson)]
    if len(geometries) != 1 or geometries[0]["type"] != "Polygon":
        raise SiteError(f"{path} must hold exactly one polygon, not {[g['type'] for g in geometries]}")
    ring = [point[:2] for point in geometries[0]["coordinates"][0]]
    if not all(-180 <= lon <= 180 and -90 <= lat <= -60 for lon, lat in ring):
        raise SiteError(f"{path} must be in lon/lat (EPSG:4326) and lie south of 60°S. Was it exported in EPSG:3031?")
    return [TO_3031.transform(lon, lat) for lon, lat in ring]


def wrap(lon):
    return (lon + 180) % 360 - 180


def densify(corners, step):
    """Points every `step` along the straight edges between corners, ending back at the first."""
    points = []
    for (x0, y0), (x1, y1) in zip(corners, corners[1:]):
        n = max(1, math.ceil(math.hypot(x1 - x0, y1 - y0) / step))
        points += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n)]
    return points + [corners[-1]]


def crosses_antimeridian(ring):
    return any(abs(lon - prev) > 180 for (lon, _), (prev, _) in zip(ring[1:], ring))


def split_at_antimeridian(ring):
    """A lon/lat ring that crosses 180°, as a MultiPolygon with one part on each side."""
    from shapely.affinity import translate
    from shapely.geometry import MultiPolygon, Polygon, box, mapping

    lons = [ring[0][0]]
    for (lon, _), (prev, _) in zip(ring[1:], ring):
        lons.append(lons[-1] + wrap(lon - prev))
    if abs(lons[-1] - lons[0]) > 1e-6:
        raise SiteError("outlines that go around the South Pole aren't supported")
    polygon = Polygon(zip(lons, [lat for _, lat in ring]))

    parts = []
    for turns in (-1, 0, 1):
        piece = polygon.intersection(box(-180 + 360 * turns, -90, 180 + 360 * turns, 90))
        if not piece.is_empty:
            piece = translate(piece, xoff=-360 * turns)
            parts += list(getattr(piece, "geoms", [piece]))
    return mapping(MultiPolygon([part for part in parts if part.geom_type == "Polygon"]))


@dataclass(frozen=True)
class Site:
    """A site to build a Landsat record for. Make one with from_bbox, from_bbox_3031 or from_geojson."""

    name: str
    title: str
    vertices: tuple  # closed ring of (x, y) in EPSG:3031
    source: str = ""  # how the outline was given, for messages

    def __post_init__(self):
        check_name(self.name)
        object.__setattr__(self, "vertices", tuple((float(x), float(y)) for x, y in self.vertices))
        if len(self.vertices) < 4:
            raise SiteError("an outline needs at least three corners")

    @classmethod
    def from_geojson(cls, name, path, title=None):
        """One polygon in a lon/lat GeoJSON file (a Feature, a FeatureCollection or a bare geometry)."""
        return cls(name, title or default_title(name), read_outline(path), f"GeoJSON {path}")

    @classmethod
    def from_bbox(cls, name, west, south, east, north, title=None):
        """A box in lon/lat degrees. If west > east, it crosses 180°.

        Its northern and southern edges follow their parallels, which are arcs in EPSG:3031, so they are densified
        before projecting. Its other edges are meridians, which are straight lines in EPSG:3031.
        """
        values = (west, south, east, north)
        if not (all(-180 <= v <= 180 for v in (west, east)) and all(-90 <= v <= -60 for v in (south, north))):
            raise SiteError(
                f"a lon/lat box is W S E N in degrees, south of 60°S, not {values}. "
                "For EPSG:3031 metres use --bbox-3031 (Site.from_bbox_3031)."
            )
        if south >= north:
            raise SiteError(f"south ({south}) must be less than north ({north})")
        if west == east:
            raise SiteError("west and east must differ")
        span = (east - west) % 360
        n = max(1, math.ceil(span / PARALLEL_STEP))
        lons = [west + span * i / n for i in range(n + 1)]
        ring = [(lon, south) for lon in lons] + [(lon, north) for lon in reversed(lons)]
        ring.append(ring[0])
        vertices = [TO_3031.transform(wrap(lon), lat) for lon, lat in ring]
        return cls(name, title or default_title(name), vertices, "bbox " + " ".join(f"{v:g}" for v in values))

    @classmethod
    def from_bbox_3031(cls, name, xmin, ymin, xmax, ymax, title=None):
        """A box in EPSG:3031 metres. Its edges are densified only so that the lon/lat search polygon follows them."""
        values = (xmin, ymin, xmax, ymax)
        if all(abs(v) <= 180 for v in values):
            raise SiteError(f"{values} look like degrees. For lon/lat use --bbox (Site.from_bbox).")
        if xmin >= xmax or ymin >= ymax:
            raise SiteError(f"an EPSG:3031 box is XMIN YMIN XMAX YMAX, with min < max, not {values}")
        corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)]
        if max(TO_LONLAT.transform(x, y)[1] for x, y in corners) > -60:
            raise SiteError(f"the EPSG:3031 box {values} must lie south of 60°S")
        return cls(name, title or default_title(name), densify(corners, EDGE_STEP), "bbox-3031 " + " ".join(f"{v:g}" for v in values))

    @classmethod
    def from_json(cls, data):
        return cls(data["name"], data["title"], data["vertices"], data.get("source", ""))

    def to_json(self):
        return {"name": self.name, "title": self.title, "crs": "EPSG:3031", "vertices": [list(v) for v in self.vertices], "source": self.source}

    @property
    def bounds(self):
        xs, ys = zip(*self.vertices)
        return (min(xs), min(ys), max(xs), max(ys))

    def same_outline(self, other, tolerance=0.01):
        """Crops depend only on the outline's bounds, so two outlines with the same bounds give the same record."""
        return all(abs(a - b) <= tolerance for a, b in zip(self.bounds, other.bounds))

    def describe(self):
        x0, y0, x1, y1 = self.bounds
        return f'{self.name} "{self.title}", {(x1 - x0) / 1000:.0f} × {(y1 - y0) / 1000:.0f} km ({self.source})'

    def search_geometry(self):
        """The outline in lon/lat for the STAC search, split in two if it crosses 180°."""
        ring = [list(TO_LONLAT.transform(x, y)) for x, y in self.vertices]
        if crosses_antimeridian(ring):
            return split_at_antimeridian(ring)
        return {"type": "Polygon", "coordinates": [ring]}
