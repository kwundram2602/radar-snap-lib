"""The node model shared by the YAML front end and the Python builder API.

A :class:`Graph` is a plain, JVM-free description of a SNAP processing chain.
It serialises to the same GPF graph XML that the ``gpt`` command line tool
consumes, which is what :mod:`radar_snap_lib.snap_ops.runner` executes.

Building a graph never touches SNAP, so graphs can be constructed, validated,
diffed and unit-tested without booting the JVM.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from radar_snap_lib.snap_ops.nodes import Node, NodeRef
from radar_snap_lib.snap_ops.op_funcs import OpFuncs
from radar_snap_lib.snap_ops.registry import (
    OperatorSpec,
    ParamSpec,
    Registry,
    load_registry,
)

__all__ = ["Graph", "GraphError", "Node", "NodeRef"]


class GraphError(Exception):
    """Raised when a graph is structurally invalid."""


# --------------------------------------------------------------------------- #
# Parameter serialisation (the inverse of registry.parse_default)
# --------------------------------------------------------------------------- #


def _scalar_to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _append_param(parent: ET.Element, name: str, value: Any) -> None:
    """Serialise one parameter value into ``parent`` as ``<name>...</name>``.

    Mappings and sequences-of-mappings recurse, which is how the nested POJO
    parameters (``BandMaths.targetBands``, ``Mosaic.variables``, binning
    aggregators, ...) are expressed.
    """
    element = ET.SubElement(parent, name)

    if isinstance(value, Mapping):
        for key, item in value.items():
            _append_param(element, str(key), item)
        return

    if isinstance(value, (list, tuple)):
        if any(isinstance(item, Mapping) for item in value):
            # A list of complex objects: SNAP expects one child element per
            # entry, singularised where possible (targetBands -> targetBand).
            child_name = name[:-1] if name.endswith("s") and len(name) > 1 else "item"
            for item in value:
                _append_param(element, child_name, item)
            return
        element.text = ",".join(_scalar_to_text(item) for item in value)
        return

    if value is not None:
        element.text = _scalar_to_text(value)


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #


class Graph(OpFuncs):
    """An ordered, acyclic set of SNAP operator nodes.

    Nodes are usually added through the generated builder methods (see
    :mod:`radar_snap_lib.snap_ops.op_funcs`) or by loading a YAML config, but
    :meth:`node` works for any of the operators SNAP knows about.
    """

    def __init__(
        self, *, registry: Registry | None = None, graph_id: str = "radar-snap"
    ) -> None:
        self._registry = registry if registry is not None else load_registry()
        self._nodes: dict[str, Node] = {}
        self._last: str | None = None
        self.graph_id = graph_id

    # -- introspection ----------------------------------------------------- #

    @property
    def registry(self) -> Registry:
        return self._registry

    @property
    def nodes(self) -> list[Node]:
        """Nodes in insertion order."""
        return list(self._nodes.values())

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: object) -> bool:
        return str(node_id) in self._nodes

    def __getitem__(self, node_id: str) -> Node:
        return self._nodes[node_id]

    def spec(self, op: str) -> OperatorSpec:
        """Registry entry for an operator alias."""
        try:
            return self._registry[op]
        except KeyError:
            raise GraphError(f"Unknown SNAP operator: {op!r}") from None

    # -- construction ------------------------------------------------------ #

    def _unique_id(self, base: str) -> str:
        if base not in self._nodes:
            return base
        index = 2
        while f"{base}-{index}" in self._nodes:
            index += 1
        return f"{base}-{index}"

    def _add(
        self,
        op: str,
        sources: Sequence[NodeRef | Node | str | None],
        params: Mapping[str, Any],
        *,
        node_id: str | None = None,
    ) -> NodeRef:
        """Append a node.  Used by the generated builder methods."""
        spec = self.spec(op)
        resolved: list[str] = []
        for source in sources:
            if source is None:
                continue
            key = source.id if isinstance(source, (NodeRef, Node)) else str(source)
            if key not in self._nodes:
                raise GraphError(
                    f"Node {node_id or op!r} references unknown source {key!r}"
                )
            resolved.append(key)

        cleaned = self._strip_defaults(spec, params)
        node = Node(
            id=self._unique_id(node_id or op),
            op=op,
            sources=resolved,
            params=cleaned,
        )
        self._nodes[node.id] = node
        self._last = node.id
        return node.ref

    @staticmethod
    def _strip_defaults(
        spec: OperatorSpec, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Drop values identical to the operator's own default.

        SNAP applies the same value, so the graph is unchanged and the emitted
        XML stays readable.
        """
        cleaned: dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            param: ParamSpec | None = spec.resolve_param(key)
            if param is not None:
                if param.default is not None and value == param.default:
                    continue
                key = param.name
            cleaned[key] = value
        return cleaned

    def node(
        self,
        op: str,
        *sources: NodeRef | Node | str,
        node_id: str | None = None,
        **params: Any,
    ) -> NodeRef:
        """Add a node for any SNAP operator.

        The escape hatch for operators without a generated builder method::

            g.node("c2rcc.olci", src, salinity=35.0)
        """
        return self._add(op, sources, params, node_id=node_id)

    @property
    def last(self) -> NodeRef | None:
        """The most recently added node, used as the implicit default source."""
        return NodeRef(self._last) if self._last is not None else None

    # -- ordering ---------------------------------------------------------- #

    def topological_order(self) -> list[Node]:
        """Nodes ordered so every node follows its sources.

        Raises :class:`GraphError` on a cycle.
        """
        visiting: set[str] = set()
        done: set[str] = set()
        order: list[Node] = []

        def visit(node_id: str, trail: tuple[str, ...]) -> None:
            if node_id in done:
                return
            if node_id in visiting:
                cycle = " -> ".join((*trail, node_id))
                raise GraphError(f"Cycle in graph: {cycle}")
            visiting.add(node_id)
            for source in self._nodes[node_id].sources:
                visit(source, (*trail, node_id))
            visiting.discard(node_id)
            done.add(node_id)
            order.append(self._nodes[node_id])

        for node_id in self._nodes:
            visit(node_id, ())
        return order

    # -- serialisation ----------------------------------------------------- #

    def to_element(self) -> ET.Element:
        """Build the GPF graph as an ElementTree element."""
        if not self._nodes:
            raise GraphError("Cannot serialise an empty graph")

        root = ET.Element("graph", id=self.graph_id)
        ET.SubElement(root, "version").text = "1.0"

        for node in self.topological_order():
            spec = self.spec(node.op)
            node_element = ET.SubElement(root, "node", id=node.id)
            ET.SubElement(node_element, "operator").text = node.op

            if node.sources:
                sources_element = ET.SubElement(node_element, "sources")
                self._append_sources(sources_element, spec, node.sources)

            params_element = ET.SubElement(node_element, "parameters")
            for name, value in node.params.items():
                _append_param(params_element, name, value)

        return root

    @staticmethod
    def _append_sources(
        parent: ET.Element, spec: OperatorSpec, sources: Iterable[str]
    ) -> None:
        sources = list(sources)
        if spec.takes_source_array:
            # SNAP accepts repeated <sourceProduct> elements for array slots;
            # subsequent ones conventionally get a numeric suffix.
            for index, source in enumerate(sources):
                name = "sourceProduct" if index == 0 else f"sourceProduct.{index}"
                ET.SubElement(parent, name, refid=source)
            return

        named = [src for src in spec.sources if not src.is_array]
        for index, source in enumerate(sources):
            name = (
                named[index].xml_name
                if index < len(named)
                else f"sourceProduct.{index}"
            )
            ET.SubElement(parent, name, refid=source)

    def to_xml(self, path: Path | str | None = None) -> str:
        """Serialise to GPF graph XML, optionally writing it to ``path``."""
        root = self.to_element()
        ET.indent(root, space="  ")
        xml = ET.tostring(root, encoding="unicode")
        if not xml.endswith("\n"):
            xml += "\n"
        if path is not None:
            Path(path).write_text(xml, encoding="utf-8")
        return xml

    def run(self, *, dump_xml: Path | str | None = None) -> None:
        """Execute the graph through SNAP's ``GraphProcessor``."""
        from radar_snap_lib.snap_ops.runner import execute_xml  # noqa: PLC0415

        execute_xml(self.to_xml(dump_xml))

    def __repr__(self) -> str:
        chain = " -> ".join(node.id for node in self._nodes.values())
        return f"<Graph {len(self._nodes)} nodes: {chain}>"
