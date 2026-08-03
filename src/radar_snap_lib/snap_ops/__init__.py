"""Build and run ESA SNAP process graphs.

Two ways to describe a chain, one execution path.

From YAML::

    from radar_snap_lib.snap_ops import run_graph

    run_graph("pipeline.yaml", dump_xml="graph.xml")

From Python::

    from radar_snap_lib.snap_ops import Graph

    g = Graph()
    src = g.read("S1A_IW_SLC.zip")
    tc = g.terrain_correction(g.apply_orbit_file(src), pixelSpacingInMeter=10)
    g.write(tc, file="out.tif", formatName="GeoTIFF")
    g.run()

Both produce the same GPF graph XML, which SNAP's ``GraphProcessor`` executes.
"""

from radar_snap_lib.snap_ops.graph import Graph, GraphError, Node, NodeRef
from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, OpsConfig
from radar_snap_lib.snap_ops.registry import (
    OperatorSpec,
    ParamSpec,
    Registry,
    SourceSpec,
    load_registry,
)
from radar_snap_lib.snap_ops.runner import execute_xml, run_graph

__all__ = [
    "Graph",
    "GraphConfigError",
    "GraphError",
    "Node",
    "NodeRef",
    "OperatorSpec",
    "OpsConfig",
    "ParamSpec",
    "Registry",
    "SourceSpec",
    "execute_xml",
    "load_registry",
    "run_graph",
]
