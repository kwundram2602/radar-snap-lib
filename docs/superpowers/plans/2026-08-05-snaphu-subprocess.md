# Snaphu Subprocess Step Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `run_graph()` (and `radar-snap run`) execute a pipeline config that contains a `SnaphuExport` → `SnaphuImport` pair end-to-end, by transparently splitting it into two SNAP graph executions joined by a `snaphu` CLI subprocess call, and extend `examples/s1_slc_interferogram.yaml` to the full chain through displacement + terrain correction.

**Architecture:** A new JVM-free module `snap_ops/snaphu.py` detects the `SnaphuExport`/`SnaphuImport` pair in a parsed pipeline, validates it, and provides the file-discovery and subprocess helpers. `runner.py::run_graph()` uses these to run graph A (through `SnaphuExport`) via the existing `GraphProcessor` path, shell out to `snaphu`, then run graph B (from two synthetic `Read` nodes replacing the `SnaphuExport` → `SnaphuImport` edge) via a second `GraphProcessor` call. No new YAML syntax; existing configs without a `SnaphuExport` node are unaffected.

**Tech Stack:** Python 3.13, OmegaConf (YAML config), `esa_snappy`/JVM (`GraphProcessor`) for graph execution, stdlib `subprocess` for the `snaphu` CLI, `pytest` (+ `pytest.mark.snap` for JVM tests).

## Global Constraints

- No new reserved YAML keys or DSL node types — confirmed decision from brainstorming (design doc, Non-goals section).
- `run_graph()` for configs without a `SnaphuExport` node must behave byte-for-byte as before — verified by the existing `tests/test_runner.py` and `tests/test_ops_config.py` suites staying green throughout.
- Every new function needs JVM-free unit test coverage except the parts that call `GraphProcessor.executeGraph` directly, per the project's existing pattern of validating everything off `operators.json` without SNAP running (see `snap_ops/OpsConfig.py` module docstring).
- Follow the codebase's `from __future__ import annotations` + full type hints + dataclass style throughout (see `OpsConfig.py`, `graph.py`, `nodes.py`).
- `git commit` after every task (see Task Structure below); never batch multiple tasks into one commit.
- Design doc: [docs/superpowers/specs/2026-08-05-snaphu-subprocess-design.md](../specs/2026-08-05-snaphu-subprocess-design.md) — read it before starting; it has the confirmed-from-SNAP facts (conf-line format, `UnwPhase_*.snaphu.hdr` naming, two-source `SnaphuImport` wiring) that every task below relies on.

---

### Task 1: Extract a reusable `build_graph` helper from `OpsConfig.to_graph`

Both the existing single-graph path and the new split path need to turn an
ordered `list[ParsedNode]` into a `Graph`. Extracting this now means the
split path (Task 6) doesn't duplicate it.

**Files:**
- Modify: `src/radar_snap_lib/snap_ops/OpsConfig.py:311-321` (the `to_graph` method)
- Test: `tests/test_ops_config.py`

