"""YAML front end for SNAP process graphs.

A config describes one processing chain as a mapping of node id to parameters::

    pipeline:
      Read:
        file: S1A_IW_SLC.zip
      Apply-Orbit-File: {}
      Terrain-Correction:
        demName: Copernicus 30m Global DEM
      Write:
        file: out.tif
        formatName: GeoTIFF

The key is the node id and doubles as the operator alias.  ``sources`` defaults
to the previous node, so a linear chain needs no wiring at all.  Two reserved
keys cover everything else:

``op``
    Operator alias, when the node id is not the alias (needed to use one
    operator twice).
``sources``
    Explicit upstream node id, or a list of them, for multi-input operators.

::

    pipeline:
      ref:   {op: Read, file: a.zip}
      sec:   {op: Read, file: b.zip}
      coreg: {op: Back-Geocoding, sources: [ref, sec], demName: SRTM 3Sec}
      Write: {file: ifg.dim}

Neither reserved key can collide with a real parameter: no SNAP operator has a
parameter named ``op`` or ``sources``.

Validation runs entirely against the committed operator registry, so a config
can be checked without SNAP installed.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omegaconf import DictConfig, OmegaConf

from radar_snap_lib._omegaconf import _plain, _register_resolvers, _suggest
from radar_snap_lib.snap_ops.graph import Graph
from radar_snap_lib.snap_ops.registry import OperatorSpec, Registry, load_registry

__all__ = [
    "GraphConfigError",
    "OpsConfig",
    "ParsedNode",
    "build_graph",
    "dependency_order",
    "save_write_nodes",
]

#: Node-level keys that are structure rather than operator parameters.
RESERVED_KEYS = frozenset({"op", "sources", "save"})

PIPELINE_KEY = "pipeline"
OUTPUT_DIR_KEY = "output_dir"

#: Format for saved intermediates -- BEAM-DIMAP is SNAP-native, so the
#: products open directly in SNAP Desktop, which is the point of saving them.
SAVE_FORMAT = "BEAM-DIMAP"


class GraphConfigError(Exception):
    """Raised when a config does not describe a valid graph.

    Carries every problem found, not just the first.
    """

    def __init__(self, errors: list[str], source: str | None = None) -> None:
        self.errors = errors
        self.source = source
        location = f" in {source}" if source else ""
        body = "\n".join(f"  - {error}" for error in errors)
        plural = "s" if len(errors) != 1 else ""
        super().__init__(f"{len(errors)} problem{plural}{location}:\n{body}")


def dependency_order(nodes: list[ParsedNode]) -> list[ParsedNode]:
    """Order nodes so each one follows its sources.

    A config may reference a node defined further down the file; the graph
    needs its sources to exist before the node is added. Sources not present
    in ``nodes`` are skipped rather than erroring, so this also works on
    partial node lists (e.g. one half of a SnaphuExport/SnaphuImport split).
    """
    by_id = {node.id: node for node in nodes}
    done: set[str] = set()
    order: list[ParsedNode] = []

    def visit(node_id: str) -> None:
        if node_id in done:
            return
        done.add(node_id)
        for source in by_id[node_id].sources:
            if source in by_id:
                visit(source)
        order.append(by_id[node_id])

    for node in nodes:
        visit(node.id)
    return order


def build_graph(
    nodes: list[ParsedNode], registry: Registry, *, graph_id: str = "radar-snap"
) -> Graph:
    """Turn an already-ordered node list into a :class:`Graph`.

    ``nodes`` must already be in dependency order (see
    :func:`dependency_order`) -- each node's sources must already be present
    in the graph by the time it is added.
    """
    graph = Graph(registry=registry, graph_id=graph_id)
    for node in nodes:
        graph._add(node.op, node.sources, node.params, node_id=node.id)
    return graph


def save_write_nodes(nodes: list[ParsedNode], output_dir: Path) -> list[ParsedNode]:
    """Build a ``Write`` node for every node marked ``save:``.

    The ids follow the same ``<node>__<role>`` convention the runner already
    uses when it injects reads around the snaphu split.

    Callers must inject these into the graph they are about to build, not into
    the parsed node list: a split config validates that every node is reachable
    from one side of the ``SnaphuExport``/``SnaphuImport`` pair, and these
    ``Write`` nodes are reachable from neither.
    """
    extra: list[ParsedNode] = []
    for node in nodes:
        if node.save is None or node.save is False:
            continue
        name = node.id if node.save is True else str(node.save)
        extra.append(
            ParsedNode(
                id=f"{node.id}__save",
                op="Write",
                sources=[node.id],
                params={
                    "file": str(output_dir / f"{Path(name).stem}.dim"),
                    "formatName": SAVE_FORMAT,
                },
            )
        )
    return extra


@dataclass
class ParsedNode:
    """One pipeline entry after structural parsing, before validation."""

    id: str
    op: str
    sources: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    #: ``True`` to write this node's output under the config's
    #: ``output_dir`` named after the node, or a string to name the file.
    save: bool | str | None = None


class OpsConfig:
    """A loaded and (optionally) validated process-graph config."""

    def __init__(
        self,
        config: DictConfig,
        *,
        registry: Registry | None = None,
        source: str | None = None,
    ) -> None:
        self.config = config
        self.registry = registry if registry is not None else load_registry()
        self.source = source

    # -- loading ----------------------------------------------------------- #

    @classmethod
    def load(
        cls,
        config: str | Path | DictConfig | dict[str, Any],
        *,
        registry: Registry | None = None,
        source: str | None = None,
    ) -> OpsConfig:
        """Load a config from a YAML path, a mapping, or an existing DictConfig."""
        _register_resolvers()
        if isinstance(config, (str, Path)):
            path = Path(config)
            if not path.is_file():
                raise GraphConfigError(
                    [f"Config not found: {path}"], source or str(path)
                )
            source = source or str(path)
            loaded = OmegaConf.load(path)
        elif isinstance(config, DictConfig):
            loaded = config
        else:
            loaded = OmegaConf.create(config)

        if not isinstance(loaded, DictConfig):
            raise GraphConfigError(["Config root must be a mapping"], source)
        return cls(loaded, registry=registry, source=source)

    @property
    def output_dir(self) -> Path | None:
        """Where the graph XML and any saved intermediates are written.

        Optional: without it a run behaves exactly as before, writing no XML.
        Marking a node ``save:`` without it is a validation error.
        """
        raw = _plain(self.config.get(OUTPUT_DIR_KEY))
        return Path(str(raw)) if raw is not None else None

    # -- structural parsing ------------------------------------------------ #

    def parse(self) -> list[ParsedNode]:
        """Turn the pipeline mapping into :class:`ParsedNode` objects.

        Resolves OmegaConf interpolations and applies the implicit defaults for
        ``op`` (the node id) and ``sources`` (the previous node).
        """
        if PIPELINE_KEY not in self.config:
            raise GraphConfigError(
                [f"Config has no {PIPELINE_KEY!r} section"], self.source
            )
        pipeline = self.config[PIPELINE_KEY]
        if not isinstance(pipeline, DictConfig):
            raise GraphConfigError(
                [f"{PIPELINE_KEY!r} must be a mapping of node id to parameters"],
                self.source,
            )

        nodes: list[ParsedNode] = []
        previous: str | None = None
        for key in pipeline:
            node_id = str(key)
            body = _plain(pipeline[key]) or {}
            if not isinstance(body, dict):
                raise GraphConfigError(
                    [
                        f"Node {node_id!r}: body must be a mapping or empty, "
                        f"got {type(body).__name__}"
                    ],
                    self.source,
                )

            op = str(body.get("op", node_id))
            raw_sources = body.get("sources")
            if raw_sources is None:
                sources = [previous] if previous is not None else []
            elif isinstance(raw_sources, str):
                sources = [raw_sources]
            elif isinstance(raw_sources, (list, tuple)):
                sources = [str(item) for item in raw_sources]
            else:
                raise GraphConfigError(
                    [f"Node {node_id!r}: 'sources' must be a node id or a list"],
                    self.source,
                )

            save = body.get("save")
            if save is not None and not isinstance(save, (bool, str)):
                raise GraphConfigError(
                    [
                        f"Node {node_id!r}: 'save' must be true or a file name, "
                        f"got {type(save).__name__}"
                    ],
                    self.source,
                )

            params = {k: v for k, v in body.items() if k not in RESERVED_KEYS}
            nodes.append(
                ParsedNode(
                    id=node_id,
                    op=op,
                    sources=[s for s in sources if s is not None],
                    params=params,
                    save=save,
                )
            )
            previous = node_id
        return nodes

    # -- validation -------------------------------------------------------- #

    def validate(self, nodes: list[ParsedNode] | None = None) -> list[str]:
        """Check the config against the operator registry.

        Returns every problem found.  An empty list means the config is valid.
        """
        nodes = self.parse() if nodes is None else nodes
        errors: list[str] = []
        known_ids = {node.id for node in nodes}

        if not nodes:
            return [f"{PIPELINE_KEY!r} is empty"]

        saved = [n.id for n in nodes if n.save is not None and n.save is not False]
        if saved and self.output_dir is None:
            errors.append(
                f"Node(s) {saved!r} use 'save' but the config has no "
                f"{OUTPUT_DIR_KEY!r} to write them to"
            )

        for node in nodes:
            spec = self.registry.get(node.op)
            if spec is None:
                errors.append(
                    f"Node {node.id!r}: unknown operator {node.op!r}."
                    f"{_suggest(node.op, self.registry)}"
                )
                continue

            errors.extend(self._check_sources(node, spec, known_ids))
            errors.extend(self._check_params(node, spec))

        errors.extend(self._check_acyclic(nodes))

        if not any(node.op == "Write" for node in nodes):
            warnings.warn(
                "Pipeline has no 'Write' node; GraphProcessor will produce no output.",
                stacklevel=2,
            )
        return errors

    def _check_sources(
        self, node: ParsedNode, spec: OperatorSpec, known_ids: set[str]
    ) -> list[str]:
        errors: list[str] = []
        for source in node.sources:
            if source not in known_ids:
                errors.append(
                    f"Node {node.id!r}: unknown source {source!r}."
                    f"{_suggest(source, known_ids)}"
                )
            elif source == node.id:
                errors.append(f"Node {node.id!r}: cannot be its own source")

        count = len(node.sources)
        minimum, maximum = spec.min_sources, spec.max_sources
        if count < minimum:
            errors.append(
                f"Node {node.id!r}: operator {spec.alias!r} needs at least "
                f"{minimum} source(s), got {count}"
            )
        elif maximum is not None and count > maximum:
            errors.append(
                f"Node {node.id!r}: operator {spec.alias!r} accepts at most "
                f"{maximum} source(s), got {count}"
            )
        return errors

    def _check_params(self, node: ParsedNode, spec: OperatorSpec) -> list[str]:
        errors: list[str] = []
        provided: set[str] = set()

        for key, value in node.params.items():
            param = spec.resolve_param(key)
            if param is None:
                errors.append(
                    f"Node {node.id!r}: operator {spec.alias!r} has no parameter "
                    f"{key!r}.{_suggest(key, spec.params)}"
                )
                continue
            provided.add(param.name)

            if param.value_set:
                candidates = value if isinstance(value, (list, tuple)) else [value]
                for item in candidates:
                    if item is None:
                        continue
                    if str(item) not in param.value_set:
                        allowed = ", ".join(param.value_set)
                        errors.append(
                            f"Node {node.id!r}: {key}={item!r} is not valid. "
                            f"Allowed: {allowed}"
                        )

        for param in spec.params.values():
            if param.required and param.name not in provided:
                errors.append(
                    f"Node {node.id!r}: operator {spec.alias!r} requires "
                    f"parameter {param.name!r}"
                )
        return errors

    @staticmethod
    def _check_acyclic(nodes: list[ParsedNode]) -> list[str]:
        by_id = {node.id: node for node in nodes}
        state: dict[str, int] = {}

        def visit(node_id: str, trail: tuple[str, ...]) -> str | None:
            if state.get(node_id) == 2:
                return None
            if state.get(node_id) == 1:
                return " -> ".join((*trail, node_id))
            state[node_id] = 1
            for source in by_id[node_id].sources:
                if source in by_id:
                    cycle = visit(source, (*trail, node_id))
                    if cycle is not None:
                        return cycle
            state[node_id] = 2
            return None

        for node in nodes:
            cycle = visit(node.id, ())
            if cycle is not None:
                return [f"Cycle in pipeline: {cycle}"]
        return []

    # -- graph construction ------------------------------------------------ #

    def to_graph(self, *, graph_id: str = "radar-snap") -> Graph:
        """Validate the config and build the corresponding :class:`Graph`."""
        nodes = self.parse()
        errors = self.validate(nodes)
        if errors:
            raise GraphConfigError(errors, self.source)

        return build_graph(dependency_order(nodes), self.registry, graph_id=graph_id)

    @staticmethod
    def _dependency_order(nodes: list[ParsedNode]) -> list[ParsedNode]:
        """Order nodes so each one follows its sources.

        Kept as a thin wrapper around the module-level :func:`dependency_order`
        for existing internal call sites.
        """
        return dependency_order(nodes)

    def to_xml(self, path: Path | str | None = None) -> str:
        """Validate and serialise straight to GPF graph XML."""
        return self.to_graph().to_xml(path)
