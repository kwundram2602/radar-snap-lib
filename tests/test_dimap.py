"""BEAM-DIMAP access and despeckle_dimap, on small synthetic products.

Torch-free: the denoiser is replaced by a stand-in that computes the amplitude
of what it is given, so the tests check the band and header handling.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from radar_snap_lib.despeckle import deep
from radar_snap_lib.despeckle.deep import despeckle_dimap
from radar_snap_lib.despeckle.dimap import DimapProduct, copy_product

H, W = 40, 30
_DTYPES = {"int16": ">i2", "uint16": ">u2", "float32": ">f4"}


def make_dimap(
    path: Path,
    bands: list[tuple[str, str, str, np.ndarray | str]],
    *,
    sample_type: str = "COMPLEX",
    band_names: str = "",
    shape: tuple[int, int] = (H, W),
) -> Path:
    """Write a DIMAP product the way SNAP lays it out.

    ``bands`` are ``(name, data type, unit, data or virtual expression)``.
    """
    data_dir = path.with_suffix(".data")
    (data_dir / "tie_point_grids").mkdir(parents=True)
    (data_dir / "tie_point_grids" / "latitude.img").write_bytes(b"\0" * 16)
    (data_dir / "tie_point_grids" / "latitude.hdr").write_text("ENVI\n")

    files, infos = [], []
    for index, (name, dtype, unit, content) in enumerate(bands):
        info = (
            f"<Spectral_Band_Info><BAND_INDEX>{index}</BAND_INDEX>"
            f"<BAND_NAME>{name}</BAND_NAME><DATA_TYPE>{dtype}</DATA_TYPE>"
            f"<PHYSICAL_UNIT>{unit}</PHYSICAL_UNIT>"
            "<NO_DATA_VALUE_USED>true</NO_DATA_VALUE_USED>"
        )
        if isinstance(content, str):
            info += (
                f"<VIRTUAL_BAND>true</VIRTUAL_BAND><EXPRESSION>{content}</EXPRESSION>"
            )
        else:
            content.astype(_DTYPES[dtype]).tofile(data_dir / f"{name}.img")
            (data_dir / f"{name}.hdr").write_text("ENVI\n")
            files.append(
                f'<Data_File><DATA_FILE_PATH href="{data_dir.name}/{name}.hdr" />'
                f"<BAND_INDEX>{index}</BAND_INDEX></Data_File>"
            )
        infos.append(info + "</Spectral_Band_Info>")

    path.write_text(
        f"""<?xml version="1.0" encoding="ISO-8859-1"?>
<Dimap_Document name="{path.name}">
  <Dataset_Id><DATASET_NAME>{path.stem}</DATASET_NAME></Dataset_Id>
  <Raster_Dimensions><NCOLS>{shape[1]}</NCOLS><NROWS>{shape[0]}</NROWS>
    <NBANDS>{len(bands)}</NBANDS></Raster_Dimensions>
  <Data_Access>
    <DATA_FILE_FORMAT>ENVI</DATA_FILE_FORMAT>
    {"".join(files)}
    <Tie_Point_Grid_File>
      <TIE_POINT_GRID_FILE_PATH href="{data_dir.name}/tie_point_grids/latitude.hdr" />
      <TIE_POINT_GRID_INDEX>0</TIE_POINT_GRID_INDEX>
    </Tie_Point_Grid_File>
  </Data_Access>
  <Image_Interpretation>{"".join(infos)}</Image_Interpretation>
  <Dataset_Sources><MDElem name="metadata"><MDElem name="Abstracted_Metadata">
    <MDATTR name="sample_type" type="ascii">{sample_type}</MDATTR>
    <MDElem name="Band_IW2_VV">
      <MDATTR name="band_names" type="ascii">{band_names}</MDATTR>
    </MDElem>
  </MDElem></MDElem></Dataset_Sources>
