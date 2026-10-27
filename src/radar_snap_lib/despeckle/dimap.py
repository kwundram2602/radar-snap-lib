"""Minimal BEAM-DIMAP access without the JVM.

A DIMAP product is a ``.dim`` XML header plus a ``.data`` folder holding one raw
ENVI image per band (``<band>.img`` with its ``.hdr``): big-endian,
band-sequential, sized by the header. That is all a Python step between two
SNAP graphs needs -- read a band as a memory map, write a new one, rewire the
header. Everything else in the header (abstracted metadata, tie-point grids,
geocoding) is carried over untouched, so SNAP reads the result as if it had
written it itself.
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["Band", "DimapProduct", "copy_product", "remove_product"]

#: DIMAP ``DATA_TYPE`` -> numpy dtype of the raw ``.img`` (always big-endian).
_DTYPES = {
    "int8": "i1",
    "uint8": "u1",
    "int16": ">i2",
    "uint16": ">u2",
    "int32": ">i4",
    "uint32": ">u4",
    "float32": ">f4",
    "float64": ">f8",
}
#: DIMAP ``DATA_TYPE`` -> ENVI ``data type`` code, as SNAP writes it.
_ENVI_TYPES = {
    "int8": 1,
    "uint8": 1,
    "int16": 2,
    "uint16": 12,
    "int32": 3,
    "uint32": 13,
    "float32": 4,
    "float64": 5,
}


@dataclass
class Band:
    """One ``Spectral_Band_Info`` entry of a DIMAP header."""

    name: str
    index: int
    data_type: str
    unit: str | None
    #: Band-maths expression of a virtual band, ``None`` for a band with data.
    expression: str | None
    info: ET.Element

    @property
    def virtual(self) -> bool:
        return self.expression is not None


def _text(parent: ET.Element, tag: str) -> str | None:
    element = parent.find(tag)
    return element.text if element is not None else None


def _set_text(parent: ET.Element, tag: str, value: str) -> None:
    element = parent.find(tag)
    if element is None:
        element = ET.SubElement(parent, tag)
    element.text = value


def _data_dir(dim: Path) -> Path:
    return dim.with_suffix(".data")


def remove_product(dim: Path) -> None:
    """Delete a DIMAP product: its ``.dim`` and its ``.data`` folder."""
    dim.unlink(missing_ok=True)
    shutil.rmtree(_data_dir(dim), ignore_errors=True)


class DimapProduct:
    """A DIMAP product on disk, its header parsed and editable."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if self.path.suffix != ".dim" or not self.path.is_file():
            raise FileNotFoundError(f"Not a BEAM-DIMAP product: {self.path}")
        self.tree = ET.parse(self.path)
        self.root = self.tree.getroot()

        dims = self.root.find("Raster_Dimensions")
        if dims is None:
            raise ValueError(f"{self.path}: no Raster_Dimensions")
        self.width = int(_text(dims, "NCOLS") or 0)
        self.height = int(_text(dims, "NROWS") or 0)

    # -- reading ----------------------------------------------------------- #

    @property
    def data_dir(self) -> Path:
        return _data_dir(self.path)

    @property
    def bands(self) -> list[Band]:
        bands = []
        for info in self.root.iterfind("Image_Interpretation/Spectral_Band_Info"):
            bands.append(
                Band(
                    name=_text(info, "BAND_NAME") or "",
                    index=int(_text(info, "BAND_INDEX") or -1),
                    data_type=_text(info, "DATA_TYPE") or "float32",
                    unit=_text(info, "PHYSICAL_UNIT"),
                    expression=_text(info, "EXPRESSION")
                    if _text(info, "VIRTUAL_BAND") == "true"
                    else None,
                    info=info,
                )
            )
        return bands

    def band(self, name: str) -> Band:
        for band in self.bands:
            if band.name == name:
                return band
        names = ", ".join(b.name for b in self.bands)
        raise KeyError(f"{self.path.name} has no band {name!r} (bands: {names})")

    def _data_file(self, band: Band) -> ET.Element:
        for data_file in self.root.iterfind("Data_Access/Data_File"):
            if _text(data_file, "BAND_INDEX") == str(band.index):
                return data_file
        raise KeyError(f"{self.path.name}: band {band.name!r} has no data file")

    def band_file(self, name: str) -> Path:
        """The raw ``.img`` holding a band's data."""
        path = self._data_file(self.band(name)).find("DATA_FILE_PATH")
        assert path is not None
        return (self.path.parent / path.attrib["href"]).with_suffix(".img")

    def read_band(self, name: str) -> np.memmap:
        """A band's data as a read-only ``(height, width)`` memory map."""
        band = self.band(name)
        if band.virtual:
            raise ValueError(f"Band {name!r} is virtual ({band.expression})")
        return np.memmap(
            self.band_file(name),
            dtype=np.dtype(_DTYPES[band.data_type]),
            mode="r",
            shape=(self.height, self.width),
        )

    # -- editing ----------------------------------------------------------- #

    def create_band_file(self, name: str, unit: str) -> np.memmap:
        """A new float32 band file in ``data_dir``, as a writable memory map.

        Only the data is written; :meth:`retarget_band` points a header entry
        at it.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        img = self.data_dir / f"{name}.img"
        img.with_suffix(".hdr").write_text(
            "\n".join(
                [
                    "ENVI",
                    f"description = {{{name} - Unit: {unit}}}",
                    f"samples = {self.width}",
                    f"lines = {self.height}",
                    "bands = 1",
                    "header offset = 0",
                    "file type = ENVI Standard",
                    f"data type = {_ENVI_TYPES['float32']}",
                    "interleave = bsq",
                    "byte order = 1",
                    f"band names = {{ {name} }}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return np.memmap(
            img,
            dtype=np.dtype(_DTYPES["float32"]),
            mode="w+",
            shape=(self.height, self.width),
        )

    def retarget_band(self, old: str, new: str, unit: str) -> None:
        """Point band ``old``'s header entry at the float32 file of band ``new``.

        The entry keeps its position and everything else it declares (no-data
        value, raster size, ...); its name, unit and data type change.
        """
        band = self.band(old)
        data_file = self._data_file(band)
        _set_text(band.info, "BAND_NAME", new)
        _set_text(band.info, "PHYSICAL_UNIT", unit)
        _set_text(band.info, "DATA_TYPE", "float32")
        path = data_file.find("DATA_FILE_PATH")
        assert path is not None
        path.attrib["href"] = f"{self.data_dir.name}/{new}.hdr"
        if old != new:
            self._rename_in_metadata({old: new})

    def remove_band(self, name: str) -> None:
        """Drop a band from the header, and its files if it has any."""
        band = self.band(name)
        if not band.virtual:
            img = self.band_file(name)
            img.unlink(missing_ok=True)
            img.with_suffix(".hdr").unlink(missing_ok=True)
            access = self.root.find("Data_Access")
            assert access is not None
            access.remove(self._data_file(band))
        interpretation = self.root.find("Image_Interpretation")
        assert interpretation is not None
        interpretation.remove(band.info)
        self._rename_in_metadata({name: None})

    def set_expression(self, name: str, expression: str) -> None:
        """Change the expression of a virtual band."""
        _set_text(self.band(name).info, "EXPRESSION", expression)

    def set_metadata(self, attribute: str, value: str) -> None:
        """Set every metadata attribute called ``attribute``, e.g. ``sample_type``."""
        for element in self.root.iter("MDATTR"):
            if element.get("name") == attribute:
                element.text = value

    def _rename_in_metadata(self, renames: dict[str, str | None]) -> None:
        """Follow renamed (or, for ``None``, removed) bands in ``band_names``.

        SAR products list their bands per swath and polarisation in the
        abstracted metadata, which operators like Calibration read.
        """
        for element in self.root.iter("MDATTR"):
            if element.get("name") != "band_names" or not element.text:
                continue
            names = []
            for token in element.text.split():
                if token not in renames:
                    names.append(token)
                elif (renamed := renames[token]) is not None:
                    names.append(renamed)
            element.text = " ".join(names)

    def save(self) -> None:
        """Renumber the bands and write the header back."""
        index_map: dict[str, str] = {}
        bands = self.bands
        for new_index, band in enumerate(bands):
            index_map[str(band.index)] = str(new_index)
        for data_file in self.root.iterfind("Data_Access/Data_File"):
            old = _text(data_file, "BAND_INDEX")
            if old in index_map:
                _set_text(data_file, "BAND_INDEX", index_map[old])
        for band in bands:
            _set_text(band.info, "BAND_INDEX", index_map[str(band.index)])

        dims = self.root.find("Raster_Dimensions")
        assert dims is not None
        _set_text(dims, "NBANDS", str(len(bands)))

        ET.indent(self.tree, space="    ")
        self.tree.write(self.path, encoding="ISO-8859-1", xml_declaration=True)


def copy_product(
    src: Path | str, dst: Path | str, *, skip_bands: tuple[str, ...] = ()
) -> DimapProduct:
    """Copy a DIMAP product, leaving out the data files of ``skip_bands``.

    The skipped bands stay in the copied header; the caller is about to
    retarget or remove them. An existing ``dst`` is replaced.
    """
    source = DimapProduct(src)
    dst = Path(dst)
    if dst.resolve() == source.path.resolve():
        raise ValueError(f"Cannot copy a product onto itself: {dst}")
    remove_product(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    skipped = {
        source.band_file(name).name
        for name in skip_bands
        if not source.band(name).virtual
    }
    skipped |= {Path(name).with_suffix(".hdr").name for name in skipped}
    shutil.copytree(
        source.data_dir,
        _data_dir(dst),
        ignore=lambda folder, names: [
            n for n in names if Path(folder) == source.data_dir and n in skipped
        ],
    )

    old_prefix = f"{source.data_dir.name}/"
    new_prefix = f"{_data_dir(dst).name}/"
    for element in source.root.iter():
        href = element.get("href")
        if href is not None and href.startswith(old_prefix):
            element.set("href", new_prefix + href.removeprefix(old_prefix))
    source.root.set("name", dst.name)
    source.tree.write(dst, encoding="ISO-8859-1", xml_declaration=True)
    return DimapProduct(dst)
