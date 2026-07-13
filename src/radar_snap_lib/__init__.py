"""radar-snap-lib: SAR/radar utilities for ASF search, SLC processing, and DEM creation.

Modules:
    search  — ASF Vertex catalogue search
    slc     — SLC loading and basic processing
    dem     — ALOS AW3D30 DEM tile merging and reprojection
"""

from radar_snap_lib.dem import create_dem_from_alos
from radar_snap_lib.search import search_scenes
from radar_snap_lib.slc import load_slc

__all__ = ["create_dem_from_alos", "load_slc", "search_scenes"]