**Interfaces:**
- Produces: `radar_snap_lib.snap_ops.OpsConfig.dependency_order(nodes: list[ParsedNode]) -> list[ParsedNode]` and `radar_snap_lib.snap_ops.OpsConfig.build_graph(nodes: list[ParsedNode], registry: Registry, *, graph_id: str = "radar-snap") -> Graph` — both module-level functions, importable from `radar_snap_lib.snap_ops.OpsConfig`. (`OpsConfig._dependency_order` stays as a thin wrapper delegating to `dependency_order`, so existing internal call sites and tests referencing it keep working — but every *new* cross-module call site in this plan uses the public `dependency_order`, not the private method, to keep `ruff`'s private-member-access check (`SLF001`) clean.)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ops_config.py`, inside `class TestGraphConstruction:`:

```python
    def test_build_graph_matches_to_graph(self, registry):
        from radar_snap_lib.snap_ops.OpsConfig import build_graph, dependency_order

        config = _config(LINEAR, registry)
        nodes = dependency_order(config.parse())
        assert build_graph(nodes, registry).to_xml() == config.to_graph().to_xml()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ops_config.py::TestGraphConstruction::test_build_graph_matches_to_graph -v`
Expected: FAIL with `ImportError: cannot import name 'build_graph'`

- [ ] **Step 3: Add `dependency_order` and `build_graph`, refactor `to_graph` and `_dependency_order` to use them**

In `src/radar_snap_lib/snap_ops/OpsConfig.py`, add these two module-level functions above the `OpsConfig` class (after the `GraphConfigError` class, before `@dataclass class ParsedNode`):

```python
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
```

Then replace the body of `OpsConfig.to_graph` (lines 311-321) with:

```python
    def to_graph(self, *, graph_id: str = "radar-snap") -> Graph:
        """Validate the config and build the corresponding :class:`Graph`."""
        nodes = self.parse()
        errors = self.validate(nodes)
        if errors:
            raise GraphConfigError(errors, self.source)

        return build_graph(dependency_order(nodes), self.registry, graph_id=graph_id)
```

And replace the body of the existing `OpsConfig._dependency_order` staticmethod
(directly below `to_graph`) so it delegates instead of duplicating the logic:

```python
    @staticmethod
    def _dependency_order(nodes: list[ParsedNode]) -> list[ParsedNode]:
        """Order nodes so each one follows its sources.

        Kept as a thin wrapper around the module-level :func:`dependency_order`
        for existing internal call sites.
        """
        return dependency_order(nodes)
```

Add `"build_graph"` and `"dependency_order"` to the module's `__all__` list at the top of the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ops_config.py -v`
Expected: all PASS (the new test plus every existing test in the file, unchanged)

- [ ] **Step 5: Lint check**

Run: `uv run ruff check src/radar_snap_lib/snap_ops/OpsConfig.py`
Expected: no errors (in particular, no `SLF001` -- `to_graph` now calls the public `dependency_order`, not `self._dependency_order`)

- [ ] **Step 6: Commit**

```bash
git add src/radar_snap_lib/snap_ops/OpsConfig.py tests/test_ops_config.py
git commit -m "refactor: extract build_graph and dependency_order from OpsConfig.to_graph"
```

---

### Task 2: `split_at_snaphu` — detect and validate the split, happy path

**Files:**
- Create: `src/radar_snap_lib/snap_ops/snaphu.py`
- Test: `tests/test_snaphu.py`

**Interfaces:**
- Consumes: `radar_snap_lib.snap_ops.OpsConfig.ParsedNode`, `GraphConfigError`.
- Produces: `radar_snap_lib.snap_ops.snaphu.SnaphuSplit` (dataclass with fields `graph_a: list[ParsedNode]`, `export_node: ParsedNode`, `import_node: ParsedNode`, `graph_b: list[ParsedNode]`), `radar_snap_lib.snap_ops.snaphu.split_at_snaphu(nodes: list[ParsedNode]) -> SnaphuSplit | None`, constants `EXPORT_OP = "SnaphuExport"`, `IMPORT_OP = "SnaphuImport"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_snaphu.py`:

```python
"""Tests for the SnaphuExport/SnaphuImport split detection."""

from __future__ import annotations

import pytest

from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, ParsedNode
from radar_snap_lib.snap_ops.snaphu import split_at_snaphu


def _node(id_: str, op: str, sources: list[str] | None = None) -> ParsedNode:
    return ParsedNode(id=id_, op=op, sources=sources or [], params={})


LINEAR_WITH_SNAPHU = [
    _node("read", "Read"),
    _node("goldstein", "GoldsteinPhaseFiltering", ["read"]),
    _node("snaphu_export", "SnaphuExport", ["goldstein"]),
    _node("snaphu_import", "SnaphuImport", ["snaphu_export"]),
    _node("terrain_correction", "Terrain-Correction", ["snaphu_import"]),
    _node("write", "Write", ["terrain_correction"]),
]


class TestNoSplit:
    def test_returns_none_without_snaphu_export(self):
        nodes = [_node("read", "Read"), _node("write", "Write", ["read"])]
        assert split_at_snaphu(nodes) is None


class TestHappyPath:
    def test_splits_into_graph_a_and_graph_b(self):
        split = split_at_snaphu(LINEAR_WITH_SNAPHU)
        assert split is not None
        assert [n.id for n in split.graph_a] == ["read", "goldstein", "snaphu_export"]
        assert split.export_node.id == "snaphu_export"
        assert split.import_node.id == "snaphu_import"
        assert [n.id for n in split.graph_b] == [
            "snaphu_import",
            "terrain_correction",
            "write",
        ]


class TestValidationErrors:
    def test_multiple_snaphu_export_nodes(self):
        nodes = [
            *LINEAR_WITH_SNAPHU,
            _node("snaphu_export_2", "SnaphuExport", ["read"]),
        ]
        with pytest.raises(GraphConfigError, match="exactly one 'SnaphuExport'"):
            split_at_snaphu(nodes)

    def test_missing_snaphu_import(self):
        nodes = [_node("read", "Read"), _node("snaphu_export", "SnaphuExport", ["read"])]
        with pytest.raises(GraphConfigError, match="exactly one 'SnaphuImport'"):
            split_at_snaphu(nodes)

    def test_multiple_snaphu_import_nodes(self):
        nodes = [
            *LINEAR_WITH_SNAPHU,
            _node("snaphu_import_2", "SnaphuImport", ["snaphu_export"]),
        ]
        with pytest.raises(GraphConfigError, match="exactly one 'SnaphuImport'"):
            split_at_snaphu(nodes)

    def test_import_not_sourced_from_export(self):
        nodes = [
            _node("read", "Read"),
            _node("snaphu_export", "SnaphuExport", ["read"]),
            _node("snaphu_import", "SnaphuImport", ["read"]),
        ]
        with pytest.raises(GraphConfigError, match="must list 'snaphu_export'"):
            split_at_snaphu(nodes)

    def test_import_has_extra_sources(self):
        nodes = [
            _node("read", "Read"),
            _node("snaphu_export", "SnaphuExport", ["read"]),
            _node("snaphu_import", "SnaphuImport", ["snaphu_export", "read"]),
        ]
        with pytest.raises(GraphConfigError, match="only 'snaphu_export'"):
            split_at_snaphu(nodes)

    def test_node_referenced_from_both_halves(self):
        nodes = [
            _node("read", "Read"),
            _node("snaphu_export", "SnaphuExport", ["read"]),
            _node("snaphu_import", "SnaphuImport", ["snaphu_export"]),
            # 'stray' feeds both a graph_a node and a graph_b node directly.
            _node("stray", "Read"),
            _node("write", "Write", ["snaphu_import", "stray"]),
        ]
        with pytest.raises(GraphConfigError, match="stray"):
            split_at_snaphu(nodes)

    def test_node_reachable_from_neither_half(self):
        nodes = [
            *LINEAR_WITH_SNAPHU,
            _node("orphan", "Read"),
        ]
        with pytest.raises(GraphConfigError, match="orphan"):
            split_at_snaphu(nodes)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_snaphu.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'radar_snap_lib.snap_ops.snaphu'`

- [ ] **Step 3: Implement `split_at_snaphu`**

Create `src/radar_snap_lib/snap_ops/snaphu.py`:

```python
"""SnaphuExport -> [snaphu CLI] -> SnaphuImport split.

SNAP's ``GraphProcessor`` runs one graph per JVM call and cannot shell out
mid-graph. The standard InSAR unwrapping workflow needs the ``snaphu``
binary between ``SnaphuExport`` and ``SnaphuImport``, so a pipeline
containing that pair has to become two independent graph executions joined
by a subprocess call. This module handles the JVM-free half of that: given
an already-parsed node list, detect the pair, validate the split is
well-formed, and partition the nodes into the two graphs.

See docs/superpowers/specs/2026-08-05-snaphu-subprocess-design.md for the
full design, including how the facts this module encodes (the two-source
``SnaphuImport`` wiring, the ``snaphu -f snaphu.conf ...`` conf-file line,
the ``UnwPhase_*.snaphu.hdr`` output naming) were confirmed against SNAP's
own bundled reference graphs and tool-adapter templates.
"""

from __future__ import annotations

from dataclasses import dataclass

from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, ParsedNode

__all__ = ["EXPORT_OP", "IMPORT_OP", "SnaphuSplit", "split_at_snaphu"]

EXPORT_OP = "SnaphuExport"
IMPORT_OP = "SnaphuImport"


@dataclass
class SnaphuSplit:
    """A pipeline partitioned at its SnaphuExport/SnaphuImport boundary."""

    graph_a: list[ParsedNode]
    export_node: ParsedNode
    import_node: ParsedNode
    graph_b: list[ParsedNode]


def _ancestors(node_id: str, by_id: dict[str, ParsedNode]) -> set[str]:
    seen: set[str] = set()

    def visit(current: str) -> None:
        if current in seen or current not in by_id:
            return
        seen.add(current)
        for source in by_id[current].sources:
            visit(source)

    visit(node_id)
    return seen


def _descendants(node_id: str, by_id: dict[str, ParsedNode]) -> set[str]:
    children: dict[str, list[str]] = {n: [] for n in by_id}
    for node in by_id.values():
        for source in node.sources:
            if source in children:
                children[source].append(node.id)

    seen: set[str] = set()

    def visit(current: str) -> None:
        if current in seen or current not in by_id:
            return
        seen.add(current)
        for child in children[current]:
            visit(child)

    visit(node_id)
    return seen


def split_at_snaphu(nodes: list[ParsedNode]) -> SnaphuSplit | None:
    """Detect and validate a SnaphuExport/SnaphuImport split.

    Returns ``None`` when there is no ``SnaphuExport`` node -- the common
    case, meaning the caller should run ``nodes`` as one graph as before.
    Raises :class:`GraphConfigError` for any malformed split.
    """
    exports = [n for n in nodes if n.op == EXPORT_OP]
    if not exports:
        return None

    errors: list[str] = []
    if len(exports) != 1:
        errors.append(
            f"A pipeline must have exactly one {EXPORT_OP!r} node, "
            f"found {len(exports)}: {[n.id for n in exports]}"
        )

    imports = [n for n in nodes if n.op == IMPORT_OP]
    if len(imports) != 1:
        errors.append(
            f"A pipeline with a {EXPORT_OP!r} node must have exactly one "
            f"{IMPORT_OP!r} node, found {len(imports)}: {[n.id for n in imports]}"
        )

    if errors:
        raise GraphConfigError(errors)

    export_node = exports[0]
    import_node = imports[0]

    if export_node.id not in import_node.sources:
        raise GraphConfigError(
            [
                f"Node {import_node.id!r}: a {IMPORT_OP!r} node must list "
                f"{export_node.id!r} in its sources to be wired to the matching "
                f"{EXPORT_OP!r} node"
            ]
        )
    if import_node.sources != [export_node.id]:
        raise GraphConfigError(
            [
                f"Node {import_node.id!r}: sources must be only "
                f"{export_node.id!r} -- the runner injects the two real "
                f"SnaphuImport inputs (the re-read export and the unwrapped "
                f"result) itself, got {import_node.sources!r}"
            ]
        )

    by_id = {n.id: n for n in nodes}
    graph_a_ids = _ancestors(export_node.id, by_id)
    graph_b_ids = _descendants(import_node.id, by_id)

    overlap = graph_a_ids & graph_b_ids
    if overlap:
        raise GraphConfigError(
            [
                f"Node(s) {sorted(overlap)!r} are reachable from both sides of "
                f"the {EXPORT_OP!r}/{IMPORT_OP!r} split, which is not supported"
            ]
        )

    all_ids = {n.id for n in nodes}
    uncovered = all_ids - graph_a_ids - graph_b_ids
    if uncovered:
        raise GraphConfigError(
            [
                f"Node(s) {sorted(uncovered)!r} are not reachable from either "
                f"side of the {EXPORT_OP!r}/{IMPORT_OP!r} split"
            ]
        )

    for node_id in graph_b_ids:
        if node_id == import_node.id:
            continue
        node = by_id[node_id]
        crossing = [s for s in node.sources if s in graph_a_ids]
        if crossing:
            raise GraphConfigError(
                [
                    f"Node {node.id!r}: sources {crossing!r} cross from the "
                    f"pre-snaphu half of the pipeline into the post-snaphu half "
                    f"directly, which is not supported -- only "
                    f"{IMPORT_OP!r} may reference {EXPORT_OP!r}"
                ]
            )

    graph_a = [n for n in nodes if n.id in graph_a_ids]
    graph_b = [n for n in nodes if n.id in graph_b_ids]
    return SnaphuSplit(
        graph_a=graph_a, export_node=export_node, import_node=import_node, graph_b=graph_b
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_snaphu.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/radar_snap_lib/snap_ops/snaphu.py tests/test_snaphu.py
git commit -m "feat: detect and validate the SnaphuExport/SnaphuImport split"
```

---

### Task 3: `parse_command` — extract the CLI invocation from `snaphu.conf`

**Files:**
- Modify: `src/radar_snap_lib/snap_ops/snaphu.py`
- Test: `tests/test_snaphu.py`

**Interfaces:**
- Produces: `radar_snap_lib.snap_ops.snaphu.parse_command(conf_path: Path) -> list[str]`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_snaphu.py`:

```python
from pathlib import Path

from radar_snap_lib.snap_ops.snaphu import parse_command

CONF_TEXT = """\
# Configuration file for SNAPHU
#
# Recommended command line call to snaphu (assuming this file is in the
# working directory):
# snaphu -f snaphu.conf Phase_ifg_srd_02Apr2026_13Jan2024.snaphu.img 500

STATCOSTMODE  DEFO
INITMETHOD    MCF
"""


class TestParseCommand:
    def test_extracts_the_recommended_command(self, tmp_path: Path):
        conf = tmp_path / "snaphu.conf"
        conf.write_text(CONF_TEXT, encoding="utf-8")

        assert parse_command(conf) == [
            "-f",
            "snaphu.conf",
            "Phase_ifg_srd_02Apr2026_13Jan2024.snaphu.img",
            "500",
        ]

    def test_raises_when_no_command_line_found(self, tmp_path: Path):
        conf = tmp_path / "snaphu.conf"
        conf.write_text("STATCOSTMODE  DEFO\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="no 'snaphu -f snaphu.conf'"):
            parse_command(conf)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_snaphu.py::TestParseCommand -v`
Expected: FAIL with `ImportError: cannot import name 'parse_command'`

- [ ] **Step 3: Implement `parse_command`**

Add to `src/radar_snap_lib/snap_ops/snaphu.py` (add `Path` to imports: `from pathlib import Path`; add `"parse_command"` to `__all__`):

```python
_COMMAND_MARKER = "snaphu -f snaphu.conf"


def parse_command(conf_path: Path) -> list[str]:
    """Extract the recommended ``snaphu`` invocation from a ``snaphu.conf``.

    SNAP's ``SnaphuExport`` writes the exact recommended command line as a
    comment near the top of the file. This is a direct port of the same
    scan SNAP Desktop's own "External Tools" adapter does (see
    ``~/.snap/auxdata/tool-adapters/Snaphu-unwrapping/*.vm`` on a machine
    with SNAP installed): look at the first 10 lines for one containing
    ``'snaphu -f snaphu.conf'``, then return everything from ``-f`` onward,
    split on whitespace.
    """
    with conf_path.open(encoding="utf-8") as handle:
        lines = [next(handle, "") for _ in range(10)]

    for line in lines:
        marker_index = line.find(_COMMAND_MARKER)
        if marker_index != -1:
            exec_line = line[marker_index + len("snaphu ") :]
            return exec_line.split()

    raise RuntimeError(
        f"{conf_path}: no {_COMMAND_MARKER!r} line found in the first 10 lines"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_snaphu.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/radar_snap_lib/snap_ops/snaphu.py tests/test_snaphu.py
git commit -m "feat: parse the recommended snaphu command from snaphu.conf"
```

---

### Task 4: File-discovery helpers (`find_conf`, `find_exported_product`, `find_unwrapped_product`)

**Files:**
- Modify: `src/radar_snap_lib/snap_ops/snaphu.py`
- Test: `tests/test_snaphu.py`

**Interfaces:**
- Produces: `find_conf(target_folder: Path) -> Path`, `find_exported_product(target_folder: Path) -> Path`, `find_unwrapped_product(target_folder: Path) -> Path`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_snaphu.py`:

```python
from radar_snap_lib.snap_ops.snaphu import (
    find_conf,
    find_exported_product,
    find_unwrapped_product,
)


class TestFindConf:
    def test_finds_the_single_conf_file(self, tmp_path: Path):
        (tmp_path / "snaphu.conf").write_text("x", encoding="utf-8")
        assert find_conf(tmp_path) == tmp_path / "snaphu.conf"

    def test_raises_when_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="No snaphu.conf"):
            find_conf(tmp_path)


class TestFindExportedProduct:
    def test_finds_the_single_dim_file(self, tmp_path: Path):
        (tmp_path / "Phase_ifg.dim").write_text("x", encoding="utf-8")
        assert find_exported_product(tmp_path) == tmp_path / "Phase_ifg.dim"

    def test_raises_when_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="No exported"):
            find_exported_product(tmp_path)

    def test_raises_when_ambiguous(self, tmp_path: Path):
        (tmp_path / "a.dim").write_text("x", encoding="utf-8")
        (tmp_path / "b.dim").write_text("x", encoding="utf-8")
        with pytest.raises(RuntimeError, match="Multiple exported"):
            find_exported_product(tmp_path)


class TestFindUnwrappedProduct:
    def test_finds_the_single_unwrapped_header(self, tmp_path: Path):
        (tmp_path / "UnwPhase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
        assert find_unwrapped_product(tmp_path) == tmp_path / "UnwPhase_ifg.snaphu.hdr"

    def test_raises_when_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="No unwrapped"):
            find_unwrapped_product(tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_snaphu.py::TestFindConf tests/test_snaphu.py::TestFindExportedProduct tests/test_snaphu.py::TestFindUnwrappedProduct -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement the helpers**

Add to `src/radar_snap_lib/snap_ops/snaphu.py` (add `Path` import already done in Task 3; add the three names to `__all__`):

```python
def _one_match(target_folder: Path, pattern: str, *, what: str) -> Path:
    matches = sorted(target_folder.glob(pattern))
    if not matches:
        raise RuntimeError(f"No {what} found in {target_folder} (pattern {pattern!r})")
    if len(matches) > 1:
        names = [m.name for m in matches]
        raise RuntimeError(f"Multiple {what} found in {target_folder}: {names}")
    return matches[0]


def find_conf(target_folder: Path) -> Path:
    """Locate the ``snaphu.conf`` a ``SnaphuExport`` run wrote."""
    return _one_match(target_folder, "snaphu.conf", what="snaphu.conf")


def find_exported_product(target_folder: Path) -> Path:
    """Locate the wrapped-phase ``.dim`` product ``SnaphuExport`` wrote."""
    return _one_match(target_folder, "*.dim", what="exported .dim product")


def find_unwrapped_product(target_folder: Path) -> Path:
    """Locate the unwrapped output the ``snaphu`` subprocess wrote.

    Naming convention confirmed against SNAP's own
    ``Snaphu-unwrapping-after.vm`` tool-adapter template.
    """
    return _one_match(
        target_folder, "UnwPhase_*.snaphu.hdr", what="unwrapped UnwPhase_*.snaphu.hdr output"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_snaphu.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/radar_snap_lib/snap_ops/snaphu.py tests/test_snaphu.py
git commit -m "feat: locate SnaphuExport/snaphu output files by naming convention"
```

---

### Task 5: `run_snaphu` — the subprocess wrapper

**Files:**
- Modify: `src/radar_snap_lib/snap_ops/snaphu.py`
- Test: `tests/test_snaphu.py`

**Interfaces:**
- Consumes: `parse_command(conf_path)` from Task 3.
- Produces: `run_snaphu(conf_path: Path, *, snaphu_bin: str = "snaphu", quiet: bool = False) -> subprocess.CompletedProcess[bytes]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_snaphu.py`:

```python
import subprocess

from radar_snap_lib.snap_ops.snaphu import run_snaphu


class TestRunSnaphu:
    def test_invokes_the_parsed_command_in_the_conf_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        conf = tmp_path / "snaphu.conf"
        conf.write_text(CONF_TEXT, encoding="utf-8")

        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "kwargs": kwargs})
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)

        run_snaphu(conf, snaphu_bin="snaphu")

        assert len(calls) == 1
        assert calls[0]["cmd"] == [
            "snaphu",
            "-f",
            "snaphu.conf",
            "Phase_ifg_srd_02Apr2026_13Jan2024.snaphu.img",
            "500",
        ]
        assert calls[0]["kwargs"]["cwd"] == tmp_path

    def test_missing_binary_raises_a_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        conf = tmp_path / "snaphu.conf"
        conf.write_text(CONF_TEXT, encoding="utf-8")

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="snaphu binary not found"):
            run_snaphu(conf, snaphu_bin="snaphu")

    def test_nonzero_exit_raises_with_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        conf = tmp_path / "snaphu.conf"
        conf.write_text(CONF_TEXT, encoding="utf-8")

        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, output=b"", stderr=b"bad tile geometry"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="bad tile geometry"):
            run_snaphu(conf, snaphu_bin="snaphu")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_snaphu.py::TestRunSnaphu -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement `run_snaphu`**

