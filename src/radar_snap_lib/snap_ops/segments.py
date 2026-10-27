"""Split a pipeline around its external (Python) nodes.

JVM-free: given an already-parsed node list, find the external nodes (see
:mod:`radar_snap_lib.snap_ops.external`) and partition the pipeline into SNAP
graphs that hand over to them through BEAM-DIMAP files::

    Read -> Deburst -> DeepDespeckle -> Calibration -> Write

becomes

    graph a:  Read -> Deburst -> Write(<output_dir>/DeepDespeckle__in.dim)
    step:     DeepDespeckle: DeepDespeckle__in.dim -> <output_dir>/DeepDespeckle.dim
    graph b:  Read(DeepDespeckle.dim) -> Calibration -> Write

The external node turns into a ``Read`` that keeps its id, so the nodes after
it need no rewiring. Every file name is fixed before anything runs -- unlike
the snaphu split, all graphs can be dumped up front.
"""

from __future__ import annotations

import importlib.util
import string
from dataclasses import dataclass
from pathlib import Path

from radar_snap_lib.snap_ops.external import EXTERNAL_OPS, ExternalOp
from radar_snap_lib.snap_ops.OpsConfig import (
    SAVE_FORMAT,
    GraphConfigError,
    ParsedNode,
    ancestors,
    build_graph,
    dependency_order,
    save_write_nodes,
)
from radar_snap_lib.snap_ops.registry import Registry
from radar_snap_lib.snap_ops.snaphu import EXPORT_OP

__all__ = [
    "ExternalStep",
    "Segment",
    "check_requirements",
    "segment_xmls",
    "split_at_external",
]


@dataclass
class ExternalStep:
    """One external node, with the files it reads and writes."""

    node: ParsedNode
    op: ExternalOp
    input: Path
    output: Path
    #: The input is a hand-over file only this step needs, not a ``save:``.
    remove_input: bool


@dataclass
class Segment:
    """A SNAP graph and the external step that runs after it."""

    #: In dependency order; empty when there is nothing for SNAP to do, e.g.
    #: between two external nodes in a row.
    nodes: list[ParsedNode]
    step: ExternalStep | None


def _output_name(node: ParsedNode) -> str:
    if isinstance(node.save, str):
        return Path(node.save).stem
    return node.id


def split_at_external(
    nodes: list[ParsedNode], output_dir: Path | None
) -> list[Segment] | None:
    """Partition ``nodes`` around their external nodes.

    Returns ``None`` when there is none -- the caller runs ``nodes`` as one
    graph as before. ``save:`` Write nodes are injected into the segment that
    computes the node they save. Raises :class:`GraphConfigError` for a
    pipeline that cannot be split.
    """
    ordered = dependency_order(nodes)
    externals = [n for n in ordered if n.op in EXTERNAL_OPS]
    if not externals:
        return None
    if any(n.op == EXPORT_OP for n in nodes):
        raise GraphConfigError(
            [
                f"A pipeline with a {EXPORT_OP!r} node cannot have nodes that run "
                f"outside SNAP ({[n.id for n in externals]!r}) yet"
            ]
        )
    if output_dir is None:
        raise GraphConfigError(
            [
                f"Node(s) {[n.id for n in externals]!r} run outside SNAP and need "
                f"the config's 'output_dir' for the files they hand over"
            ]
        )

    saves = save_write_nodes([n for n in nodes if n.op not in EXTERNAL_OPS], output_dir)
    saved_files = {w.sources[0]: Path(str(w.params["file"])) for w in saves}
    outputs: dict[str, Path] = {}  # external node id -> the DIMAP it writes

    remaining = {n.id: n for n in ordered}
    pending_saves = list(saves)
    segments: list[Segment] = []

    for ext in externals:
        source_id = ext.sources[0]
        before = ancestors(source_id, remaining)

        # Nodes still to come that read from this graph other than through
        # the external node. A plain Read is fine -- it is simply read again --
        # anything else would have to be computed twice.
        kept_reads = set()
        for node in remaining.values():
            if node.id in before or node.id == ext.id:
                continue
            for source in node.sources:
                if source not in before:
                    continue
                if remaining[source].op == "Read" and not remaining[source].sources:
                    kept_reads.add(source)
                else:
                    raise GraphConfigError(
                        [
                            f"Node {node.id!r}: source {source!r} is computed "
                            f"before {ext.id!r}, which runs outside SNAP; only "
                            f"{ext.id!r} may read it (or save it with 'save:' and "
                            f"read the file)"
                        ]
                    )

        graph = [n for n in remaining.values() if n.id in before]
        graph += [w for w in pending_saves if w.sources[0] in before]
        pending_saves = [w for w in pending_saves if w.sources[0] not in before]

        if source_id in outputs:
            src_file, remove = outputs[source_id], False
        elif source_id in saved_files:
            src_file, remove = saved_files[source_id], False
        else:
            src_file, remove = output_dir / f"{ext.id}__in.dim", True
            graph.append(
                ParsedNode(
                    id=f"{ext.id}__in",
                    op="Write",
                    sources=[source_id],
                    params={"file": str(src_file), "formatName": SAVE_FORMAT},
                )
            )
        if not any(n.op == "Write" for n in graph):
            graph = []

        output = output_dir / f"{_output_name(ext)}.dim"
        outputs[ext.id] = output
        segments.append(
            Segment(
                nodes=dependency_order(graph),
                step=ExternalStep(
                    node=ext,
                    op=EXTERNAL_OPS[ext.op],
                    input=src_file,
                    output=output,
                    remove_input=remove,
                ),
            )
        )

        for node_id in before - kept_reads:
            del remaining[node_id]
        remaining[ext.id] = ParsedNode(
            id=ext.id, op="Read", sources=[], params={"file": str(output)}
        )

    rest = [*remaining.values(), *pending_saves]
    if any(n.op == "Write" for n in rest):
        segments.append(Segment(nodes=dependency_order(rest), step=None))
    return segments


def segment_xmls(segments: list[Segment], registry: Registry) -> list[str | None]:
    """Graph XML per segment, ``None`` where SNAP has nothing to run."""
    xmls: list[str | None] = []
    for letter, segment in zip(string.ascii_lowercase, segments, strict=False):
        if not segment.nodes:
            xmls.append(None)
            continue
        graph = build_graph(segment.nodes, registry, graph_id=f"radar-snap-{letter}")
        xmls.append(graph.to_xml())
    return xmls


def check_requirements(segments: list[Segment]) -> None:
    """Raise ``RuntimeError`` if an external step could not run.

    Called before the first graph runs: the graph in front of an external
    node is often the expensive part, and a missing package should not only
    surface once it has finished.
    """
    for segment in segments:
        step = segment.step
        if step is not None and importlib.util.find_spec(step.op.requires) is None:
            raise RuntimeError(step.op.install_hint)
