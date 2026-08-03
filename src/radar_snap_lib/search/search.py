"""ASF Vertex search helpers for finding SAR scenes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import asf_search as asf


@dataclass(frozen=True)
class SearchBounds:
    """Bounding box in WGS-84 degrees."""

    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float

    def as_wkt(self) -> str:
        """Return a WKT polygon string for the bounding box."""
        corners = [
            (self.lon_min, self.lat_min),
            (self.lon_max, self.lat_min),
            (self.lon_max, self.lat_max),
            (self.lon_min, self.lat_max),
            (self.lon_min, self.lat_min),
        ]
        coords = ", ".join(f"{lon} {lat}" for lon, lat in corners)
        return f"POLYGON(({coords}))"


def search_scenes(
    bounds: SearchBounds,
    *,
    platform: str = "ALOS",
    start: date | None = None,
    end: date | None = None,
    max_results: int = 250,
    beam_mode: str | None = None,
) -> list[dict[str, Any]]:
    """Search the ASF Vertex catalogue for SAR scenes.

    Args:
        bounds: Geographic bounding box to search within.
        platform: Satellite platform name (e.g. "ALOS", "SENTINEL-1A").
        start: Earliest acquisition date (inclusive).
        end: Latest acquisition date (inclusive).
        max_results: Maximum number of results to return.
        beam_mode: Beam/acquisition mode filter (e.g. "FBS", "FBD", "IW").

    Returns:
        List of scene metadata dicts (ASF result properties).
    """
    kwargs: dict[str, Any] = {
        "intersectsWith": bounds.as_wkt(),
        "platform": [platform],
        "maxResults": max_results,
    }
    if start is not None:
        kwargs["start"] = start.isoformat()
    if end is not None:
        kwargs["end"] = end.isoformat()
    if beam_mode is not None:
        kwargs["beamMode"] = [beam_mode]

    results = asf.search(**kwargs)
    return [r.properties for r in results]


def search_alos_slc(
    bounds: SearchBounds,
    *,
    start: date | None = None,
    end: date | None = None,
    max_results: int = 250,
) -> list[dict[str, Any]]:
    """Convenience wrapper that searches for ALOS PALSAR SLC scenes.

    Args:
        bounds: Geographic bounding box to search within.
        start: Earliest acquisition date (inclusive).
        end: Latest acquisition date (inclusive).
        max_results: Maximum number of results to return.

    Returns:
        List of scene metadata dicts with SLC product type.
    """
    # ALOS PALSAR SLC data uses FBS (Fine Beam Single) or FBD (Fine Beam Dual)
    results = search_scenes(
        bounds,
        platform="ALOS",
        start=start,
        end=end,
        max_results=max_results,
    )
    return [r for r in results if r.get("processingLevel") in {"L1.1", "SLC"}]
