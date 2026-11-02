"""Band names from SNAP's embedded DIMAP header in a GeoTIFF."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
import tifffile

from radar_snap_lib.snap_tiff import SNAP_METADATA_TAG, label_geotiff, snap_bands

pytestmark = pytest.mark.filterwarnings(
    "ignore::rasterio.errors.NotGeoreferencedWarning"
)

BANDS = [("Sigma0_VH_db", "dB", 0.0), ("Sigma0_VV_db", "dB", None)]


def test_snap_bands_reads_names_units_and_nodata(tmp_path: Path, snap_tif):
    path = snap_tif(tmp_path / "s.tif", np.ones((2, 4, 4)), BANDS)
    bands = snap_bands(path)
    assert bands is not None
    assert [(b.name, b.unit, b.no_data) for b in bands] == BANDS


def test_snap_bands_without_a_snap_header(tmp_path: Path):
    path = tmp_path / "plain.tif"
    tifffile.imwrite(path, np.ones((4, 4), np.float32))
    assert snap_bands(path) is None
    assert label_geotiff(path) is None


def test_snap_bands_refuses_a_header_that_does_not_match(tmp_path: Path, snap_tif):
    path = snap_tif(tmp_path / "s.tif", np.ones((4, 4)), BANDS)
    assert snap_bands(path) is None


def test_label_geotiff_sets_descriptions_and_units(tmp_path: Path, snap_tif):
    path = snap_tif(tmp_path / "s.tif", np.ones((2, 4, 4)), BANDS)
    assert label_geotiff(path) == ["Sigma0_VH_db", "Sigma0_VV_db"]
    with rasterio.open(path) as dataset:
        assert dataset.descriptions == ("Sigma0_VH_db", "Sigma0_VV_db")
        assert dataset.units == ("dB", "dB")
    # SNAP must still be able to read its own header
    with tifffile.TiffFile(path) as tif:
        assert SNAP_METADATA_TAG in tif.pages.first.tags


def test_label_geotiff_keeps_an_existing_description(tmp_path: Path, snap_tif):
    path = snap_tif(tmp_path / "s.tif", np.ones((2, 4, 4)), BANDS)
    with rasterio.open(path, "r+") as dataset:
        dataset.set_band_description(1, "mine")
    assert label_geotiff(path) == ["mine", "Sigma0_VV_db"]
