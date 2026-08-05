"""Execute GPF graphs through SNAP.

This is the only module that needs the JVM.  It feeds graph XML to SNAP's own
``GraphProcessor`` -- the same path the ``gpt`` command line tool takes -- so the
whole chain streams tile by tile instead of materialising every intermediate
product.  That matters for SLC scenes, where holding each step in memory is not
an option.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omegaconf import DictConfig

from radar_snap_lib.snap_ops.OpsConfig import OpsConfig
from radar_snap_lib.snap_ops.registry import Registry

__all__ = ["execute_xml", "run_graph"]


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
    graph_io, graph_processor, string_reader, progress_monitor, console_monitor, system = _graph_classes()

    graph = graph_io.read(string_reader(xml))
    processor = graph_processor()
    monitor = progress_monitor.NULL if quiet else console_monitor(system.out)
    processor.executeGraph(graph, monitor)


def run_graph(
    config: str | Path | DictConfig | dict[str, Any],
    *,
    dump_xml: Path | str | None = None,
    registry: Registry | None = None,
    quiet: bool = False,
) -> str:
    """Validate a config, then execute it.

    Args:
        config: Path to a YAML config, a mapping, or a ``DictConfig``.
        dump_xml: Write the generated graph XML here before running.  Handy for
            debugging -- the file opens directly in SNAP Desktop.
        registry: Operator registry override, mainly for tests.
        quiet: Suppress SNAP's progress output.

    Returns:
        The graph XML that was executed.

    Raises:
        GraphConfigError: If the config does not describe a valid graph.
    """
    xml = OpsConfig.load(config, registry=registry).to_xml(dump_xml)
    execute_xml(xml, quiet=quiet)
    return xml