Add to `src/radar_snap_lib/snap_ops/snaphu.py` (add `import subprocess` to imports, add `"run_snaphu"` to `__all__`):

```python
def run_snaphu(
    conf_path: Path, *, snaphu_bin: str = "snaphu", quiet: bool = False
) -> subprocess.CompletedProcess[bytes]:
    """Run ``snaphu`` with the command recorded in ``conf_path``.

    Runs with ``conf_path.parent`` as the working directory, matching where
    ``SnaphuExport`` wrote the conf file and its referenced input. Raises
    ``RuntimeError`` -- not the raw ``subprocess`` exceptions -- so callers
    get one exception type to catch, with the binary name or captured
    stderr already in the message.
    """
    command = [snaphu_bin, *parse_command(conf_path)]
    try:
        return subprocess.run(
            command,
            cwd=conf_path.parent,
            check=True,
            capture_output=quiet,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"snaphu binary not found: {snaphu_bin!r}. Install snaphu and ensure "
            f"it is on PATH, or pass a different snaphu_bin."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        raise RuntimeError(
            f"snaphu exited with code {exc.returncode}: {' '.join(command)}\n{stderr}"
        ) from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_snaphu.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/radar_snap_lib/snap_ops/snaphu.py tests/test_snaphu.py
git commit -m "feat: run the snaphu CLI as a subprocess"
```

