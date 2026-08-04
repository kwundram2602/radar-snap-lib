"""Turn an area of interest into WKT that ASF will accept.

Four kinds of source are supported: a vector file (GeoPackage, Shapefile,
GeoJSON -- anything GDAL reads), a :class:`SearchBounds`, a four-number
sequence, and a raw WKT string.  All of them end up in ``asf.validate_wkt``,
which repairs and simplifies geometry until the ASF API will take it -- ASF
rejects overly complex outlines, and this is where that gets resolved.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asf_search as asf
from shapely.geometry.base import BaseGeometry

__all__ = ["AOIError", "SearchBounds", "aoi_to_wkt"]

_LOG = logging.getLogger(__name__)

#: Leading tokens that mark a string as WKT rather than a file path.
_WKT_PREFIXES = (
    "POINT",
    "LINESTRING",
    "POLYGON",
    "MULTIPOINT",
    "MULTILINESTRING",
    "MULTIPOLYGON",
    "GEOMETRYCOLLECTION",
)


class AOIError(ValueError):
    """Raised when an AOI source cannot be turned into search geometry."""


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


def aoi_to_wkt(source: str | Path | Sequence[float] | SearchBounds) -> str:
    """Return ASF-ready WKT for ``source``.

    Args:
        source: A vector file path, a :class:`SearchBounds`, a
            ``[lon_min, lat_min, lon_max, lat_max]`` sequence, or a WKT string.

    Returns:
        WKT in EPSG:4326, wrapped at the antimeridian and simplified as far as
        ASF requires.

    Raises:
        AOIError: If the source cannot be read or is not valid geometry.
    """
    geometry = _to_geometry(source)
    try:
        wrapped, _unwrapped, repairs = asf.validate_wkt(geometry)
    except Exception as exc:  # asf raises ASFWKTError and shapely errors alike
        raise AOIError(f"Invalid AOI geometry: {exc}") from exc

    for repair in repairs:
        _LOG.warning("AOI adjusted for ASF: %s", repair)
    return str(wrapped.wkt)


def _to_geometry(source: Any) -> BaseGeometry | str:
    """Normalise any supported source to geometry or a WKT string."""
    if isinstance(source, SearchBounds):
        return source.as_wkt()
    if isinstance(source, (str, Path)):
        if isinstance(source, str) and _looks_like_wkt(source):
            return source
        return _read_vector(Path(source))
    if isinstance(source, Sequence):
        return _from_sequence(source)
    raise AOIError(
        f"Unsupported AOI source of type {type(source).__name__}. Give a vector "
        "file path, a SearchBounds, four numbers, or a WKT string."
    )


def _looks_like_wkt(source: str) -> bool:
    return source.lstrip().upper().startswith(_WKT_PREFIXES)


def _from_sequence(source: Sequence[Any]) -> str:
    values = list(source)
    if len(values) != 4:
        raise AOIError(
            f"A bounding-box AOI needs four numbers "
            f"(lon_min, lat_min, lon_max, lat_max), got {len(values)}."
        )
    try:
        return SearchBounds(*(float(value) for value in values)).as_wkt()
    except (TypeError, ValueError) as exc:
        raise AOIError(f"Bounding-box AOI must be numeric: {exc}") from exc


def _read_vector(path: Path) -> BaseGeometry:
    """Read every feature from a vector file as one WGS-84 geometry."""
    import json

    import geopandas as gpd  # noqa: PLC0415 -- keeps import cost off the CLI

    if not path.is_file():
        raise AOIError(f"AOI file not found: {path}")

    # Check for missing CRS in GeoJSON before geopandas infers it
    if path.suffix.lower() == ".geojson":
        try:
            with open(path) as f:
                geojson_data = json.load(f)
                if "crs" not in geojson_data:
                    raise AOIError(
                        f"AOI file has no CRS: {path}. Assign one "
                        "(EPSG:4326 is expected) and try again."
                    )
        except (OSError, json.JSONDecodeError, KeyError):
            pass  # Will be caught by gpd.read_file

    try:
        frame = gpd.read_file(path)
    except Exception as exc:
        raise AOIError(f"Cannot read AOI file {path}: {exc}") from exc

    if frame.empty:
        raise AOIError(f"AOI file has no features: {path}")
    if frame.crs is None:
        raise AOIError(
            f"AOI file has no CRS: {path}. Assign one (EPSG:4326 is expected) "
            "and try again."
        )
    return frame.to_crs(4326).union_all()
