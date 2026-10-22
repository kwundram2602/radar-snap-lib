"""Tests for the Graphviz drawing of graphs. JVM-free; rendering needs `dot`."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from radar_snap_lib.snap_ops import visualization
from radar_snap_lib.snap_ops.cli import main
from radar_snap_lib.snap_ops.OpsConfig import OpsConfig
from radar_snap_lib.snap_ops.visualization import (
    file_name,
    graph_from_xml,
    render,
    short_value,
    to_dot,
    visualize,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
GRD = EXAMPLES / "s1_grd_gamma0.yaml"
HAS_DOT = shutil.which("dot") is not None

SLICES = {
    "output_dir": "/out",
    "pipeline": {
        "ProductSet-Reader": {
            "fileList": [
                "/s/S1A_IW_GRDH_1SDV_A_COG.SAFE/manifest.safe",
                "/s/S1A_IW_GRDH_1SDV_B_COG.SAFE/manifest.safe",
            ]
        },
        "SliceAssembly": {},
        "Subset": {
            "geoRegion": "POLYGON ((1 2, 3 2, 3 4, 1 4, 1 2))",
            "copyMetadata": True,
        },
        "tc_10m": {
            "op": "Terrain-Correction",
            "pixelSpacingInMeter": 10.0,
            "saveSelectedSourceBand": True,  # SNAP's default
            "save": "sigma0_10m",
        },
        "Multilook": {"sources": "Subset", "nRgLooks": 6, "nAzLooks": 6},
        "tc_60m": {
            "op": "Terrain-Correction",
            "pixelSpacingInMeter": 60.0,
            "save": "sigma0_60m",
        },
    },
}


def _graph(config, registry):
    return OpsConfig.load(config, registry=registry).to_graph()


class TestDot:
    def test_every_node_and_edge_is_drawn(self, registry):
        graph = _graph(SLICES, registry)
        dot = to_dot(graph)
        for node in graph.nodes:
            assert f'"{node.id}"' in dot
            for source in node.sources:
                assert f'"{source}" -> "{node.id}"' in dot

    def test_saves_are_output_files(self, registry):
        dot = to_dot(_graph(SLICES, registry))
        assert '"tc_10m" -> "tc_10m__save" [label=" save "]' in dot
        assert "sigma0_60m.dim" in dot

    def test_inputs_are_files_named_by_their_product(self, registry):
        dot = to_dot(_graph(SLICES, registry))
        assert '"ProductSet-Reader__in0" -> "ProductSet-Reader"' in dot
        assert '"ProductSet-Reader__in1" -> "ProductSet-Reader"' in dot
        assert "S1A_IW_GRDH_" in dot and "B_COG.SAFE" in dot
        assert "<B>manifest.safe" not in dot

    def test_defaults_hidden_and_values_shortened(self, registry):
        dot = to_dot(_graph(SLICES, registry))
        assert "saveSelectedSourceBand" not in dot.split("tooltip")[0]
        assert "POLYGON (5 pts)" in dot
        assert "<I>tc_10m</I>" in dot  # node id under the operator

    def test_legend_lists_only_present_categories(self, registry):
        dot = to_dot(_graph(SLICES, registry))
        legend = next(line for line in dot.splitlines() if "legend [" in line)
        assert "Sentinel-1" in legend and "Geometry / terrain" in legend
        assert "InSAR" not in legend

    def test_snaphu_edge_is_dashed(self, registry):
        config = OpsConfig.load(
            EXAMPLES / "s1_slc_interferogram.yaml", registry=registry
        )
        dot = to_dot(config.to_graph())
        assert "snaphu (external)" in dot

    def test_title(self, registry):
        assert "<B>my graph</B>" in to_dot(_graph(SLICES, registry), title="my graph")


class TestValues:
    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            (True, "true"),
            ("/data/s1/scene.zip", "scene.zip"),
            ("/s/X.SAFE/manifest.safe", "X.SAFE"),
            ("MULTIPOLYGON (((1 2, 3 4, 5 6, 1 2)))", "MULTIPOLYGON (4 pts)"),
            (["/a/b.dim", "/c/d.dim"], "b.dim, d.dim"),
            ({"a": 1, "b": 2}, "{2 entries}"),
            ("x" * 60, "x" * 39 + "…"),
        ],
    )
    def test_short_value(self, value, shown):
        assert short_value(value) == shown

    def test_file_name_keeps_urls(self):
        assert file_name("/data/out.tif") == "out.tif"


class TestXml:
    def test_round_trip(self, registry):
        graph = _graph(SLICES, registry)
        parsed = graph_from_xml(graph.to_xml(), registry=registry)
        assert [n.id for n in parsed.nodes] == [n.id for n in graph.nodes]
        for a, b in zip(parsed.nodes, graph.nodes, strict=True):
            assert (a.op, a.sources) == (b.op, b.sources)
            assert a.params == b.params

    def test_unordered_xml_and_default_values(self, registry):
        xml = """<graph id="g"><version>1.0</version>
          <node id="Write"><operator>Write</operator>
            <sources><sourceProduct refid="Calibration"/></sources>
            <parameters><file>out.dim</file><formatName>BEAM-DIMAP</formatName>
            </parameters></node>
          <node id="Calibration"><operator>Calibration</operator>
            <sources><sourceProduct refid="Read"/></sources>
            <parameters><outputSigmaBand>true</outputSigmaBand>
              <outputBetaBand>true</outputBetaBand><auxFile/></parameters></node>
          <node id="Read"><operator>Read</operator>
            <sources/><parameters><file>in.zip</file></parameters></node>
        </graph>"""
        graph = graph_from_xml(xml, registry=registry)
        assert [n.id for n in graph.nodes] == ["Read", "Calibration", "Write"]
        # outputSigmaBand=true is SNAP's default, empty elements are no value
        assert graph["Calibration"].params == {"outputBetaBand": True}

    def test_from_file(self, registry, tmp_path):
        path = tmp_path / "graph.xml"
        _graph(SLICES, registry).to_xml(path)
        assert len(graph_from_xml(path, registry=registry)) == 8


class TestRender:
    def test_dot_file_needs_no_graphviz(self, registry, tmp_path, monkeypatch):
        monkeypatch.setattr(visualization.shutil, "which", lambda name: None)
        out = render(_graph(SLICES, registry), tmp_path / "g.dot")
        assert out.read_text().startswith("digraph")

    def test_missing_graphviz_is_reported(self, registry, tmp_path, monkeypatch):
        monkeypatch.setattr(visualization.shutil, "which", lambda name: None)
        with pytest.raises(RuntimeError, match="install graphviz"):
            render(_graph(SLICES, registry), tmp_path / "g.svg")

    def test_unknown_format(self, registry, tmp_path):
        with pytest.raises(ValueError, match="unknown format"):
            render(_graph(SLICES, registry), tmp_path / "g.jpg")

    @pytest.mark.skipif(not HAS_DOT, reason="Graphviz dot not installed")
    def test_svg(self, registry, tmp_path):
        out = render(_graph(SLICES, registry), tmp_path / "g.svg", title="t")
        assert "<svg" in out.read_text()

    @pytest.mark.skipif(not HAS_DOT, reason="Graphviz dot not installed")
    def test_visualize_config_defaults_next_to_it(self, tmp_path):
        config = tmp_path / "grd.yaml"
        config.write_text(GRD.read_text())
        assert visualize(config) == tmp_path / "grd.svg"
        assert (tmp_path / "grd.svg").is_file()

    @pytest.mark.skipif(not HAS_DOT, reason="Graphviz dot not installed")
    def test_cli(self, tmp_path, capsys):
        out = tmp_path / "grd.png"
        assert main(["visualize", str(GRD), "-o", str(out)]) == 0
        assert out.read_bytes().startswith(b"\x89PNG")
        assert "Wrote" in capsys.readouterr().out

    def test_cli_missing_source(self, tmp_path, capsys):
        assert main(["visualize", str(tmp_path / "nope.yaml")]) == 2
        assert "Not found" in capsys.readouterr().err