---

### Task 6: `runner.py` — the three-phase `run_graph` orchestration

This is the integration point: everything from Tasks 1-5 gets wired
together behind the existing `run_graph()` API.

**Files:**
- Modify: `src/radar_snap_lib/snap_ops/runner.py`
- Test: `tests/test_runner_snaphu.py` (new file — JVM-free, unlike the existing `tests/test_runner.py` which is `pytest.mark.snap`)

**Interfaces:**
- Consumes: `split_at_snaphu`, `find_conf`, `find_exported_product`, `find_unwrapped_product`, `run_snaphu` from `snap_ops/snaphu.py`; `build_graph` from `OpsConfig.py` (Task 1); `OpsConfig`, `ParsedNode`, `GraphConfigError` (existing).
- Produces: `radar_snap_lib.snap_ops.runner.SnaphuRunResult` (dataclass: `xml_a: str`, `xml_b: str`, `snaphu_command: list[str]`). `run_graph(...)` return type becomes `str | SnaphuRunResult` (unchanged `str` for non-split configs).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runner_snaphu.py`:

```python
"""Three-phase run_graph orchestration for SnaphuExport/SnaphuImport.

JVM-free: `execute_xml` and `radar_snap_lib.snap_ops.snaphu.run_snaphu` are
monkeypatched, so these tests exercise the orchestration logic (call order,
file discovery, node injection) without SNAP or a real snaphu binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from radar_snap_lib.snap_ops import runner
from radar_snap_lib.snap_ops.runner import SnaphuRunResult, run_graph


def _config(target_folder: Path) -> dict:
    return {
        "vars": {"target_folder": str(target_folder)},
        "pipeline": {
            "Read": {"file": "src.dim"},
            "GoldsteinPhaseFiltering": {},
            "SnaphuExport": {"targetFolder": "${vars.target_folder}"},
            "SnaphuImport": {"sources": "SnaphuExport"},
            "Terrain-Correction": {},
            "Write": {"file": "out.dim", "formatName": "BEAM-DIMAP"},
        },
    }


@pytest.fixture
def target_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "snaphu_work"
    folder.mkdir()
    return folder


def _stage_export_output(target_folder: Path) -> None:
    """Simulate what a real graph-A execution would leave on disk."""
    (target_folder / "Phase_ifg.dim").write_text("x", encoding="utf-8")
    (target_folder / "snaphu.conf").write_text(
        "# snaphu -f snaphu.conf Phase_ifg.snaphu.img 64\n", encoding="utf-8"
    )


class TestThreePhaseOrchestration:
    def test_runs_graph_a_then_snaphu_then_graph_b_in_order(
        self, target_folder: Path, monkeypatch: pytest.MonkeyPatch, registry
    ):
        calls: list[str] = []

        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            calls.append("execute_xml")
            if "SnaphuExport" in xml:
                _stage_export_output(target_folder)
            if "SnaphuImport" in xml:
                (target_folder / "UnwPhase_ifg.snaphu.hdr").write_text(
                    "x", encoding="utf-8"
                )

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            calls.append("run_snaphu")
            assert conf_path == target_folder / "snaphu.conf"

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        result = run_graph(_config(target_folder), registry=registry, quiet=True)

        assert calls == ["execute_xml", "run_snaphu", "execute_xml"]
        assert isinstance(result, SnaphuRunResult)
        assert "SnaphuExport" in result.xml_a
        assert "SnaphuImport" in result.xml_b

    def test_graph_b_reads_the_discovered_files(
        self, target_folder: Path, monkeypatch: pytest.MonkeyPatch, registry
    ):
        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            if "SnaphuExport" in xml:
                _stage_export_output(target_folder)
            if "SnaphuImport" in xml:
                (target_folder / "UnwPhase_ifg.snaphu.hdr").write_text(
                    "x", encoding="utf-8"
                )

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            return None

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        result = run_graph(_config(target_folder), registry=registry, quiet=True)

        assert str(target_folder / "Phase_ifg.dim") in result.xml_b
        assert str(target_folder / "UnwPhase_ifg.snaphu.hdr") in result.xml_b

    def test_snaphu_failure_aborts_before_graph_b(
        self, target_folder: Path, monkeypatch: pytest.MonkeyPatch, registry
    ):
        calls: list[str] = []

        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            calls.append("execute_xml")
            _stage_export_output(target_folder)

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            calls.append("run_snaphu")
            raise RuntimeError("snaphu exited with code 1")

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        with pytest.raises(RuntimeError, match="snaphu exited with code 1"):
            run_graph(_config(target_folder), registry=registry, quiet=True)

        assert calls == ["execute_xml", "run_snaphu"]

    def test_non_split_config_is_unaffected(self, monkeypatch: pytest.MonkeyPatch, registry):
        calls: list[str] = []
        monkeypatch.setattr(
            runner, "execute_xml", lambda xml, *, quiet=False: calls.append(xml)
        )

        config = {
            "pipeline": {
                "Read": {"file": "src.dim"},
                "Write": {"file": "out.dim", "formatName": "BEAM-DIMAP"},
            }
        }
        result = run_graph(config, registry=registry, quiet=True)

        assert calls == [result]
        assert isinstance(result, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_runner_snaphu.py -v`
Expected: FAIL (`ImportError: cannot import name 'SnaphuRunResult'`, or `AttributeError` for `runner.snaphu`)

- [ ] **Step 3: Implement the orchestration in `runner.py`**

Replace the full content of `src/radar_snap_lib/snap_ops/runner.py` with:

```python
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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omegaconf import DictConfig

from radar_snap_lib.snap_ops import snaphu
from radar_snap_lib.snap_ops.OpsConfig import (
    OpsConfig,
    ParsedNode,
    build_graph,
    dependency_order,
)
from radar_snap_lib.snap_ops.registry import Registry

__all__ = ["SnaphuRunResult", "execute_xml", "run_graph"]


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


def _read_node(node_id: str, file_path: Path) -> ParsedNode:
    return ParsedNode(id=node_id, op="Read", sources=[], params={"file": str(file_path)})


def _run_split(
    split: snaphu.SnaphuSplit,
    *,
    registry: Registry,
    dump_xml: Path | str | None,
    snaphu_bin: str,
    quiet: bool,
) -> SnaphuRunResult:
    graph_a = build_graph(
        dependency_order(split.graph_a), registry, graph_id="radar-snap-a"
    )
    xml_a = graph_a.to_xml()
    execute_xml(xml_a, quiet=quiet)

    target_folder = Path(str(split.export_node.params["targetFolder"]))
    conf_path = snaphu.find_conf(target_folder)
    exported_product = snaphu.find_exported_product(target_folder)

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
    ]
    graph_b = build_graph(
        dependency_order(graph_b_nodes), registry, graph_id="radar-snap-b"
    )
    xml_b = graph_b.to_xml()
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


def run_graph(
    config: str | Path | DictConfig | dict[str, Any],
    *,
    dump_xml: Path | str | None = None,
    registry: Registry | None = None,
    snaphu_bin: str = "snaphu",
    quiet: bool = False,
) -> str | SnaphuRunResult:
    """Validate a config, then execute it.

    Args:
        config: Path to a YAML config, a mapping, or a ``DictConfig``.
        dump_xml: Write the generated graph XML here before running. Handy for
            debugging -- the file opens directly in SNAP Desktop. For a
            config with a ``SnaphuExport``/``SnaphuImport`` pair, two files
            are written instead (suffixed ``.a``/``.b``), since there are
            two graphs.
        registry: Operator registry override, mainly for tests.
        snaphu_bin: The ``snaphu`` executable to invoke, for configs with a
            ``SnaphuExport``/``SnaphuImport`` pair. Defaults to ``snaphu``
            on ``PATH``.
        quiet: Suppress SNAP's progress output.

    Returns:
        The graph XML that was executed, or -- for a config with a
        ``SnaphuExport``/``SnaphuImport`` pair -- a :class:`SnaphuRunResult`
        with both graphs' XML and the ``snaphu`` command that ran.

    Raises:
        GraphConfigError: If the config does not describe a valid graph.
        RuntimeError: If the ``snaphu`` subprocess is missing or fails.
    """
    ops_config = OpsConfig.load(config, registry=registry)
    nodes = ops_config.parse()
    errors = ops_config.validate(nodes)
    if errors:
        from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError

        raise GraphConfigError(errors, ops_config.source)

    split = snaphu.split_at_snaphu(nodes)
    if split is not None:
        return _run_split(
            split,
            registry=ops_config.registry,
            dump_xml=dump_xml,
            snaphu_bin=snaphu_bin,
            quiet=quiet,
        )

    xml = build_graph(dependency_order(nodes), ops_config.registry).to_xml(dump_xml)
    execute_xml(xml, quiet=quiet)
    return xml
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_runner_snaphu.py tests/test_ops_config.py -v`
Expected: all PASS

- [ ] **Step 5: Run the full JVM-free suite to check for regressions**

Run: `uv run pytest -m "not snap" -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/radar_snap_lib/snap_ops/runner.py tests/test_runner_snaphu.py
git commit -m "feat: run SnaphuExport/SnaphuImport configs as three phases"
```

---

### Task 7: CLI — `dump-xml` and `run` handle a split config

**Files:**
- Modify: `src/radar_snap_lib/snap_ops/cli.py:200-233` (`_cmd_dump_xml`, `_cmd_run`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `snap_ops.snaphu.split_at_snaphu`, `snap_ops.runner.SnaphuRunResult`.

`dump-xml` never executes anything, but graph B's `Read` nodes point at
files that only exist *after* graph A and `snaphu` have actually run. So for
a split config, `dump-xml` can only show graph A statically; getting graph
B's XML requires `run --dump-xml`, which executes for real.

- [ ] **Step 1: Write the failing tests**

Look at the existing CLI tests first for the fixture/harness pattern:

Run: `uv run pytest tests/test_cli.py -v --collect-only` and open
`tests/test_cli.py` to see how `_cmd_dump_xml`/`_cmd_run` are invoked
(likely via `build_parser().parse_args([...])` then calling `args.func(args)`,
or via `main([...])` — match whatever pattern is already there).

Add tests to `tests/test_cli.py` (adapt the exact call style to match the
file's existing tests once read):

```python
def test_dump_xml_on_split_config_writes_graph_a_only(tmp_path, capsys):
    from radar_snap_lib.snap_ops.cli import main

    target_folder = tmp_path / "snaphu_work"
    target_folder.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
pipeline:
  Read: {{file: src.dim}}
  SnaphuExport: {{targetFolder: {target_folder}}}
  SnaphuImport: {{sources: SnaphuExport}}
  Write: {{file: out.dim, formatName: BEAM-DIMAP}}
""",
        encoding="utf-8",
    )

    exit_code = main(["dump-xml", str(config_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "SnaphuExport" in out
    assert "SnaphuImport" not in out
    assert "graph B" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_dump_xml_on_split_config_writes_graph_a_only -v`
Expected: FAIL (either an exception building the full graph, or `SnaphuImport not in out` fails because today's `dump-xml` naively serialises everything into one, structurally-misleading graph)

- [ ] **Step 3: Update `_cmd_dump_xml` and `_cmd_run`**

In `src/radar_snap_lib/snap_ops/cli.py`, replace `_cmd_dump_xml` (currently
lines 200-219):

```python
def _cmd_dump_xml(args: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.OpsConfig import (
        GraphConfigError,
        OpsConfig,
        build_graph,
        dependency_order,
    )
    from radar_snap_lib.snap_ops.snaphu import split_at_snaphu

    try:
        loaded = _load_source(Path(args.config))
    except ConfigLoadError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        config = OpsConfig.load(loaded, source=str(args.config))
        nodes = config.parse()
        errors = config.validate(nodes)
        if errors:
            raise GraphConfigError(errors, config.source)
        split = split_at_snaphu(nodes)
        if split is not None:
            xml = build_graph(
                dependency_order(split.graph_a), config.registry, graph_id="radar-snap-a"
            ).to_xml(args.output)
        else:
            xml = build_graph(dependency_order(nodes), config.registry).to_xml(args.output)
    except GraphConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.output:
        print(f"Wrote {args.output}")
    else:
        print(xml, end="")

    if split is not None:
        print(
            "\nThis config also has a SnaphuExport/SnaphuImport pair -- graph B "
            "depends on files snaphu writes at run time, so it can't be dumped "
            "statically. Use `radar-snap run --dump-xml <path>` instead, which "
            "writes both graphs after actually running.",
            file=sys.stderr,
        )
    return 0
```

Replace `_cmd_run` (currently lines 222-232):

```python
def _cmd_run(args: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError
    from radar_snap_lib.snap_ops.runner import run_graph

    try:
        run_graph(args.config, dump_xml=args.dump_xml, quiet=args.quiet)
    except GraphConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{args.config}: done")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/radar_snap_lib/snap_ops/cli.py tests/test_cli.py
git commit -m "feat: cli dump-xml/run handle SnaphuExport/SnaphuImport configs"
```

---

### Task 8: Extend `examples/s1_slc_interferogram.yaml` through the full chain

Insert the missing `Enhanced-Spectral-Diversity` (`esd`) and `Multilook`
steps, then continue through the snaphu split to a final displacement
product, matching the chain:

```text
read_ref → orbit_ref → split_ref ─┐
                                   ├→ coreg → esd → ifg → deburst
read_sec → orbit_sec → split_sec ─┘
→ multilook → goldstein → snaphu_export → [snaphu CLI] → snaphu_import
→ phase_to_disp → terrain_correction → write
```

**Files:**
- Modify: `examples/s1_slc_interferogram.yaml`
- Test: `tests/test_ops_config.py` (existing `TestExamples` parametrized tests pick this file up automatically via `GRAPH_EXAMPLES`), `tests/test_snaphu.py`

**Interfaces:**
- Consumes: `split_at_snaphu` (Task 2), `OpsConfig.load` / `.parse()` (existing).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_snaphu.py`:

```python
from pathlib import Path

from radar_snap_lib.snap_ops.OpsConfig import OpsConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class TestInterferogramExampleSplit:
    def test_the_example_config_splits_at_snaphu(self, registry):
        path = EXAMPLES / "s1_slc_interferogram.yaml"
        nodes = OpsConfig.load(path, registry=registry).parse()

        split = split_at_snaphu(nodes)

        assert split is not None
        assert split.export_node.op == "SnaphuExport"
        assert split.import_node.op == "SnaphuImport"
        assert [n.id for n in split.graph_b][-1] == "Write"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_snaphu.py::TestInterferogramExampleSplit -v`
Expected: FAIL (`split is None` -- today's example has no `SnaphuExport` node)

- [ ] **Step 3: Extend the example config**

Replace the `pipeline:` section of `examples/s1_slc_interferogram.yaml`
(everything from `pipeline:` onward) with:

```yaml
  pipeline:
    read_ref:
      op: Read
      file: ${vars.reference}

    orbit_ref:
      op: Apply-Orbit-File
      sources: read_ref

    split_ref:
      op: TOPSAR-Split
      sources: orbit_ref
      subswath: ${vars.subswath}
      selectedPolarisations: ${vars.polarisation}

    read_sec:
      op: Read
      sources: []
      file: ${vars.secondary}

    orbit_sec:
      op: Apply-Orbit-File
      sources: read_sec

    split_sec:
      op: TOPSAR-Split
      sources: orbit_sec
      subswath: ${vars.subswath}
      selectedPolarisations: ${vars.polarisation}

    coreg:
      op: Back-Geocoding
      sources: [split_ref, split_sec]
      demName: Copernicus 30m Global DEM
      resamplingType: BISINC_5_POINT_INTERPOLATION
      maskOutAreaWithoutElevation: true

    Enhanced-Spectral-Diversity:
      sources: coreg

    Interferogram:
      sources: Enhanced-Spectral-Diversity
      subtractFlatEarthPhase: true
      includeCoherence: true
      cohWinRg: 10
      cohWinAz: 2

    TOPSAR-Deburst: {}

    Multilook:
      nRgLooks: 4
      nAzLooks: 1

    GoldsteinPhaseFiltering:
      alpha: 1.0

    SnaphuExport:
      targetFolder: ${vars.snaphu_folder}
      statCostMode: DEFO

    SnaphuImport:
      sources: SnaphuExport

    PhaseToDisplacement: {}

    Terrain-Correction:
      demName: Copernicus 30m Global DEM

    Write:
      file: ${vars.output}
      formatName: BEAM-DIMAP
```

Also add `snaphu_folder` to the `vars:` block (after `output:`):

```yaml
    snaphu_folder: /home/kjell/projects/py_projects/radar-snap-lib/results/s1/petacciato/snaphu
```

Update the file's leading comment (lines 5-7) since the chain is no longer
"not a linear chain" only for the reasons stated -- it now also crosses a
`snaphu` subprocess boundary:

```yaml
  # Sentinel-1 SLC interferogram from a reference/secondary pair, unwrapped
  # via snaphu and terrain-corrected to a displacement product.
  #
  #   radar-snap run examples/s1_slc_interferogram.yaml
  #
  # Not a linear chain: two Read nodes feed Back-Geocoding, and TOPSAR-Split
  # is used twice. Both need the reserved keys -- `op` to name the operator
  # when the node id differs from it, and `sources` to wire inputs
  # explicitly. The SnaphuExport -> SnaphuImport edge is also special: SNAP
  # can't unwrap phase itself, so `run_graph` splits execution there and
  # shells out to the `snaphu` binary in between (see
  # docs/superpowers/specs/2026-08-05-snaphu-subprocess-design.md).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_snaphu.py tests/test_ops_config.py -v`
Expected: all PASS, including the existing parametrized `TestExamples` tests
now covering the extended file (`test_example_is_valid`,
`test_example_serialises`).

- [ ] **Step 5: Run the full JVM-free suite**

Run: `uv run pytest -m "not snap" -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add examples/s1_slc_interferogram.yaml tests/test_snaphu.py
git commit -m "docs: extend the interferogram example through snaphu to a displacement product"
```

---

## Manual verification (not automated — see design doc's Testing section)

After all tasks are merged, running `radar-snap run examples/s1_slc_interferogram.yaml`
against real Sentinel-1 data with a real `snaphu` binary on `PATH` is the
only way to confirm the `SnaphuImport` two-source wiring and file-naming
assumptions hold against actual SNAP output, not just the mocked tests
above. This is explicitly out of scope for the automated suite (see the
design doc's Testing section for why) but should happen before anyone
relies on this path for real processing.