</Dimap_Document>
""",
        encoding="ISO-8859-1",
    )
    return path


def _random(dtype: str, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(-500, 500, size=(H, W)).astype(dtype)


@pytest.fixture
def slc(tmp_path: Path) -> Path:
    return make_dimap(
        tmp_path / "in" / "slc.dim",
        [
            ("i_IW2_VV", "int16", "real", _random("int16", 1)),
            ("q_IW2_VV", "int16", "imaginary", _random("int16", 2)),
            (
                "Intensity_IW2_VV",
                "float32",
                "intensity",
                "i_IW2_VV * i_IW2_VV + q_IW2_VV * q_IW2_VV",
            ),
            ("i_IW2_VH", "int16", "real", _random("int16", 3)),
            ("q_IW2_VH", "int16", "imaginary", _random("int16", 4)),
        ],
        band_names="i_IW2_VV q_IW2_VV Intensity_IW2_VV",
    )


@pytest.fixture
def grd(tmp_path: Path) -> Path:
    amplitude = np.abs(_random("int16", 5)).astype("uint16") + 1
    return make_dimap(
        tmp_path / "in" / "grd.dim",
        [
            ("Amplitude_VV", "uint16", "amplitude", amplitude),
            ("Intensity_VV", "float32", "intensity", "Amplitude_VV * Amplitude_VV"),
        ],
        sample_type="DETECTED",
    )


class FakeDenoiser:
    """Writes the amplitude of what it reads, halved -- a recognisable result."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def denoise_image_tiled(self, image, out, **tiling) -> None:
        self.calls.append(tiling)
        block = np.asarray(image[0:H, 0:W], dtype=np.float32)
        amplitude = np.hypot(block[..., 0], block[..., 1]) if block.ndim == 3 else block
        out[0:H, 0:W] = amplitude / 2


@pytest.fixture
def denoiser(monkeypatch: pytest.MonkeyPatch) -> FakeDenoiser:
    fake = FakeDenoiser()
    monkeypatch.setattr(deep, "_denoiser", lambda method, model, symetrise: fake)
    return fake


class TestDimapProduct:
    def test_reads_bands_and_their_data(self, slc: Path):
        product = DimapProduct(slc)
        assert (product.width, product.height) == (W, H)
        assert [b.name for b in product.bands][:3] == [
            "i_IW2_VV",
            "q_IW2_VV",
            "Intensity_IW2_VV",
        ]
        assert product.band("Intensity_IW2_VV").virtual
        np.testing.assert_array_equal(
            product.read_band("i_IW2_VV"), _random("int16", 1)
        )

    def test_unknown_band_lists_the_ones_there_are(self, slc: Path):
        with pytest.raises(KeyError, match="i_IW2_VV"):
            DimapProduct(slc).band("Amplitude_VV")

    def test_copy_skips_data_and_rewires_paths(self, slc: Path, tmp_path: Path):
        copy = copy_product(
            slc, tmp_path / "out" / "copy.dim", skip_bands=("i_IW2_VV",)
        )

        assert not (copy.data_dir / "i_IW2_VV.img").exists()
        assert (copy.data_dir / "q_IW2_VV.img").is_file()
        assert (copy.data_dir / "tie_point_grids" / "latitude.img").is_file()
        hrefs = [href for e in copy.root.iter() if (href := e.get("href"))]
        assert hrefs and all(h.startswith("copy.data/") for h in hrefs)
        assert copy.root.get("name") == "copy.dim"

    def test_copy_onto_itself_is_refused(self, slc: Path):
        with pytest.raises(ValueError, match="onto itself"):
            copy_product(slc, slc)


