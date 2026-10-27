"""Execute GPF graphs through SNAP.

This is the only module that needs the JVM. It feeds graph XML to SNAP's own
``GraphProcessor`` -- the same path the ``gpt`` command line tool takes -- so the
whole chain streams tile by tile instead of materialising every intermediate
product. That matters for SLC scenes, where holding each step in memory is not
an option.

A config containing a ``SnaphuExport``/``SnaphuImport`` pair cannot run as one
graph -- the actual phase unwrapping happens in the external ``snaphu`` binary,
between the two SNAP operators. ``run_graph`` detects that pair (see
``snap_ops.snaphu.split_at_snaphu``) and transparently runs it as three
phases: graph A through ``SnaphuExport``, the ``snaphu`` subprocess, then
graph B from two freshly injected ``Read`` nodes through the rest of the
pipeline. Configs without that pair are unaffected. See
docs/superpowers/specs/2026-08-05-snaphu-subprocess-design.md for the full
design.

The same goes for a config with nodes that run in Python (``DeepDespeckle``):
``run_graph`` splits it around them (see ``snap_ops.segments``) and runs the
SNAP graphs and the Python steps in turn, handing over BEAM-DIMAP files in
the config's ``output_dir``.
"""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omegaconf import DictConfig

from radar_snap_lib.despeckle.dimap import remove_product
from radar_snap_lib.snap_ops import snaphu
from radar_snap_lib.snap_ops.OpsConfig import (
    GraphConfigError,
    OpsConfig,
    ParsedNode,
    build_graph,
    dependency_order,
    save_write_nodes,
)
from radar_snap_lib.snap_ops.registry import Registry
from radar_snap_lib.snap_ops.segments import (
    ExternalStep,
    Segment,
    check_requirements,
    segment_xmls,
    split_at_external,
)

__all__ = ["SegmentedRunResult", "SnaphuRunResult", "execute_xml", "run_graph"]


def _graph_classes() -> tuple[Any, Any, Any, Any, Any, Any]:
    """Import the SNAP graph API, ensuring the snappy venv is on ``sys.path``."""
    from radar_snap_lib.config import ensure_esa_snappy

    ensure_esa_snappy()
    from esa_snappy import jpy  # noqa: PLC0415

    return (
        jpy.get_type("org.esa.snap.core.gpf.graph.GraphIO"),
        jpy.get_type("org.esa.snap.core.gpf.graph.GraphProcessor"),
        jpy.get_type("java.io.StringReader"),
        jpy.get_type("com.bc.ceres.core.ProgressMonitor"),
        jpy.get_type("com.bc.ceres.core.PrintWriterConciseProgressMonitor"),
        jpy.get_type("java.lang.System"),
    )


def execute_xml(xml: str, *, quiet: bool = False) -> None:
    """Parse GPF graph XML and run it.

    Args:
        xml: A complete ``<graph>`` document.
        quiet: Suppress SNAP's progress output.
    """
    (
        graph_io,
        graph_processor,
        string_reader,
        progress_monitor,
        console_monitor,
        system,
    ) = _graph_classes()

    graph = graph_io.read(string_reader(xml))
    processor = graph_processor()
    monitor = progress_monitor.NULL if quiet else console_monitor(system.out)
    processor.executeGraph(graph, monitor)


@dataclass
class SnaphuRunResult:
    """Result of a split (SnaphuExport/SnaphuImport) run.

    ``run_graph`` returns this instead of a plain ``str`` whenever the
    config contains a ``SnaphuExport``/``SnaphuImport`` pair, since there
    are two graphs to report instead of one.
    """

    xml_a: str
    xml_b: str
    snaphu_command: list[str]


@dataclass
class SegmentedRunResult:
    """Result of a run split around external (Python) nodes.

    ``run_graph`` returns this instead of a plain ``str`` whenever the config
    has a node that runs outside SNAP, such as ``DeepDespeckle``.
    """

    #: The SNAP graphs that ran, in order.
    xmls: list[str]
    #: The external steps that ran, in order, with the files they read and wrote.
    steps: list[ExternalStep]


