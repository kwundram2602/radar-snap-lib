"""SLC (Single Look Complex) data loading and basic processing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
import rasterio
from rasterio.windows import Window


@dataclass
class SlcData:
    """Container for a single SLC band."""

    data: npt.NDArray[np.complex64]
    transform: rasterio.transform.Affine
    crs: rasterio.crs.CRS
    source_path: Path


def load_slc(
    path: Path,
    *,
    band: int = 1,
    window: Window | None = None,
) -> SlcData:
    """Load a SLC raster into a complex numpy array.

    Expects the file to contain complex (real + imaginary) data.  Many SAR
    formats store I/Q as two separate bands; use ``load_slc_iq`` for those.

    Args:
        path: Path to the SLC file (GeoTIFF, ENVI, or similar).
        band: Band index to read (1-based).
        window: Optional rasterio Window for spatial sub-setting.

    Returns:
        SlcData with the raw complex array, affine transform, and CRS.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the band dtype is not complex.
    """
    if not path.exists():
        raise FileNotFoundError(f"SLC file not found: {path}")

    with rasterio.open(path) as src:
        raw = src.read(band, window=window)
        transform = src.window_transform(window) if window else src.transform
        crs = src.crs

    if not np.issubdtype(raw.dtype, np.complexfloating):
        raise ValueError(
            f"Expected complex dtype in band {band}, got {raw.dtype!r}."
            " Use load_slc_iq() for I/Q band pairs."
        )

    return SlcData(
        data=raw.astype(np.complex64),
        transform=transform,
        crs=crs,
        source_path=path,
    )


def load_slc_iq(
    path: Path,
    *,
    i_band: int = 1,
    q_band: int = 2,
    window: Window | None = None,
) -> SlcData:
    """Load an SLC stored as separate I and Q bands and combine into complex.

    Args:
        path: Path to the SLC file with separate I/Q bands.
        i_band: Band index for the in-phase (I) component.
        q_band: Band index for the quadrature (Q) component.
        window: Optional rasterio Window for spatial sub-setting.

    Returns:
        SlcData with a complex64 array constructed from I + jQ.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"SLC file not found: {path}")

    with rasterio.open(path) as src:
        i_data = src.read(i_band, window=window).astype(np.float32)
        q_data = src.read(q_band, window=window).astype(np.float32)
        transform = src.window_transform(window) if window else src.transform
        crs = src.crs

    complex_data = (i_data + 1j * q_data).astype(np.complex64)
    return SlcData(
        data=complex_data,
        transform=transform,
        crs=crs,
        source_path=path,
    )


def compute_intensity(slc: SlcData) -> npt.NDArray[np.float32]:
    """Compute intensity (power) image from SLC: |z|^2.

    Args:
        slc: SLC data container.

    Returns:
        2-D float32 array of intensity values.
    """
    return (slc.data.real**2 + slc.data.imag**2).astype(np.float32)


def compute_amplitude(slc: SlcData) -> npt.NDArray[np.float32]:
    """Compute amplitude image from SLC: |z|.

    Args:
        slc: SLC data container.

    Returns:
        2-D float32 amplitude array.
    """
    return np.abs(slc.data).astype(np.float32)


def multilook(
    slc: SlcData,
    *,
    range_looks: int,
    azimuth_looks: int,
) -> npt.NDArray[np.float32]:
    """Apply multi-looking (spatial averaging) to reduce speckle.

    Averages intensity over ``azimuth_looks`` x ``range_looks`` blocks and
    returns the resulting reduced-resolution intensity image.

    Args:
        slc: SLC data container.
        range_looks: Number of looks in the range (column) direction.
        azimuth_looks: Number of looks in the azimuth (row) direction.

    Returns:
        Multi-looked intensity array with reduced dimensions.
    """
    intensity = compute_intensity(slc)
    rows, cols = intensity.shape
    trimmed_rows = (rows // azimuth_looks) * azimuth_looks
    trimmed_cols = (cols // range_looks) * range_looks
    trimmed = intensity[:trimmed_rows, :trimmed_cols]
    reshaped = trimmed.reshape(
        trimmed_rows // azimuth_looks,
        azimuth_looks,
        trimmed_cols // range_looks,
        range_looks,
    )
    return reshaped.mean(axis=(1, 3)).astype(np.float32)
