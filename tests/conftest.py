"""Shared fixtures.

Most tests run against the committed ``operators.json``, which needs no JVM.
Tests marked ``snap`` need a real SNAP install and are skipped without one.
"""

from __future__ import annotations

import pytest

from radar_snap_lib.snap_ops.registry import load_registry


def _snap_available() -> bool:
    try:
        from radar_snap_lib.config import snappy_site_packages

        snappy_site_packages()
    except Exception:
        return False
    return True


SNAP_AVAILABLE = _snap_available()


def pytest_runtest_setup(item: pytest.Item) -> None:
    if list(item.iter_markers(name="snap")) and not SNAP_AVAILABLE:
        pytest.skip("no esa_snappy environment configured")


@pytest.fixture(scope="session")
def registry():
    """The committed operator registry."""
    return load_registry()


def _dimap_header(bands: list[tuple[str, str | None, float | None]]) -> str:
    """A minimal DIMAP header declaring ``(name, unit, no_data)`` bands."""
    infos = []
    for index, (name, unit, no_data) in enumerate(bands):
        infos.append(
            "<Spectral_Band_Info>"
            f"<BAND_INDEX>{index}</BAND_INDEX>"
            f"<BAND_NAME>{name}</BAND_NAME>"
            "<DATA_TYPE>float32</DATA_TYPE>"
            + (f"<PHYSICAL_UNIT>{unit}</PHYSICAL_UNIT>" if unit else "")
            + (
                "<NO_DATA_VALUE_USED>true</NO_DATA_VALUE_USED>"
                f"<NO_DATA_VALUE>{no_data}</NO_DATA_VALUE>"
                if no_data is not None
                else "<NO_DATA_VALUE_USED>false</NO_DATA_VALUE_USED>"
            )
            + "</Spectral_Band_Info>"
        )
    return (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<Dimap_Document name="test.dim">'
        f"<Raster_Dimensions><NBANDS>{len(bands)}</NBANDS></Raster_Dimensions>"
        f"<Image_Interpretation>{''.join(infos)}</Image_Interpretation>"
        "</Dimap_Document>"
    )


@pytest.fixture
def snap_tif():
    """Writes a GeoTIFF the way SNAP does: band names only in tag 65000.

    ``snap_tif(path, data, bands)`` with ``data`` of shape ``(bands, h, w)`` or
    ``(h, w)`` and ``bands`` a list of ``(name, unit, no_data)``.
    """
    import numpy as np
    import tifffile

    from radar_snap_lib.snap_tiff import SNAP_METADATA_TAG

    def write(path, data, bands):
        data = np.asarray(data, np.float32)
        data = data[np.newaxis] if data.ndim == 2 else data
        tifffile.imwrite(
            path,
            data,
            photometric="minisblack",
            planarconfig="separate",
            extratags=[(SNAP_METADATA_TAG, "s", 0, _dimap_header(bands), True)],
        )
        return path

    return write
