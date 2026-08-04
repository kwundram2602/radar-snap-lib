"""Find and fetch SAR scenes from the ASF archive.

The search half of the library mirrors the SNAP half: a YAML config describes
what to look for, validation runs offline, and one call executes it.

    from radar_snap_lib.search import search

    results = search("searches/testgebiet_s1.yaml")
"""

from radar_snap_lib.search.aoi import AOIError, SearchBounds, aoi_to_wkt

__all__ = ["AOIError", "SearchBounds", "aoi_to_wkt"]
