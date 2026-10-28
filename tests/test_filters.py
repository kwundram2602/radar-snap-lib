"""Filter nodes: the ops, filtering a GeoTIFF, validation, the split and the run.

JVM-free: `execute_xml` is monkeypatched where a config has a SNAP part.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from skimage import morphology

from radar_snap_lib.filters import CANNY_NODATA, FilterStep, run_filters
from radar_snap_lib.filters import ops as filter_ops
from radar_snap_lib.snap_ops import runner
from radar_snap_lib.snap_ops.cli import main
from radar_snap_lib.snap_ops.filter_tail import split_filter_tail
from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, OpsConfig
from radar_snap_lib.snap_ops.runner import FilterRunResult, run_graph
from radar_snap_lib.snap_ops.segments import split_at_external
from radar_snap_lib.snap_ops.visualization import to_dot

GRD = {
    "Read": {"file": "S1A_IW_GRDH.zip"},
    "Calibration": {},
    "Terrain-Correction": {},
    "Write": {"file": "/out/gamma0.tif", "formatName": "GeoTIFF"},
    "Canny": {"sigma": 2.0},
    "Closing": {"radius": 2},
}


def _square(size: int = 32) -> np.ndarray:
    image = np.zeros((size, size), np.float32)
    image[8:24, 8:24] = 1.0
    return image


def _write_tif(path: Path, data: np.ndarray, nodata: float | None = None) -> Path:
    data = data[np.newaxis] if data.ndim == 2 else data
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=data.shape[2],
        height=data.shape[1],
        count=data.shape[0],
        dtype=data.dtype,
        crs="EPSG:32632",
        transform=from_origin(500000, 5600000, 10, 10),
        nodata=nodata,
    ) as dataset:
        dataset.write(data)
    return path


def _config(pipeline: dict, registry, output_dir: str | None = "/work") -> OpsConfig:
    config: dict = {"pipeline": pipeline}
    if output_dir is not None:
        config["output_dir"] = output_dir
    return OpsConfig.load(config, registry=registry)


def _split(pipeline: dict, registry, output_dir: str | None = "/work"):
    config = _config(pipeline, registry, output_dir)
    nodes = config.parse()
    assert config.validate(nodes) == []
    return split_filter_tail(nodes, config.output_dir)


class TestOps:
    def test_canny_finds_the_edge_of_a_square(self):
        image = _square()
        edges = filter_ops.canny(image, np.ones(image.shape, bool))
        assert edges.dtype == np.uint8
        assert edges[8:24, 8].any() and not edges[16, 16] and not edges[2, 2]

    @pytest.mark.parametrize(
        ("name", "reference"),
        [
            ("erosion", morphology.erosion),
            ("dilation", morphology.dilation),
            ("opening", morphology.opening),
            ("closing", morphology.closing),
        ],
    )
    def test_morphology_matches_skimage_on_valid_data(self, name, reference):
        rng = np.random.default_rng(0)
        image = rng.random((24, 24)).astype(np.float32)
        result = getattr(filter_ops, name)(
            image, np.ones(image.shape, bool), footprint="diamond", radius=2
        )
        np.testing.assert_array_equal(result, reference(image, morphology.diamond(2)))

    def test_invalid_pixels_do_not_leak_into_valid_ones(self):
        image = np.ones((9, 9), np.float32)
        valid = np.ones(image.shape, bool)
        image[4, 4], valid[4, 4] = np.nan, False
        for name in ("erosion", "dilation", "opening", "closing"):
            result = getattr(filter_ops, name)(image, valid, radius=1)
            assert np.all(result[valid] == 1.0), name

    def test_unknown_footprint(self):
        with pytest.raises(ValueError, match="unknown footprint"):
            filter_ops.footprint("hexagon", 1)


class TestRunFilters:
    def test_chain_keeps_georeferencing_and_writes_outputs(self, tmp_path: Path):
        src = _write_tif(tmp_path / "in.tif", np.stack([_square(), _square()]))
        steps = [
            FilterStep("edges", "Canny", bands=[2]),
            FilterStep("closed", "Closing", source="edges", params={"radius": 1}),
        ]
        written = run_filters(
            src, steps, {"edges": tmp_path / "e.tif", "closed": tmp_path / "c.tif"}
        )
        assert written == [tmp_path / "e.tif", tmp_path / "c.tif"]
        with rasterio.open(tmp_path / "c.tif") as out, rasterio.open(src) as ref:
            assert out.count == 1
            assert out.dtypes == ("uint8",)
            assert out.nodata == CANNY_NODATA
            assert out.crs == ref.crs and out.transform == ref.transform
            assert set(np.unique(out.read(1))) <= {0, 1}

    def test_nodata_is_carried_over(self, tmp_path: Path):
        image = _square()
        image[0, :] = -9999.0
        src = _write_tif(tmp_path / "in.tif", image, nodata=-9999.0)
        run_filters(
            src,
            [FilterStep("d", "Dilation"), FilterStep("c", "Canny")],
            {"d": tmp_path / "d.tif", "c": tmp_path / "c.tif"},
        )
        with rasterio.open(tmp_path / "d.tif") as out:
            assert out.dtypes == ("float32",) and out.nodata == -9999.0
            assert np.all(out.read(1)[0] == -9999.0)
        with rasterio.open(tmp_path / "c.tif") as out:
            assert np.all(out.read(1)[0] == CANNY_NODATA)

    def test_a_band_out_of_range(self, tmp_path: Path):
        src = _write_tif(tmp_path / "in.tif", _square())
        with pytest.raises(ValueError, match=r"band\(s\) \[3\]"):
            run_filters(src, [FilterStep("c", "Canny", bands=[3])], {})


class TestValidation:
    def test_a_valid_config_passes(self, registry):
        assert _config(GRD, registry).validate() == []

    def test_typo_in_a_parameter_gets_a_suggestion(self, registry):
        errors = _config({**GRD, "Closing": {"radus": 2}}, registry).validate()
        assert any("no parameter 'radus'" in e and "radius" in e for e in errors)

    @pytest.mark.parametrize(
        ("node", "params", "message"),
        [
            ("Closing", {"radius": 0}, "radius must be an integer >= 1"),
            ("Closing", {"footprint": "hexagon"}, "footprint='hexagon' is not valid"),
            ("Canny", {"sigma": 0}, "sigma must be > 0"),
            ("Canny", {"low_threshold": 0.5, "high_threshold": 0.2}, "is above"),
            (
                "Canny",
                {"use_quantiles": True, "high_threshold": 2},
                "between 0 and 1",
            ),
            ("Canny", {"bands": [0]}, "bands must be a list"),
        ],
    )
    def test_parameter_checks(self, registry, node, params, message):
        errors = _config({**GRD, node: params}, registry).validate()
        assert any(message in e for e in errors), errors

    def test_output_dir_is_required(self, registry):
        errors = _config(GRD, registry, output_dir=None).validate()
        assert any("Filter node(s) ['Canny', 'Closing']" in e for e in errors)

    def test_no_write_warning_for_a_pure_filter_config(self, registry, recwarn):
        _config({"Read": {"file": "a.tif"}, "Canny": {}}, registry).validate()
        assert not [w for w in recwarn if "no 'Write' node" in str(w.message)]

    def test_to_xml_refuses(self, registry):
        with pytest.raises(GraphConfigError, match="run outside SNAP"):
            _config(GRD, registry).to_xml()


class TestSplit:
    def test_no_filter_means_no_tail(self, registry):
        nodes, tail = _split({"Read": {"file": "a.zip"}, "Write": {}}, registry)
        assert tail is None and [n.id for n in nodes] == ["Read", "Write"]

    def test_a_geotiff_write_is_read_back(self, registry):
        nodes, tail = _split(GRD, registry)
        assert [n.id for n in nodes] == [
            "Read",
            "Calibration",
            "Terrain-Correction",
            "Write",
        ]
        assert tail is not None
        (chain,) = tail.chains
        assert chain.input == Path("/out/gamma0.tif")
        assert [(s.id, s.source) for s in chain.steps] == [
            ("Canny", None),
            ("Closing", "Canny"),
        ]
        assert chain.outputs == {"Closing": Path("/work/Closing.tif")}

    def test_snap_adds_tif_to_a_write_without_suffix(self, registry):
        write = {"file": "/out/gamma0", "formatName": "GeoTIFF-BigTIFF"}
        _, tail = _split({**GRD, "Write": write}, registry)
        assert tail is not None
        assert tail.chains[0].input == Path("/out/gamma0.tif")

    def test_a_dimap_write_is_an_error(self, registry):
        with pytest.raises(GraphConfigError, match="set formatName: GeoTIFF"):
            _split({**GRD, "Write": {"file": "/out/gamma0.dim"}}, registry)

    def test_a_tif_read_needs_no_snap(self, registry):
        nodes, tail = _split(
            {"Read": {"file": "/data/a.tif"}, "Canny": {}, "Dilation": {}}, registry
        )
        assert nodes == []
        assert tail is not None
        assert tail.chains[0].input == Path("/data/a.tif")
        assert tail.outputs == [Path("/work/Dilation.tif")]

    def test_a_snap_node_gets_a_geotiff_write(self, registry):
        pipeline = {
            "Read": {"file": "a.zip"},
            "Terrain-Correction": {},
            "Canny": {},
        }
        nodes, tail = _split(pipeline, registry)
        added = nodes[-1]
        assert (added.id, added.op, added.sources) == (
            "Terrain-Correction__tif",
            "Write",
            ["Terrain-Correction"],
        )
        assert added.params["file"] == "/work/Terrain-Correction.tif"
        assert tail is not None
        assert tail.chains[0].input == Path("/work/Terrain-Correction.tif")

    def test_saves_and_branches_are_outputs(self, registry):
        pipeline = {
            "Read": {"file": "/data/a.tif"},
            "Canny": {"save": "edges"},
            "Closing": {},
            "smooth": {"op": "Opening", "sources": "Read", "bands": [1]},
        }
        _, tail = _split(pipeline, registry)
        assert tail is not None
        # one chain per GeoTIFF: each band is read once for both branches
        (chain,) = tail.chains
        assert chain.input == Path("/data/a.tif")
        assert sorted(map(str, tail.outputs)) == [
            "/work/Closing.tif",
            "/work/edges.tif",
            "/work/smooth.tif",
        ]
        assert [(s.id, s.bands) for s in chain.steps if s.source is None] == [
            ("Canny", None),
            ("smooth", [1]),
        ]

    def test_a_snap_node_after_a_filter_is_an_error(self, registry):
        pipeline = {**GRD, "Speckle-Filter": {"sources": "Canny"}}
        with pytest.raises(GraphConfigError, match="have to come at the end"):
            _split(pipeline, registry)

    def test_bands_after_another_filter_is_an_error(self, registry):
        with pytest.raises(GraphConfigError, match="'bands' only applies"):
            _split({**GRD, "Closing": {"bands": [1]}}, registry)

    def test_outputs_overwriting_each_other(self, registry):
        pipeline = {**GRD, "Canny": {"save": "Closing"}}
        with pytest.raises(GraphConfigError, match="overwrite each other"):
            _split(pipeline, registry)

    def test_works_with_an_external_node_before_it(self, registry):
        pipeline = {
            "Read": {"file": "S1A_IW_GRDH.zip"},
            "DeepDespeckle": {"method": "sar2sar"},
            "Terrain-Correction": {},
            "Canny": {},
        }
        config = _config(pipeline, registry)
        nodes = config.parse()
        assert config.validate(nodes) == []
        snap_nodes, tail = split_filter_tail(nodes, config.output_dir)
        segments = split_at_external(snap_nodes, config.output_dir)
        assert tail is not None and segments is not None
        last = segments[-1].nodes
        assert [n.op for n in last][-1] == "Write"
        assert last[-1].params["file"] == "/work/Terrain-Correction.tif"


class TestRun:
    def test_a_pure_filter_config_does_not_start_snap(
        self, registry, tmp_path: Path, monkeypatch
    ):
        def no_snap(*_args, **_kwargs):
            raise AssertionError("SNAP must not run")

        monkeypatch.setattr(runner, "execute_xml", no_snap)
        src = _write_tif(tmp_path / "in.tif", _square())
        config = {
            "output_dir": str(tmp_path / "out"),
            "pipeline": {"Read": {"file": str(src)}, "Canny": {}},
        }
        result = run_graph(config, registry=registry)
        assert isinstance(result, FilterRunResult)
        assert result.snap is None
        assert result.outputs == [tmp_path / "out" / "Canny.tif"]
        assert result.outputs[0].is_file()

    def test_snap_runs_first_then_the_filters(
        self, registry, tmp_path: Path, monkeypatch
    ):
        tif = tmp_path / "gamma0.tif"
        xmls: list[str] = []

        def fake_snap(xml: str, **_kwargs) -> None:
            xmls.append(xml)
            _write_tif(tif, _square())

        monkeypatch.setattr(runner, "execute_xml", fake_snap)
        pipeline = {
            **GRD,
            "Write": {"file": str(tif), "formatName": "GeoTIFF"},
        }
        config = {"output_dir": str(tmp_path / "out"), "pipeline": pipeline}
        result = run_graph(config, registry=registry)
        assert isinstance(result, FilterRunResult)
        assert isinstance(result.snap, str) and len(xmls) == 1
        assert "Canny" not in xmls[0]
        assert result.outputs == [tmp_path / "out" / "Closing.tif"]
        assert (tmp_path / "out" / "graph.xml").is_file()


class TestCli:
    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "filters.yaml"
        path.write_text(
            f"output_dir: {tmp_path / 'out'}\n"
            "pipeline:\n"
            "  Read: {file: in.tif}\n"
            "  Canny: {sigma: 2.0}\n"
        )
        return path

    def test_validate(self, config_file: Path, capsys):
        assert main(["validate", str(config_file)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_dump_xml_says_there_is_no_graph(self, config_file: Path, capsys):
        assert main(["dump-xml", str(config_file)]) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no SNAP graph" in captured.err

    def test_describe(self, capsys):
        assert main(["describe", "Closing"]) == 0
        out = capsys.readouterr().out
        assert "footprint" in out and "one of: disk, square, diamond" in out


def test_drawn_as_filters_with_a_geotiff_hand_over(registry):
    dot = to_dot(_config(GRD, registry).to_graph())
    assert "Filter (scikit-image)" in dot
    assert '"Write" -> "Canny" [style=dashed, label=" GeoTIFF "]' in dot
    assert '"Canny" -> "Closing";' in dot
    assert "Closing.tif" in dot
