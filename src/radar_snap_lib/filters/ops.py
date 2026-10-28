"""Image filters on one raster band, with scikit-image.

Every op has the signature ``op(band, valid, **params) -> ndarray``: ``band`` is a
2-D array, ``valid`` a boolean mask of the pixels that carry data. Invalid pixels
never influence a valid one; what an op leaves in them is undefined, the writer
(:mod:`radar_snap_lib.filters.raster`) overwrites them with nodata.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
from skimage import feature, morphology

__all__ = [
    "FOOTPRINTS",
    "OPS",
    "canny",
    "closing",
    "dilation",
    "erosion",
    "footprint",
    "opening",
]

FOOTPRINTS = ("disk", "square", "diamond")


def footprint(shape: str = "disk", radius: int = 1) -> np.ndarray:
    """Structuring element of ``shape`` reaching ``radius`` pixels from its centre."""
    if shape == "disk":
        return morphology.disk(radius)
    if shape == "square":
        return morphology.footprint_rectangle((2 * radius + 1, 2 * radius + 1))
    if shape == "diamond":
        return morphology.diamond(radius)
    raise ValueError(f"unknown footprint {shape!r}, expected one of {FOOTPRINTS}")


def canny(
    band: np.ndarray,
    valid: np.ndarray,
    *,
    sigma: float = 1.0,
    low_threshold: float | None = None,
    high_threshold: float | None = None,
    use_quantiles: bool = False,
) -> np.ndarray:
    """Canny edges as ``uint8`` 0/1."""
    # canny smooths with a mask-normalised Gaussian, but a NaN would still
    # poison every product it enters
    image = np.where(valid, band, 0).astype(np.float64)
    edges = feature.canny(
        image,
        sigma=sigma,
        low_threshold=low_threshold,
        high_threshold=high_threshold,
        mask=valid,
        use_quantiles=use_quantiles,
    )
    return edges.astype(np.uint8)


def _extreme(dtype: np.dtype, *, high: bool) -> Any:
    """The value that never wins a minimum (``high``) or a maximum filter."""
    if dtype == np.bool_:
        return high
    if np.issubdtype(dtype, np.floating):
        return np.inf if high else -np.inf
    info = np.iinfo(dtype)
    return info.max if high else info.min


def _min_filter(band: np.ndarray, valid: np.ndarray, fp: np.ndarray) -> np.ndarray:
    filled = np.where(valid, band, _extreme(band.dtype, high=True))
    return morphology.erosion(filled.astype(band.dtype), fp)


def _max_filter(band: np.ndarray, valid: np.ndarray, fp: np.ndarray) -> np.ndarray:
    filled = np.where(valid, band, _extreme(band.dtype, high=False))
    return morphology.dilation(filled.astype(band.dtype), fp)


def erosion(
    band: np.ndarray, valid: np.ndarray, *, footprint: str = "disk", radius: int = 1
) -> np.ndarray:
    """Grey-value erosion (minimum filter); on 0/1 input a binary erosion."""
    return _min_filter(band, valid, _footprint(footprint, radius))


def dilation(
    band: np.ndarray, valid: np.ndarray, *, footprint: str = "disk", radius: int = 1
) -> np.ndarray:
    """Grey-value dilation (maximum filter); on 0/1 input a binary dilation."""
    return _max_filter(band, valid, _footprint(footprint, radius))


def opening(
    band: np.ndarray, valid: np.ndarray, *, footprint: str = "disk", radius: int = 1
) -> np.ndarray:
    """Erosion, then dilation: removes bright details smaller than the footprint."""
    fp = _footprint(footprint, radius)
    return _max_filter(_min_filter(band, valid, fp), valid, fp)


def closing(
    band: np.ndarray, valid: np.ndarray, *, footprint: str = "disk", radius: int = 1
) -> np.ndarray:
    """Dilation, then erosion: fills dark gaps smaller than the footprint."""
    fp = _footprint(footprint, radius)
    return _min_filter(_max_filter(band, valid, fp), valid, fp)


# the ops take a parameter called ``footprint``, which shadows the function
_footprint = footprint

#: Config alias -> op.
OPS: dict[str, Callable[..., np.ndarray]] = {
    "Canny": canny,
    "Erosion": erosion,
    "Dilation": dilation,
    "Opening": opening,
    "Closing": closing,
}
