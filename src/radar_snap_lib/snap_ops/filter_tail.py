"""Split the filter nodes off the end of a pipeline.

JVM-free: given an already-parsed node list, separate the filter nodes (see
:mod:`radar_snap_lib.snap_ops.filter_ops`) from what SNAP runs::

    Read -> Terrain-Correction -> Write(gamma0.tif) -> Canny -> Closing

becomes

    SNAP:     Read -> Terrain-Correction -> Write(gamma0.tif)
    Python:   gamma0.tif -> Canny -> Closing -> <output_dir>/Closing.tif

A filter reads a GeoTIFF, which it finds in one of three ways:

* its source is a ``Write`` with a GeoTIFF format: the file it writes;
* its source is a plain ``Read`` of a ``.tif``: that file, directly -- a
  pipeline of only such a ``Read`` and filters never starts SNAP;
* anything else: a GeoTIFF ``Write`` to ``<output_dir>/<source>.tif`` is added.

Every filter node no other filter reads from is written to
``<output_dir>/<id>.tif``, and any filter node marked ``save:`` too.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

from radar_snap_lib.filters.raster import FilterStep
from radar_snap_lib.snap_ops.filter_ops import FILTER_OPS
from radar_snap_lib.snap_ops.OpsConfig import (
    OUTPUT_DIR_KEY,
    GraphConfigError,
    ParsedNode,
    dependency_order,
)

__all__ = [
    "FilterChain",
    "FilterTail",
    "check_filter_requirements",
    "filter_write_nodes",
    "split_filter_tail",
]

#: ``Write`` formats a filter can read.
GEOTIFF_FORMATS = frozenset({"GeoTIFF", "GeoTIFF-BigTIFF"})
#: What the added ``Write`` uses: plain GeoTIFF stops at 4 GB.
TIFF_FORMAT = "GeoTIFF-BigTIFF"
_TIFF_SUFFIXES = frozenset({".tif", ".tiff"})
#: ``Read`` parameters that do not change what is read.
_PLAIN_READ_PARAMS = frozenset({"file", "formatName"})


@dataclass
class FilterChain:
    """The filter nodes that start from one GeoTIFF."""

    input: Path
    #: In dependency order.
    steps: list[FilterStep]
    #: Step id -> the GeoTIFF it writes.
    outputs: dict[str, Path] = field(default_factory=dict)


@dataclass
class FilterTail:
    """Everything that runs in Python after SNAP."""

    chains: list[FilterChain]

    @property
    def outputs(self) -> list[Path]:
        return [path for chain in self.chains for path in chain.outputs.values()]


def _tif(file: object) -> Path:
    """The file a GeoTIFF ``Write`` produces: SNAP adds ``.tif`` if missing."""
    path = Path(str(file))
    if path.suffix.lower() in _TIFF_SUFFIXES:
        return path
    return path.with_name(f"{path.name}.tif")


def _output_name(node: ParsedNode) -> str:
    if isinstance(node.save, str):
        return Path(node.save).stem
    return node.id


def _outputs(nodes: list[ParsedNode], output_dir: Path) -> dict[str, Path]:
    """Filter node id -> the GeoTIFF it writes: the leaves and every ``save:``."""
    filters = [n for n in nodes if n.op in FILTER_OPS]
    read_by_filters = {s for n in filters for s in n.sources}
    return {
        n.id: output_dir / f"{_output_name(n)}.tif"
        for n in filters
        if n.id not in read_by_filters or (n.save is not None and n.save is not False)
    }


def filter_write_nodes(nodes: list[ParsedNode], output_dir: Path) -> list[ParsedNode]:
    """A stand-in ``Write`` per filter output, so a drawing shows the files."""
    return [
        ParsedNode(
            id=f"{node_id}__tif",
            op="Write",
            sources=[node_id],
            params={"file": str(path), "formatName": "GeoTIFF"},
        )
        for node_id, path in _outputs(nodes, output_dir).items()
    ]


def split_filter_tail(
    nodes: list[ParsedNode], output_dir: Path | None
) -> tuple[list[ParsedNode], FilterTail | None]:
    """Separate the filter nodes from the nodes SNAP runs.

    Returns ``(nodes, None)`` when there are none. Otherwise the SNAP nodes --
    without the filters and a ``Read`` only they use, with any ``Write`` the
    filters need added -- and the :class:`FilterTail`. Raises
    :class:`GraphConfigError` for a pipeline whose filters are not at its end.
    """
    filters = [n for n in dependency_order(nodes) if n.op in FILTER_OPS]
    if not filters:
        return nodes, None

    by_id = {n.id: n for n in nodes}
    errors: list[str] = []
    if output_dir is None:
        errors.append(
            f"Filter node(s) {[n.id for n in filters]!r} write their results to "
            f"the config's {OUTPUT_DIR_KEY!r}, which is not set"
        )
    for node in nodes:
        if node.op in FILTER_OPS:
            if len(node.sources) != 1:
                errors.append(
                    f"Node {node.id!r}: a filter reads exactly one source, "
                    f"got {node.sources!r}"
                )
            elif by_id[node.sources[0]].op in FILTER_OPS and "bands" in node.params:
                errors.append(
                    f"Node {node.id!r}: 'bands' only applies to a filter that "
                    f"reads the GeoTIFF, not to one after another filter"
                )
            continue
        read = [s for s in node.sources if s in by_id and by_id[s].op in FILTER_OPS]
        if read:
            errors.append(
                f"Node {node.id!r}: reads filter node(s) {read!r}, but filters "
                f"run after SNAP and have to come at the end of the pipeline"
            )
    if errors:
        raise GraphConfigError(errors)
    assert output_dir is not None

    entries = {
        n.sources[0] for n in filters if by_id[n.sources[0]].op not in FILTER_OPS
    }
    snap_ids = {n.id for n in nodes if n.op not in FILTER_OPS}
    added: list[ParsedNode] = []
    inputs: dict[str, Path] = {}
    for entry in sorted(entries):
        node = by_id[entry]
        file = node.params.get("file")
        if node.op == "Write":
            fmt = str(node.params.get("formatName", "BEAM-DIMAP"))
            if fmt not in GEOTIFF_FORMATS:
                errors.append(
                    f"Node {entry!r}: filters read GeoTIFF, but this Write writes "
                    f"{fmt!r} -- set formatName: GeoTIFF (or GeoTIFF-BigTIFF)"
                )
                continue
            inputs[entry] = _tif(file)
        elif (
            node.op == "Read"
            and not node.sources
            and file is not None
            and Path(str(file)).suffix.lower() in _TIFF_SUFFIXES
            and set(node.params) <= _PLAIN_READ_PARAMS
        ):
            inputs[entry] = Path(str(file))
            if not any(entry in n.sources for n in nodes if n.op not in FILTER_OPS):
                snap_ids.discard(entry)
        else:
            inputs[entry] = output_dir / f"{entry}.tif"
            added.append(
                ParsedNode(
                    id=f"{entry}__tif",
                    op="Write",
                    sources=[entry],
                    params={"file": str(inputs[entry]), "formatName": TIFF_FORMAT},
                )
            )

    outputs = _outputs(nodes, output_dir)
    written = [*outputs.values(), *(n.params["file"] for n in added)]
    clashes = sorted({str(p) for p in written if written.count(p) > 1})
    if clashes:
        errors.append(f"Filter outputs would overwrite each other: {clashes!r}")
    if errors:
        raise GraphConfigError(errors)

    root: dict[str, str] = {}
    chains: dict[str, FilterChain] = {}
    for node in filters:
        source = node.sources[0]
        entry = root[node.id] = root.get(source, source)
        chain = chains.setdefault(entry, FilterChain(input=inputs[entry], steps=[]))
        params = {k: v for k, v in node.params.items() if k != "bands"}
        bands = node.params.get("bands")
        chain.steps.append(
            FilterStep(
                id=node.id,
                op=node.op,
                source=None if source in entries else source,
                params=params,
                bands=[bands] if isinstance(bands, int) else bands,
            )
        )
        if node.id in outputs:
            chain.outputs[node.id] = outputs[node.id]

    snap_nodes = [n for n in nodes if n.id in snap_ids] + added
    return snap_nodes, FilterTail(chains=list(chains.values()))


def check_filter_requirements() -> None:
    """Raise ``RuntimeError`` if the filters could not run.

    scikit-image is a dependency, so this only fails in a broken install --
    but it is cheap, and the SNAP part in front of the filters is not.
    """
    if importlib.util.find_spec("skimage") is None:
        raise RuntimeError(
            "Filter nodes need scikit-image: reinstall radar-snap-lib "
            "(in this repo: uv sync)"
        )
