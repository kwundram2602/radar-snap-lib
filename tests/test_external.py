"""External (Python) nodes: validation, the split around them, and running it.

JVM-free and torch-free: `execute_xml` and the external op's `run` are
monkeypatched, so these tests exercise the orchestration only.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from radar_snap_lib.snap_ops import runner
from radar_snap_lib.snap_ops.cli import main
from radar_snap_lib.snap_ops.external import EXTERNAL_OPS
from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, OpsConfig
from radar_snap_lib.snap_ops.runner import SegmentedRunResult, run_graph
from radar_snap_lib.snap_ops.segments import split_at_external
from radar_snap_lib.snap_ops.visualization import to_dot

SLC = {
    "Read": {"file": "S1A_IW_SLC.zip"},
    "TOPSAR-Split": {"subswath": "IW2"},
    "TOPSAR-Deburst": {},
    "DeepDespeckle": {"method": "merlin"},
    "Calibration": {},
    "Write": {"file": "/out/sigma0.dim"},
}


def _config(pipeline: dict, registry, output_dir: str | None = "/work") -> OpsConfig:
    config: dict = {"pipeline": pipeline}
    if output_dir is not None:
        config["output_dir"] = output_dir
    return OpsConfig.load(config, registry=registry)


def _split(pipeline: dict, registry, output_dir: str | None = "/work"):
    config = _config(pipeline, registry, output_dir)
    nodes = config.parse()
    assert config.validate(nodes) == []
    return split_at_external(nodes, config.output_dir)


def _ids(segment) -> list[tuple[str, str]]:
    return [(n.id, n.op) for n in segment.nodes]


class TestValidation:
    def test_a_valid_node_passes(self, registry):
        assert _config(SLC, registry).validate() == []

    def test_method_is_required_and_checked(self, registry):
        errors = _config({**SLC, "DeepDespeckle": {}}, registry).validate()
        assert any("requires parameter 'method'" in e for e in errors)
        errors = _config(
            {**SLC, "DeepDespeckle": {"method": "lee"}}, registry
        ).validate()
        assert any("method='lee' is not valid" in e for e in errors)

    def test_merlin_parameters_are_rejected_for_sar2sar(self, registry):
        pipeline = {**SLC, "DeepDespeckle": {"method": "sar2sar", "model": "stripmap"}}
        errors = _config(pipeline, registry).validate()
        assert any("only apply to method 'merlin'" in e for e in errors)

    def test_typo_in_a_parameter_gets_a_suggestion(self, registry):
        pipeline = {**SLC, "DeepDespeckle": {"method": "merlin", "modle": "x"}}
        errors = _config(pipeline, registry).validate()
        assert any("no parameter 'modle'" in e and "model" in e for e in errors)

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"patch_size": 200}, "power of two"),
            ({"stride_size": 256}, "stride_size must be between"),
            ({"block_size": 128}, "block_size must be at least"),
        ],
    )
    def test_tiling_parameters(self, registry, params, message):
        pipeline = {**SLC, "DeepDespeckle": {"method": "merlin", **params}}
        errors = _config(pipeline, registry).validate()
        assert any(message in e for e in errors)

    def test_output_dir_is_required(self, registry):
        errors = _config(SLC, registry, output_dir=None).validate()
        assert any("need the config's 'output_dir'" in e for e in errors)

    def test_no_write_warning_when_the_pipeline_ends_in_an_external_node(
        self, registry, recwarn
    ):
        pipeline = {"Read": {"file": "a.zip"}, "DeepDespeckle": {"method": "merlin"}}
        assert _config(pipeline, registry).validate() == []
        assert not [w for w in recwarn if "no 'Write' node" in str(w.message)]

    def test_to_xml_refuses(self, registry):
        with pytest.raises(GraphConfigError, match="several graphs"):
            _config(SLC, registry).to_xml()


class TestSplit:
    def test_no_external_node_means_no_split(self, registry):
        pipeline = {k: v for k, v in SLC.items() if k != "DeepDespeckle"}
        assert _split(pipeline, registry) is None

    def test_one_node_makes_two_graphs(self, registry):
        first, second = _split(SLC, registry)

        assert _ids(first) == [
            ("Read", "Read"),
            ("TOPSAR-Split", "TOPSAR-Split"),
            ("TOPSAR-Deburst", "TOPSAR-Deburst"),
            ("DeepDespeckle__in", "Write"),
        ]
        hand_over = first.nodes[-1]
        assert hand_over.sources == ["TOPSAR-Deburst"]
        assert hand_over.params == {
            "file": "/work/DeepDespeckle__in.dim",
            "formatName": "BEAM-DIMAP",
        }
        assert first.step.input == Path("/work/DeepDespeckle__in.dim")
        assert first.step.output == Path("/work/DeepDespeckle.dim")
        assert first.step.remove_input

        assert _ids(second) == [
            ("DeepDespeckle", "Read"),
            ("Calibration", "Calibration"),
            ("Write", "Write"),
        ]
        assert second.nodes[0].params == {"file": "/work/DeepDespeckle.dim"}
        assert second.step is None

    def test_a_saved_source_is_handed_over_as_is(self, registry):
        pipeline = {**SLC, "TOPSAR-Deburst": {"save": "deburst"}}
        first, _ = _split(pipeline, registry)

        assert [n.id for n in first.nodes][-1] == "TOPSAR-Deburst__save"
        assert first.step.input == Path("/work/deburst.dim")
        assert not first.step.remove_input

    def test_save_names_the_external_output(self, registry):
        pipeline = {**SLC, "DeepDespeckle": {"method": "merlin", "save": "merlin"}}
        first, second = _split(pipeline, registry)
        assert first.step.output == Path("/work/merlin.dim")
        assert second.nodes[0].params == {"file": "/work/merlin.dim"}

    def test_saves_land_in_the_graph_that_computes_them(self, registry):
        pipeline = {**SLC, "TOPSAR-Split": {"subswath": "IW2", "save": True}}
        pipeline["Calibration"] = {"save": True}
        first, second = _split(pipeline, registry)
        assert "TOPSAR-Split__save" in [n.id for n in first.nodes]
        assert "Calibration__save" in [n.id for n in second.nodes]

    def test_pipeline_ending_in_an_external_node_has_one_graph(self, registry):
        pipeline = {k: v for k, v in SLC.items() if k not in ("Calibration", "Write")}
        (only,) = _split(pipeline, registry)
        assert only.step is not None

    def test_two_external_nodes_in_a_row_need_no_graph_between(self, registry):
        pipeline = {
            "Read": {"file": "a.zip"},
            "first": {"op": "DeepDespeckle", "method": "merlin"},
            "second": {"op": "DeepDespeckle", "method": "sar2sar"},
            "Write": {"file": "/out/x.dim"},
        }
        a, b, c = _split(pipeline, registry)

        assert a.step.node.id == "first"
        assert b.nodes == []
        assert b.step.input == Path("/work/first.dim")
        assert not b.step.remove_input
        assert _ids(c) == [("second", "Read"), ("Write", "Write")]

    def test_a_read_used_on_both_sides_is_read_again(self, registry):
        pipeline = {
            "Read": {"file": "a.dim"},
            "DeepDespeckle": {"method": "sar2sar"},
            "BandMerge": {"sources": ["DeepDespeckle", "Read"]},
            "Write": {"file": "/out/x.dim"},
        }
        first, second = _split(pipeline, registry)
        assert ("Read", "Read") in _ids(first)
        assert ("Read", "Read") in _ids(second)

    def test_a_computed_node_used_on_both_sides_is_an_error(self, registry):
        pipeline = {
            **SLC,
            "BandMerge": {"sources": ["Calibration", "TOPSAR-Deburst"]},
            "Write": {"file": "/out/x.dim"},
        }
        with pytest.raises(GraphConfigError, match="'TOPSAR-Deburst' is computed"):
            _split(pipeline, registry)

    def test_snaphu_and_external_nodes_do_not_mix_yet(self, registry):
        pipeline = {
            "Read": {"file": "ifg.dim"},
            "SnaphuExport": {"targetFolder": "/snaphu"},
            "SnaphuImport": {"sources": "SnaphuExport"},
            "DeepDespeckle": {"method": "sar2sar"},
            "Write": {"file": "/out/x.dim"},
        }
        config = _config(pipeline, registry)
        with pytest.raises(GraphConfigError, match="cannot have nodes"):
            split_at_external(config.parse(), config.output_dir)


@pytest.fixture
def fake_op(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path, dict]]:
    """Replace DeepDespeckle's run with a recorder; its requirement is met."""
    calls: list[tuple[Path, Path, dict]] = []

    def run(src: Path, dst: Path, params: dict) -> None:
        calls.append((src, dst, params))

    op = dataclasses.replace(EXTERNAL_OPS["DeepDespeckle"], run=run, requires="json")
    monkeypatch.setitem(EXTERNAL_OPS, "DeepDespeckle", op)
    return calls


