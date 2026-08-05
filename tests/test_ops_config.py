"""Tests for the YAML front end: parsing, defaulting and validation."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from radar_snap_lib.snap_ops.cli import _is_pipeline_config
from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, OpsConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
GRAPH_EXAMPLES = sorted(p for p in EXAMPLES.glob("*.yaml") if _is_pipeline_config(p))


def _config(pipeline: dict, registry, **top) -> OpsConfig:
    return OpsConfig.load({"pipeline": pipeline, **top}, registry=registry)


LINEAR = {
    "Read": {"file": "in.zip"},
    "Apply-Orbit-File": {},
    "Terrain-Correction": {"pixelSpacingInMeter": 10.0},
    "Write": {"file": "out.tif", "formatName": "GeoTIFF"},
}


class TestParsing:
    def test_node_id_doubles_as_operator(self, registry):
        nodes = _config(LINEAR, registry).parse()
        assert [n.id for n in nodes] == list(LINEAR)
        assert [n.op for n in nodes] == list(LINEAR)

    def test_source_defaults_to_previous_node(self, registry):
        nodes = _config(LINEAR, registry).parse()
        assert nodes[0].sources == []
        assert nodes[1].sources == ["Read"]
        assert nodes[2].sources == ["Apply-Orbit-File"]

    def test_empty_body_means_no_parameters(self, registry):
        nodes = _config(LINEAR, registry).parse()
        assert nodes[1].params == {}

    def test_null_body_is_allowed(self, registry):
        nodes = _config(
            {"Read": {"file": "a.zip"}, "Apply-Orbit-File": None}, registry
        ).parse()
        assert nodes[1].params == {}

    def test_op_key_overrides_the_alias(self, registry):
        nodes = _config({"ref": {"op": "Read", "file": "a.zip"}}, registry).parse()
        assert nodes[0].id == "ref"
        assert nodes[0].op == "Read"
        assert nodes[0].params == {"file": "a.zip"}

    def test_scalar_sources_is_accepted(self, registry):
        pipeline = {
            "a": {"op": "Read", "file": "a.zip"},
            "b": {"op": "Read", "sources": [], "file": "b.zip"},
            "tc": {"op": "Terrain-Correction", "sources": "a"},
        }
        nodes = _config(pipeline, registry).parse()
        assert nodes[1].sources == []
        assert nodes[2].sources == ["a"]

    def test_list_sources(self, registry):
        pipeline = {
            "a": {"op": "Read", "file": "a.zip"},
            "b": {"op": "Read", "sources": [], "file": "b.zip"},
            "coreg": {"op": "Back-Geocoding", "sources": ["a", "b"]},
        }
        assert _config(pipeline, registry).parse()[2].sources == ["a", "b"]

    def test_reserved_keys_are_not_parameters(self, registry):
        pipeline = {
            "a": {"op": "Read", "file": "a.zip"},
            "tc": {"op": "Terrain-Correction", "sources": "a"},
        }
        assert _config(pipeline, registry).parse()[1].params == {}

    def test_interpolation_is_resolved(self, registry):
        config = _config(
            {"Read": {"file": "${vars.scene}"}},
            registry,
            vars={"scene": "/data/x.zip"},
        )
        assert config.parse()[0].params["file"] == "/data/x.zip"

    def test_missing_pipeline_section(self, registry):
        with pytest.raises(GraphConfigError, match="no 'pipeline' section"):
            OpsConfig.load({"vars": {}}, registry=registry).parse()

    def test_missing_file(self, registry):
        with pytest.raises(GraphConfigError, match="not found"):
            OpsConfig.load("does/not/exist.yaml", registry=registry)


class TestValidation:
    def test_valid_pipeline_has_no_errors(self, registry):
        assert _config(LINEAR, registry).validate() == []

    def test_unknown_operator_suggests_alternatives(self, registry):
        errors = _config({"Terrain-Corection": {}}, registry).validate()
        assert len(errors) == 1
        assert "unknown operator" in errors[0]
        assert "Terrain-Correction" in errors[0]

    def test_unknown_parameter_suggests_alternatives(self, registry):
        pipeline = dict(LINEAR)
        pipeline["Terrain-Correction"] = {"pixelSpacingInMetre": 10.0}
        errors = _config(pipeline, registry).validate()
        assert any("has no parameter 'pixelSpacingInMetre'" in e for e in errors)
        assert any("pixelSpacingInMeter" in e for e in errors)

    def test_parameter_alias_is_accepted(self, registry):
        pipeline = dict(LINEAR)
        pipeline["Terrain-Correction"] = {"sourceBands": ["Sigma0_VV"]}
        assert _config(pipeline, registry).validate() == []

    def test_value_outside_value_set(self, registry):
        pipeline = dict(LINEAR)
        pipeline["Terrain-Correction"] = {"demResamplingMethod": "WRONG"}
        errors = _config(pipeline, registry).validate()
        assert any(
            "is not valid" in e and "BILINEAR_INTERPOLATION" in e for e in errors
        )

    def test_value_inside_value_set(self, registry):
        pipeline = dict(LINEAR)
        pipeline["Terrain-Correction"] = {"demResamplingMethod": "CUBIC_CONVOLUTION"}
        assert _config(pipeline, registry).validate() == []

    def test_missing_required_parameter(self, registry):
        errors = _config({"Read": {}, "Write": {"file": "o.tif"}}, registry).validate()
        assert any("requires parameter 'file'" in e for e in errors)

    def test_unknown_source_reference(self, registry):
        pipeline = {
            "Read": {"file": "a.zip"},
            "Terrain-Correction": {"sources": "ghost"},
        }
        errors = _config(pipeline, registry).validate()
        assert any("unknown source 'ghost'" in e for e in errors)

    def test_too_many_sources(self, registry):
        pipeline = {
            "a": {"op": "Read", "file": "a.zip"},
            "b": {"op": "Read", "sources": [], "file": "b.zip"},
            "tc": {"op": "Terrain-Correction", "sources": ["a", "b"]},
        }
        errors = _config(pipeline, registry).validate()
        assert any("at most 1 source" in e for e in errors)

    def test_too_few_sources(self, registry):
        errors = _config({"Terrain-Correction": {}}, registry).validate()
        assert any("at least 1 source" in e for e in errors)

    def test_array_source_operator_accepts_many(self, registry):
        pipeline = {
            "a": {"op": "Read", "file": "a.zip"},
            "b": {"op": "Read", "sources": [], "file": "b.zip"},
            "c": {"op": "Read", "sources": [], "file": "c.zip"},
            "coreg": {"op": "Back-Geocoding", "sources": ["a", "b", "c"]},
            "Write": {"file": "o.dim"},
        }
        assert _config(pipeline, registry).validate() == []

    def test_self_reference(self, registry):
        pipeline = {
            "Read": {"file": "a.zip"},
            "tc": {"op": "Terrain-Correction", "sources": "tc"},
        }
        errors = _config(pipeline, registry).validate()
        assert any("cannot be its own source" in e for e in errors)

    def test_cycle_is_detected(self, registry):
        pipeline = {
            "a": {"op": "Subset", "sources": "b"},
            "b": {"op": "Subset", "sources": "a"},
        }
        errors = _config(pipeline, registry).validate()
        assert any("Cycle in pipeline" in e for e in errors)

    def test_all_errors_are_reported_together(self, registry):
        pipeline = {
            "Read": {"file": "a.zip"},
            "Nope-Operator": {},
            "Terrain-Correction": {"bogusParam": 1, "demResamplingMethod": "WRONG"},
        }
        errors = _config(pipeline, registry).validate()
        assert len(errors) >= 3

    def test_missing_write_node_warns(self, registry):
        with pytest.warns(UserWarning, match="no 'Write' node"):
            _config({"Read": {"file": "a.zip"}}, registry).validate()

    def test_error_message_lists_every_problem(self, registry):
        pipeline = {"Read": {}, "Nope": {}}
        with pytest.raises(GraphConfigError) as excinfo:
            _config(pipeline, registry).to_graph()
        assert len(excinfo.value.errors) == len(str(excinfo.value).splitlines()) - 1


class TestGraphConstruction:
    def test_builds_a_graph(self, registry):
        graph = _config(LINEAR, registry).to_graph()
        assert [node.id for node in graph.nodes] == list(LINEAR)

    def test_forward_reference_is_ordered_correctly(self, registry):
        """A node may name a source defined further down the file."""
        pipeline = {
            "tc": {"op": "Terrain-Correction", "sources": "src"},
            "src": {"op": "Read", "sources": [], "file": "a.zip"},
            "Write": {"sources": "tc", "file": "o.tif"},
        }
        graph = _config(pipeline, registry).to_graph()
        order = [node.id for node in graph.topological_order()]
        assert order.index("src") < order.index("tc") < order.index("Write")

    def test_invalid_config_raises_before_building(self, registry):
        with pytest.raises(GraphConfigError):
            _config({"Bogus-Operator": {}}, registry).to_graph()

    def test_yaml_and_builder_agree(self, registry):
        """Both front ends must produce byte-identical XML."""
        from radar_snap_lib.snap_ops.graph import Graph

        from_yaml = _config(LINEAR, registry).to_xml()

        graph = Graph(registry=registry)
        src = graph.read("in.zip")
        orbit = graph.apply_orbit_file(src)
        tc = graph.terrain_correction(orbit, pixelSpacingInMeter=10.0)
        graph.write(tc, file="out.tif", formatName="GeoTIFF")

        assert from_yaml == graph.to_xml()


class TestExamples:
    @pytest.mark.parametrize("path", GRAPH_EXAMPLES, ids=lambda p: p.name)
    def test_example_is_valid(self, path, registry):
        assert OpsConfig.load(path, registry=registry).validate() == []

    @pytest.mark.parametrize("path", GRAPH_EXAMPLES, ids=lambda p: p.name)
    def test_example_serialises(self, path, registry):
        root = ET.fromstring(OpsConfig.load(path, registry=registry).to_xml())
        assert root.tag == "graph"
        assert root.findall("node")

    def test_interferogram_wires_two_sources(self, registry):
        path = EXAMPLES / "s1_slc_interferogram.yaml"
        root = ET.fromstring(OpsConfig.load(path, registry=registry).to_xml())
        sources = root.find("./node[@id='coreg']/sources")
        assert sources is not None
        assert [child.get("refid") for child in sources] == ["split_ref", "split_sec"]


@pytest.mark.snap
class TestSnapAcceptsGeneratedXml:
    @pytest.mark.parametrize("path", GRAPH_EXAMPLES, ids=lambda p: p.name)
    def test_graph_io_parses(self, path, registry):
        from radar_snap_lib.config import ensure_esa_snappy

        ensure_esa_snappy()
        from esa_snappy import jpy

        graph_io = jpy.get_type("org.esa.snap.core.gpf.graph.GraphIO")
        string_reader = jpy.get_type("java.io.StringReader")

        config = OpsConfig.load(path, registry=registry)
        graph = graph_io.read(string_reader(config.to_xml()))
        assert len(list(graph.getNodes())) == len(config.parse())