class TestMerlin:
    def test_iq_pairs_become_amplitude_bands(self, slc: Path, tmp_path: Path, denoiser):
        dst = despeckle_dimap(slc, tmp_path / "out" / "merlin.dim", method="merlin")
        product = DimapProduct(dst)

        assert [b.name for b in product.bands] == [
            "Amplitude_IW2_VV",
            "Intensity_IW2_VV",
            "Amplitude_IW2_VH",
        ]
        assert [b.index for b in product.bands] == [0, 1, 2]
        vv = product.band("Amplitude_IW2_VV")
        assert (vv.unit, vv.data_type) == ("amplitude", "float32")
        assert product.band("Intensity_IW2_VV").expression == (
            "Amplitude_IW2_VV * Amplitude_IW2_VV"
        )

        i, q = _random("int16", 1), _random("int16", 2)
        np.testing.assert_allclose(
            product.read_band("Amplitude_IW2_VV"), np.hypot(i, q) / 2, rtol=1e-6
        )
        assert not list(product.data_dir.glob("[iq]_*"))
        assert len(denoiser.calls) == 2

    def test_header_follows(self, slc: Path, tmp_path: Path, denoiser):
        dst = despeckle_dimap(slc, tmp_path / "out" / "merlin.dim", method="merlin")
        root = ET.parse(dst).getroot()

        attrs = {e.get("name"): e.text for e in root.iter("MDATTR")}
        assert attrs["sample_type"] == "DETECTED"
        assert attrs["band_names"] == "Amplitude_IW2_VV Intensity_IW2_VV"
        assert root.findtext("Raster_Dimensions/NBANDS") == "3"
        files = {
            f.findtext("BAND_INDEX"): path.get("href")
            for f in root.iterfind("Data_Access/Data_File")
            for path in f.iter("DATA_FILE_PATH")
        }
        assert files == {
            "0": "merlin.data/Amplitude_IW2_VV.hdr",
            "2": "merlin.data/Amplitude_IW2_VH.hdr",
        }

    def test_bands_restricts_the_pairs(self, slc: Path, tmp_path: Path, denoiser):
        dst = despeckle_dimap(
            slc, tmp_path / "out" / "vh.dim", method="merlin", bands=["i_IW2_VH"]
        )
        names = [b.name for b in DimapProduct(dst).bands]
        assert "Amplitude_IW2_VH" in names
        assert "i_IW2_VV" in names

    def test_needs_complex_bands(self, grd: Path, tmp_path: Path, denoiser):
        with pytest.raises(ValueError, match="complex i_\\*/q_\\* bands"):
            despeckle_dimap(grd, tmp_path / "out" / "x.dim", method="merlin")


class TestSar2Sar:
    def test_amplitude_is_replaced(self, grd: Path, tmp_path: Path, denoiser):
        dst = despeckle_dimap(grd, tmp_path / "out" / "s2s.dim", method="sar2sar")
        product = DimapProduct(dst)

        assert [b.name for b in product.bands] == ["Amplitude_VV", "Intensity_VV"]
        assert product.band("Amplitude_VV").data_type == "float32"
        original = DimapProduct(grd).read_band("Amplitude_VV").astype(np.float32)
        np.testing.assert_allclose(product.read_band("Amplitude_VV"), original / 2)

    def test_intensity_is_despeckled_as_amplitude(self, tmp_path: Path, denoiser):
        intensity = (np.abs(_random("int16", 6)) + 1).astype(np.float32) ** 2
        src = make_dimap(
            tmp_path / "in" / "sigma0.dim",
            [("Sigma0_VV", "float32", "intensity", intensity)],
        )
        dst = despeckle_dimap(src, tmp_path / "out" / "s2s.dim", method="sar2sar")
        result = DimapProduct(dst).read_band("Sigma0_VV")
        np.testing.assert_allclose(result, (np.sqrt(intensity) / 2) ** 2, rtol=1e-5)


class TestRealModels:
    """The real networks on a small product; skipped without the extra."""

    @pytest.fixture(autouse=True)
    def _needs_deepdespeckling(self):
        pytest.importorskip("deepdespeckling")

    def test_merlin(self, tmp_path: Path):
        rng = np.random.default_rng(0)
        shape = (300, 280)
        src = make_dimap(
            tmp_path / "in" / "slc.dim",
            [
                ("i_IW2_VV", "float32", "real", rng.normal(0, 200, shape)),
                ("q_IW2_VV", "float32", "imaginary", rng.normal(0, 200, shape)),
            ],
            shape=shape,
        )
        dst = despeckle_dimap(
            src, tmp_path / "out" / "m.dim", method="merlin", block_size=256
        )
        result = np.asarray(DimapProduct(dst).read_band("Amplitude_IW2_VV"))
        assert result.shape == shape
        assert np.isfinite(result).all() and (result > 0).all()

    def test_sar2sar(self, tmp_path: Path):
        rng = np.random.default_rng(0)
        shape = (300, 280)
        amplitude = rng.rayleigh(200, shape).astype(np.float32) + 1
        src = make_dimap(
            tmp_path / "in" / "grd.dim",
            [("Amplitude_VV", "float32", "amplitude", amplitude)],
            sample_type="DETECTED",
            shape=shape,
        )
        dst = despeckle_dimap(src, tmp_path / "out" / "s.dim", method="sar2sar")
        result = np.asarray(DimapProduct(dst).read_band("Amplitude_VV"))
        assert np.isfinite(result).all()
        # despeckling reduces the spread of a homogeneous area
        assert result.std() < amplitude.std()
