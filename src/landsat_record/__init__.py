"""Crop the complete Landsat Collection 2 Level 1 record to an Antarctic site, and build a viewer to browse it.

    from landsat_record import LandsatRecord, Site

    site = Site.from_bbox("fossil_bluff", -68.6, -71.5, -67.9, -71.2, title="Fossil Bluff")
    record = LandsatRecord(site, test=True)
    record.crop(); record.cogs(); record.browse()

Or from a terminal: landsat-record run fossil_bluff --bbox -68.6 -71.5 -67.9 -71.2 --test

Once a site is published, update_site("fossil_bluff") adds the scenes released since, working from the published site.
"""
from .record import LandsatRecord, stored_site
from .site import Site, SiteError
from .update import update_site

__version__ = "0.2.1"
__all__ = ["LandsatRecord", "Site", "SiteError", "stored_site", "update_site", "__version__"]