def _lettered(path: Path, letter: str) -> Path:
    """``graph.xml`` -> ``graph.a.xml``."""
    return path.with_name(f"{path.stem}.{letter}{path.suffix}")


def _dump(xml: str, output_dir: Path | None, name: str) -> None:
    """Write an executed graph's XML beside the results.

    Called before the graph runs, not after: a run that dies half way is
    exactly when having the graph on disk is worth something.
    """
    if output_dir is None:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / name).write_text(xml, encoding="utf-8")


def _read_node(node_id: str, file_path: Path) -> ParsedNode:
    return ParsedNode(
        id=node_id, op="Read", sources=[], params={"file": str(file_path)}
    )


def _run_split(
    split: snaphu.SnaphuSplit,
    *,
    registry: Registry,
    dump_xml: Path | str | None,
    output_dir: Path | None,
    saves: list[ParsedNode],
    snaphu_bin: str,
    quiet: bool,
) -> SnaphuRunResult:
    # Before anything expensive: graph A is a full coregistration run, and a
    # missing or non-executable snaphu would otherwise only surface once it
    # had finished.
    snaphu.check_binary(snaphu_bin)

    # Each injected Write belongs to whichever half holds the node it saves.
    graph_a_ids = {n.id for n in split.graph_a}
    saves_a = [n for n in saves if n.sources[0] in graph_a_ids]
    saves_b = [n for n in saves if n.sources[0] not in graph_a_ids]

    graph_a = build_graph(
        dependency_order([*split.graph_a, *saves_a]),
        registry,
        graph_id="radar-snap-a",
    )
    xml_a = graph_a.to_xml()
    _dump(xml_a, output_dir, "graph.a.xml")
    execute_xml(xml_a, quiet=quiet)

    target_folder = Path(str(split.export_node.params["targetFolder"]))
    conf_path = snaphu.find_conf(target_folder)
    exported_product = snaphu.find_exported_product(target_folder)

    # SnaphuExport can name a CORRFILE it never wrote; snaphu would only
    # report that once per tile, in files nobody reads.
    snaphu.check_corrfile(conf_path)

    snaphu.run_snaphu(conf_path, snaphu_bin=snaphu_bin, quiet=quiet)

    unwrapped_product = snaphu.find_unwrapped_product(target_folder)

    phase_read_id = f"{split.import_node.id}__phase"
    unwrapped_read_id = f"{split.import_node.id}__unwrapped"
    import_node = ParsedNode(
        id=split.import_node.id,
        op=split.import_node.op,
        sources=[phase_read_id, unwrapped_read_id],
        params=split.import_node.params,
    )

    graph_b_nodes = [
        _read_node(phase_read_id, exported_product),
        _read_node(unwrapped_read_id, unwrapped_product),
        import_node,
        *[n for n in split.graph_b if n.id != split.import_node.id],
        *saves_b,
    ]
    graph_b = build_graph(
        dependency_order(graph_b_nodes), registry, graph_id="radar-snap-b"
    )
    xml_b = graph_b.to_xml()
    _dump(xml_b, output_dir, "graph.b.xml")
    execute_xml(xml_b, quiet=quiet)

    if dump_xml is not None:
        dump_path = Path(dump_xml)
        dump_path.with_name(f"{dump_path.stem}.a{dump_path.suffix}").write_text(
            xml_a, encoding="utf-8"
        )
        dump_path.with_name(f"{dump_path.stem}.b{dump_path.suffix}").write_text(
            xml_b, encoding="utf-8"
        )

    return SnaphuRunResult(
        xml_a=xml_a,
        xml_b=xml_b,
        snaphu_command=[snaphu_bin, *snaphu.parse_command(conf_path)],
    )


