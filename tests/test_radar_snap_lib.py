"""Basic tests for radar-snap-lib."""

from __future__ import annotations

import numpy as np

from radar_snap_lib.search import SearchBounds
from radar_snap_lib.slc import SlcData, compute_amplitude, compute_intensity, multilook

# ---------------------------------------------------------------------------
# SearchBounds
# ---------------------------------------------------------------------------

def test_search_bounds_as_wkt_closes_ring() -> None:
    bounds = SearchBounds(lon_min=10.0, lat_min=47.0, lon_max=11.0, lat_max=48.0)
    wkt = bounds.as_wkt()
    assert wkt.startswith("POLYGON((")
    # First and last coordinate pair must be identical (closed ring)
    coords_str = wkt[len("POLYGON(("):-2]
    pairs = [tuple(map(float, p.split())) for p in coords_str.split(",")]
    assert pairs[0] == pairs[-1]


# ---------------------------------------------------------------------------
# SlcData helpers
# ---------------------------------------------------------------------------

def _make_slc(rows: int = 4, cols: int = 8) -> SlcData:
    from pathlib import Path

    import rasterio

    rng = np.random.default_rng(42)
    real = rng.standard_normal((rows, cols))
    imag = rng.standard_normal((rows, cols))
    data = (real + 1j * imag).astype(np.complex64)
    return SlcData(
        data=data,
        transform=rasterio.transform.from_bounds(0, 0, 1, 1, cols, rows),
        crs=rasterio.crs.CRS.from_epsg(4326),
        source_path=Path("dummy.tif"),
    )


def test_compute_intensity_shape_and_dtype() -> None:
    slc = _make_slc()
    intensity = compute_intensity(slc)
    assert intensity.shape == slc.data.shape
    assert intensity.dtype == np.float32


def test_compute_intensity_non_negative() -> None:
    slc = _make_slc()
    assert (compute_intensity(slc) >= 0).all()


def test_compute_amplitude_equals_abs() -> None:
    slc = _make_slc()
    amplitude = compute_amplitude(slc)
    expected = np.abs(slc.data).astype(np.float32)
    np.testing.assert_allclose(amplitude, expected, rtol=1e-6)


def test_multilook_reduces_dimensions() -> None:
    slc = _make_slc(rows=8, cols=16)
    result = multilook(slc, range_looks=4, azimuth_looks=2)
    assert result.shape == (4, 4)
    assert result.dtype == np.float32
