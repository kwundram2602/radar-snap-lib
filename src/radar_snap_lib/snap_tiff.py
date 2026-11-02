"""Band names in the GeoTIFFs SNAP writes.

SNAP's GeoTIFF writer stores band names, units and no-data values only in a
DIMAP header it embeds in a private TIFF tag (65000). GDAL -- and with it
rasterio and QGIS -- ignores that tag and shows "Band 1", so a band called
``Sigma0_VV_db`` loses its name the moment the file leaves SNAP. This module
reads the embedded header back and copies names and units into GDAL's own band
descriptions::

    from radar_snap_lib.snap_tiff import label_geotiff, snap_bands

    snap_bands("gamma0.tif")      # [Band(name='Gamma0_VV_db', unit='dB', ...)]
    label_geotiff("gamma0.tif")   # gdalinfo / QGIS now show Gamma0_VV_db
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from radar_snap_lib.despeckle.dimap import Band, parse_bands

__all__ = ["SNAP_METADATA_TAG", "label_geotiff", "snap_bands"]

#: TIFF tag holding SNAP's embedded DIMAP header (``BEAM_METADATA``).
SNAP_METADATA_TAG = 65000


def _header(path: Path) -> ET.Element | None:
    import tifffile  # noqa: PLC0415

    try:
        with tifffile.TiffFile(path) as tif:
            tag = tif.pages.first.tags.get(SNAP_METADATA_TAG)
            value = tag.value if tag is not None else None
    except (OSError, tifffile.TiffFileError):
        return None
    if not value:
        return None
    try:
        return ET.fromstring(value)
    except ET.ParseError:
        return None


def snap_bands(path: Path | str, count: int | None = None) -> list[Band] | None:
    """The bands SNAP's embedded header declares, one per band of the file.

    Args:
        path: A GeoTIFF.
        count: The file's number of bands, if already known; read otherwise.

    Returns:
        The header's bands in file order, or ``None`` for a TIFF without a SNAP
        header or one whose header does not match the file's bands.
    """
    path = Path(path)
    root = _header(path)
    if root is None:
        return None
    if count is None:
        import rasterio  # noqa: PLC0415

        with rasterio.open(path) as dataset:
            count = dataset.count
    bands = sorted(parse_bands(root), key=lambda b: b.index)
    if len(bands) == count:
        return bands
    # in case the writer left out virtual bands but kept them in the header
    stored = [b for b in bands if not b.virtual]
    return stored if len(stored) == count else None


def label_geotiff(path: Path | str) -> list[str] | None:
    """Copy band names and units from SNAP's embedded header into GDAL's.

    Bands that already have a GDAL description keep it. The file is updated in
    place; its SNAP header stays, so SNAP still reads it as before.

    Returns:
        The band descriptions afterwards, or ``None`` if the file has no SNAP
        header (and was left untouched).
    """
    import rasterio  # noqa: PLC0415

    path = Path(path)
    with rasterio.open(path) as dataset:
        count = dataset.count
        descriptions = list(dataset.descriptions)
        units = list(dataset.units)
    bands = snap_bands(path, count)
    if bands is None:
        return None
    todo = [i for i, band in enumerate(bands) if not descriptions[i] and band.name]
    if todo:
        with rasterio.open(path, "r+") as dataset:
            for i in todo:
                dataset.set_band_description(i + 1, bands[i].name)
                descriptions[i] = bands[i].name
                if not units[i] and bands[i].unit:
                    dataset.set_band_unit(i + 1, bands[i].unit)
    return [d or "" for d in descriptions]