class TestRun:
    def test_graphs_and_steps_run_in_order(
        self, tmp_path: Path, registry, fake_op, monkeypatch: pytest.MonkeyPatch
    ):
        order: list[str] = []
        hand_over = tmp_path / "DeepDespeckle__in.dim"

        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            order.append("graph b" if "radar-snap-b" in xml else "graph a")
            if "DeepDespeckle__in" in xml:
                hand_over.write_text("<Dimap_Document/>", encoding="utf-8")
                hand_over.with_suffix(".data").mkdir()

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        result = run_graph(
            {"output_dir": str(tmp_path), "pipeline": SLC}, registry=registry
        )

        assert isinstance(result, SegmentedRunResult)
        assert order == ["graph a", "graph b"]
        ((src, dst, params),) = fake_op
        assert (src, dst) == (hand_over, tmp_path / "DeepDespeckle.dim")
        assert params["method"] == "merlin"
        assert params["model"] == "Sentinel-TOPS"  # the spec's default
        assert params["block_size"] == 4096
        # the hand-over file is gone, the graphs are kept beside the results
        assert not hand_over.exists()
        assert not hand_over.with_suffix(".data").exists()
        assert (tmp_path / "graph.a.xml").is_file()
        assert (tmp_path / "graph.b.xml").is_file()
        assert [s.node.id for s in result.steps] == ["DeepDespeckle"]

    def test_dump_xml_writes_every_graph(
        self, tmp_path: Path, registry, fake_op, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(runner, "execute_xml", lambda xml, quiet=False: None)
        run_graph(
            {"output_dir": str(tmp_path / "work"), "pipeline": SLC},
            registry=registry,
            dump_xml=tmp_path / "graph.xml",
        )
        assert "TOPSAR-Deburst" in (tmp_path / "graph.a.xml").read_text()
        assert "Calibration" in (tmp_path / "graph.b.xml").read_text()

    def test_missing_package_stops_before_the_first_graph(
        self, tmp_path: Path, registry, monkeypatch: pytest.MonkeyPatch
    ):
        op = dataclasses.replace(
            EXTERNAL_OPS["DeepDespeckle"], requires="no_such_module_for_tests"
        )
        monkeypatch.setitem(EXTERNAL_OPS, "DeepDespeckle", op)
        ran: list[str] = []
        monkeypatch.setattr(
            runner, "execute_xml", lambda xml, quiet=False: ran.append(xml)
        )

        with pytest.raises(RuntimeError, match="despeckle"):
            run_graph({"output_dir": str(tmp_path), "pipeline": SLC}, registry=registry)
        assert ran == []


class TestCli:
    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "merlin.yaml"
        lines = [f"output_dir: {tmp_path / 'work'}", "pipeline:"]
        for node, params in SLC.items():
            lines.append(f"  {node}: {params!r}".replace("'", '"'))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_validate(self, config_file: Path, fake_op, capsys):
        assert main(["validate", str(config_file)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_validate_warns_about_a_missing_package(
        self, config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        op = dataclasses.replace(
            EXTERNAL_OPS["DeepDespeckle"], requires="no_such_module_for_tests"
        )
        monkeypatch.setitem(EXTERNAL_OPS, "DeepDespeckle", op)
        assert main(["validate", str(config_file)]) == 0
        assert "warning" in capsys.readouterr().err

    def test_dump_xml_writes_every_graph(self, config_file: Path, tmp_path: Path):
        out = tmp_path / "graph.xml"
        assert main(["dump-xml", str(config_file), "-o", str(out)]) == 0
        assert (tmp_path / "graph.a.xml").is_file()
        assert (tmp_path / "graph.b.xml").is_file()


def test_drawn_as_a_python_node_with_dashed_hand_overs(registry):
    dot = to_dot(_config(SLC, registry).to_graph())
    assert "Python (outside SNAP)" in dot
    hand_over = '[style=dashed, label=" BEAM-DIMAP "]'
    assert f'"TOPSAR-Deburst" -> "DeepDespeckle" {hand_over}' in dot
    assert f'"DeepDespeckle" -> "Calibration" {hand_over}' in dot