def _run_segments(
    segments: list[Segment],
    *,
    registry: Registry,
    dump_xml: Path | str | None,
    output_dir: Path | None,
    quiet: bool,
) -> SegmentedRunResult:
    # Before anything runs: the graph in front of an external node is usually
    # the expensive part.
    check_requirements(segments)

    # Every file name is known up front, so all graphs are built -- and
    # dumped -- before the first one runs.
    xmls = segment_xmls(segments, registry)
    for letter, xml in zip(string.ascii_lowercase, xmls, strict=False):
        if xml is None:
            continue
        _dump(xml, output_dir, f"graph.{letter}.xml")
        if dump_xml is not None:
            _lettered(Path(dump_xml), letter).write_text(xml, encoding="utf-8")

    for segment, xml in zip(segments, xmls, strict=True):
        if xml is not None:
            execute_xml(xml, quiet=quiet)
        step = segment.step
        if step is not None:
            step.op.run(step.input, step.output, step.op.params(step.node))
            if step.remove_input:
                remove_product(step.input)

    return SegmentedRunResult(
        xmls=[xml for xml in xmls if xml is not None],
        steps=[s.step for s in segments if s.step is not None],
    )


def run_graph(
    config: str | Path | DictConfig | dict[str, Any],
    *,
    dump_xml: Path | str | None = None,
    registry: Registry | None = None,
    snaphu_bin: str = "snaphu",
    quiet: bool = False,
    vars: Mapping[str, Any] | None = None,
) -> str | SnaphuRunResult | SegmentedRunResult:
    """Validate a config, then execute it.

    Args:
        config: Path to a YAML config, a mapping, or a ``DictConfig``.
        dump_xml: Write the generated graph XML here before running. Handy for
            debugging -- the file opens directly in SNAP Desktop. For a
            config with a ``SnaphuExport``/``SnaphuImport`` pair or an
            external node, one file per graph is written instead (suffixed
            ``.a``, ``.b``, ...).
        registry: Operator registry override, mainly for tests.
        snaphu_bin: The ``snaphu`` executable to invoke, for configs with a
            ``SnaphuExport``/``SnaphuImport`` pair. Defaults to ``snaphu``
            on ``PATH``.
        quiet: Suppress SNAP's progress output.
        vars: Values merged over the config's ``vars`` section before it is
            resolved, e.g. the input scene of one run of a shared config.

    Returns:
        The graph XML that was executed, or -- for a config with a
        ``SnaphuExport``/``SnaphuImport`` pair -- a :class:`SnaphuRunResult`
        with both graphs' XML and the ``snaphu`` command that ran, or -- for a
        config with an external node -- a :class:`SegmentedRunResult`.

    Raises:
        GraphConfigError: If the config does not describe a valid graph.
        RuntimeError: If the ``snaphu`` binary is missing or not
            executable (checked before any graph runs), or if the
            ``snaphu`` subprocess itself fails, or if an external node's
            Python package is not installed (also checked before any graph
            runs).
    """
    ops_config = OpsConfig.load(config, registry=registry, vars=vars)
    nodes = ops_config.parse()
    errors = ops_config.validate(nodes)
    if errors:
        raise GraphConfigError(errors, ops_config.source)

    output_dir = ops_config.output_dir
    saves = save_write_nodes(nodes, output_dir) if output_dir is not None else []

    segments = split_at_external(nodes, output_dir)
    if segments is not None:
        return _run_segments(
            segments,
            registry=ops_config.registry,
            dump_xml=dump_xml,
            output_dir=output_dir,
            quiet=quiet,
        )

    split = snaphu.split_at_snaphu(nodes)
    if split is not None:
        return _run_split(
            split,
            registry=ops_config.registry,
            dump_xml=dump_xml,
            output_dir=output_dir,
            saves=saves,
            snaphu_bin=snaphu_bin,
            quiet=quiet,
        )

    xml = build_graph(dependency_order([*nodes, *saves]), ops_config.registry).to_xml(
        dump_xml
    )
    _dump(xml, output_dir, "graph.xml")
    execute_xml(xml, quiet=quiet)
    return xml
