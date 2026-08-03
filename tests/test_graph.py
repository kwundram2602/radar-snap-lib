"""Tests for the node model and GPF XML serialisation."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from radar_snap_lib.snap_ops.graph import Graph, GraphError, NodeRef


@pytest.fixture
def graph(registry):
    return Graph(registry=registry)


def _node(xml: str, node_id: str) -> ET.Element:
    root = ET.fromstring(xml)
    found = root.find(f"./node[@id='{node_id}']")
    assert found is not None, f"no node {node_id!r} in\n{xml}"
    return found


def _params(xml: str, node_id: str) -> dict[str, str | None]:
    element = _node(xml, node_id).find("parameters")
    assert element is not None
    return {child.tag: child.text for child in element}


def _sources(xml: str, node_id: str) -> list[str]:
    element = _node(xml, node_id).find("sources")
    if element is None:
        return []
    return [child.get("refid", "") for child in element]


class TestConstruction:
    def test_linear_chain(self, graph):
        src = graph.read("in.zip")
        tc = graph.terrain_correction(src)
        graph.write(tc, file="out.tif")
        assert [node.id for node in graph.nodes] == [
            "Read",
            "Terrain-Correction",
            "Write",
        ]

    def test_repeated_operator_gets_suffixed_id(self, graph):
        src = graph.read("in.zip")
        first = graph.subset(src, region="0,0,100,100")
        graph.subset(first, region="0,0,50,50")
        assert [node.id for node in graph.nodes] == ["Read", "Subset", "Subset-2"]

    def test_generic_node_escape_hatch(self, graph):
        """Operators without a generated builder stay reachable."""
        src = graph.read("in.zip")
        graph.node("c2rcc.olci", src, salinity=31.0)
        assert graph["c2rcc.olci"].params == {"salinity": 31.0}

    def test_unknown_operator_rejected(self, graph):
        with pytest.raises(GraphError, match="Unknown SNAP operator"):
            graph.node("No-Such-Operator")

    def test_unknown_source_rejected(self, graph):
        with pytest.raises(GraphError, match="unknown source"):
            graph.node("Terrain-Correction", "ghost")

    def test_last_is_none_on_an_empty_graph(self, graph):
        assert graph.last is None

    def test_last_tracks_most_recent_node(self, graph):
        graph.read("in.zip")
        graph.node("Subset", "Read")
        assert graph.last == NodeRef("Subset")


class TestDefaultStripping:
    def test_default_values_are_omitted(self, graph):
        src = graph.read("in.zip")
        # demName is passed explicitly but equals SNAP's own default.
        graph.terrain_correction(src, demName="SRTM 3Sec", pixelSpacingInMeter=10.0)
        params = _params(graph.to_xml(), "Terrain-Correction")
        assert "demName" not in params
        assert params["pixelSpacingInMeter"] == "10.0"

    def test_none_values_are_omitted(self, graph):
        src = graph.read("in.zip")
        graph.terrain_correction(src, externalDEMFile=None)
        assert "externalDEMFile" not in _params(graph.to_xml(), "Terrain-Correction")

    def test_parameter_alias_is_normalised(self, graph):
        src = graph.read("in.zip")
        graph.node("Terrain-Correction", src, sourceBands=("Sigma0_VV",))
        assert "sourceBandNames" in _params(graph.to_xml(), "Terrain-Correction")


class TestSerialisation:
    def test_booleans_are_java_literals(self, graph):
        src = graph.read("in.zip")
        graph.terrain_correction(src, saveDEM=True, nodataValueAtSea=False)
        params = _params(graph.to_xml(), "Terrain-Correction")
        assert params["saveDEM"] == "true"
        assert params["nodataValueAtSea"] == "false"

    def test_sequences_are_comma_joined(self, graph):
        src = graph.read("in.zip")
        graph.node(
            "Terrain-Correction", src, sourceBandNames=("Sigma0_VV", "Sigma0_VH")
        )
        params = _params(graph.to_xml(), "Terrain-Correction")
        assert params["sourceBandNames"] == "Sigma0_VV,Sigma0_VH"

    def test_nested_pojo_parameters_become_nested_xml(self, graph):
        """The ~53 nested-POJO parameters are why the XML backend was chosen."""
        src = graph.read("in.zip")
        graph.band_maths(
            src,
            targetBandDescriptors=[
                {
                    "name": "ratio",
                    "type": "float32",
                    "expression": "Sigma0_VV / Sigma0_VH",
                }
            ],
        )
        element = _node(graph.to_xml(), "BandMaths").find(
            "parameters/targetBandDescriptors"
        )
        assert element is not None
        band = element.find("targetBandDescriptor")
        assert band is not None
        assert band.findtext("name") == "ratio"
        assert band.findtext("expression") == "Sigma0_VV / Sigma0_VH"

    def test_array_sources_are_numbered(self, graph):
        ref = graph.node("Read", node_id="ref", file="a.zip")
        sec = graph.node("Read", node_id="sec", file="b.zip")
        graph.back_geocoding(ref, sec)
        element = _node(graph.to_xml(), "Back-Geocoding").find("sources")
        assert element is not None
        assert [child.tag for child in element] == ["sourceProduct", "sourceProduct.1"]
        assert [child.get("refid") for child in element] == ["ref", "sec"]

    def test_graph_has_version_element(self, graph):
        graph.read("in.zip")
        assert ET.fromstring(graph.to_xml()).findtext("version") == "1.0"

    def test_empty_graph_cannot_serialise(self, graph):
        with pytest.raises(GraphError, match="empty graph"):
            graph.to_xml()

    def test_writes_to_path(self, graph, tmp_path):
        graph.read("in.zip")
        target = tmp_path / "graph.xml"
        xml = graph.to_xml(target)
        assert target.read_text(encoding="utf-8") == xml


class TestOrdering:
    def test_sources_precede_consumers(self, graph):
        ref = graph.node("Read", node_id="ref", file="a.zip")
        sec = graph.node("Read", node_id="sec", file="b.zip")
        coreg = graph.back_geocoding(ref, sec)
        graph.write(coreg, file="out.dim")
        order = [node.id for node in graph.topological_order()]
        assert order.index("ref") < order.index("Back-Geocoding")
        assert order.index("sec") < order.index("Back-Geocoding")
        assert order.index("Back-Geocoding") < order.index("Write")

    def test_cycle_is_detected(self, graph):
        graph.read("in.zip")
        first = graph.node("Subset", "Read")
        second = graph.node("Subset", first)
        # Force a cycle that the builder API cannot produce on its own.
        graph["Subset"].sources = [second.id]
        with pytest.raises(GraphError, match="Cycle in graph"):
            graph.topological_order()


class TestGeneratedBuilders:
    def test_defaults_are_real_python_values(self):
        """Regression guard for the old generator's unquoted output."""
        import inspect

        signature = inspect.signature(Graph.terrain_correction)
        assert signature.parameters["demName"].default == "SRTM 3Sec"
        assert signature.parameters["nodataValueAtSea"].default is True
        assert signature.parameters["pixelSpacingInMeter"].default == 0.0
        assert signature.parameters["mapProjection"].default == "WGS84(DD)"

    def test_required_parameter_is_positional(self):
        import inspect

        parameter = inspect.signature(Graph.read).parameters["file"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    def test_python_keyword_parameter_is_escaped(self, graph):
        """``RPCA-Change-Detection`` has a parameter literally named ``lambda``."""
        import inspect

        signature = inspect.signature(Graph.rpca_change_detection)
        assert "lambda_" in signature.parameters
        assert "lambda" not in signature.parameters

        src = graph.read("in.zip")
        graph.rpca_change_detection(src, lambda_=0.5)
        assert graph["RPCA-Change-Detection"].params == {"lambda": 0.5}

    def test_builder_count_matches_sar_subset(self, registry):
        expected = sum(1 for spec in registry.values() if spec.is_sar)
        from radar_snap_lib.snap_ops.codegen import method_name

        for spec in registry.values():
            if spec.is_sar:
                assert hasattr(Graph, method_name(spec.alias)), spec.alias
        assert expected > 100
