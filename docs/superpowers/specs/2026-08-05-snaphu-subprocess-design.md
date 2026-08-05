# Snaphu Subprocess Step — Design

Date: 2026-08-05
Status: approved

## Problem

The standard Sentinel-1 SLC interferogram workflow needs phase unwrapping via
`snaphu`, ESA's reference unwrapper. In SNAP's own GPF/InSAR toolbox this is a
three-step affair:

```
... -> SnaphuExport -> [snaphu CLI, run out-of-process] -> SnaphuImport -> ...
```

`SnaphuExport` and `SnaphuImport` are ordinary SNAP operators (already in the
committed registry, already have generated `Graph` builder methods), but the
actual unwrapping happens in a separate `snaphu` binary invoked from the
command line — SNAP does not, and cannot, shell out to it mid-graph.

Today [runner.py](../../../src/radar_snap_lib/snap_ops/runner.py) only knows
how to execute one GPF graph XML document through
`GraphProcessor.executeGraph()`. There is no subprocess execution anywhere in
the codebase (confirmed by search — `subprocess` is only used in
`codegen.py` to run `ruff` on generated files). So a pipeline config
describing the full chain from `read_ref` through `write` — including the
snaphu step — cannot run end-to-end yet. This blocks
[examples/s1_slc_interferogram.yaml](../../../examples/s1_slc_interferogram.yaml)
from ever reaching a displacement product.

## Goals

- One YAML config describes the whole chain, snaphu step included, using the
  existing `pipeline:` DSL with **no new reserved keys or node types**.
- `radar-snap run config.yaml` (and the underlying `run_graph()`) executes it
  end-to-end: SNAP graph → `snaphu` CLI → SNAP graph, transparently.
- Configs with no `SnaphuExport`/`SnaphuImport` pair behave exactly as today
  (single graph, no behavior change, no performance cost).
- Split-detection and validation are JVM-free and unit-testable, matching the
  rest of `OpsConfig`.
- Clear, fail-fast errors: missing `snaphu` binary, malformed split (e.g.
  `SnaphuExport` with no matching `SnaphuImport`), or a nonzero `snaphu` exit
  code all stop the pipeline with a specific message instead of a confusing
  downstream failure.

## Non-goals

- `BatchSnaphuUnwrapOp` (the PyRate-oriented operator that downloads and runs
  SNAPHU itself, for batches of interferograms) is a different workflow and
  out of scope here.
- A general "run arbitrary shell command mid-pipeline" DSL feature. This
  design is specific to the `SnaphuExport` → `SnaphuImport` pair; a generic
  subprocess node type is explicitly not being added (see brainstorming
  discussion — the Python-runner-orchestrates approach was chosen over a new
  `op: subprocess` pseudo-node).
- Bundling or vendoring the `snaphu` binary. It's expected on `PATH` (or
  pointed to via a parameter), same assumption SNAP itself makes.

## Architecture

### Why the split has to happen outside the JVM

Each `GraphProcessor.executeGraph()` call is a self-contained execution: it
takes XML in, streams tiles through, produces output files, and returns.
Nothing persists in memory between two separate calls. So a config that
crosses a `snaphu` subprocess boundary is unavoidably **two** independent GPF
graphs joined by a file on disk plus an external process — there's no way to
keep it as one JVM execution.

### The split is invisible to the YAML author

The pipeline is still written as one linear-ish chain, same as
`s1_slc_interferogram.yaml` today:

```yaml
pipeline:
  read_ref: {op: Read, file: ${vars.reference}}
  orbit_ref: {op: Apply-Orbit-File, sources: read_ref}
  split_ref: {op: TOPSAR-Split, sources: orbit_ref, subswath: ${vars.subswath}}
  read_sec: {op: Read, sources: [], file: ${vars.secondary}}
  orbit_sec: {op: Apply-Orbit-File, sources: read_sec}
  split_sec: {op: TOPSAR-Split, sources: orbit_sec, subswath: ${vars.subswath}}
  coreg: {op: Back-Geocoding, sources: [split_ref, split_sec], demName: Copernicus 30m Global DEM}
  Interferogram: {sources: coreg, subtractFlatEarthPhase: true, includeCoherence: true}
  TOPSAR-Deburst: {}
  Multilook: {nRgLooks: 4, nAzLooks: 1}
  GoldsteinPhaseFiltering: {alpha: 1.0}
  snaphu_export:
    op: SnaphuExport
    targetFolder: ${vars.snaphu_folder}
    statCostMode: DEFO
  snaphu_import:
    op: SnaphuImport
    sources: snaphu_export
  phase_to_disp: {op: PhaseToDisplacement, sources: snaphu_import}
  terrain_correction: {op: Terrain-Correction, sources: phase_to_disp, demName: Copernicus 30m Global DEM}
  Write: {file: ${vars.output}, formatName: BEAM-DIMAP}
```

`sources: snaphu_export` on `snaphu_import` reads exactly like any other
edge. `OpsConfig.parse()`/`.validate()` don't need to change at all — the
edge is structurally valid (a real node id, correct source count for
`SnaphuImport`'s array slot). What changes is what `run_graph()` does with
that edge when it reaches execution.

### `split_at_snaphu`

New module `snap_ops/snaphu.py`. Given the already-`parse()`d node list:

```python
def split_at_snaphu(nodes: list[ParsedNode]) -> SnaphuSplit | None:
    ...
```

Returns `None` when there's no `SnaphuExport` node (the common case — the
existing single-graph path in `run_graph()` is untouched). When present,
validates and returns a `SnaphuSplit(graph_a, export_node, graph_b)`:

- Exactly one `SnaphuExport` node. More than one is a `GraphConfigError`
  (SNAP's own operator doesn't support batching this way — that's what
  `BatchSnaphuUnwrapOp` is for, and it's a different, non-split workflow).
- Exactly one `SnaphuImport` node downstream of it, and — this is the
  important constraint — the `SnaphuExport` node id must appear directly in
  `SnaphuImport`'s `sources`. That edge is the one the runner intercepts and
  turns into "write, unwrap, re-read" instead of an in-JVM reference.
- `graph_a` = the `SnaphuExport` node plus everything reachable as its
  ancestor (same topological-order logic `OpsConfig._dependency_order`
  already implements).
- `graph_b` = the `SnaphuImport` node plus everything reachable as its
  descendant, i.e. the rest of the pipeline (`phase_to_disp`,
  `terrain_correction`, `Write`, ...). Any source edge in `graph_b` that
  points at a `graph_a` node (there should be exactly one: the
  `SnaphuExport` → `SnaphuImport` edge) is replaced with a **synthetic
  `Read`** node, injected by the runner, whose `file` parameter is derived
  from `SnaphuExport`'s own `targetFolder` parameter and the standard SNAP
  BEAM-DIMAP naming convention for that folder's exported product.
- Nodes that exist only to serve both halves (none, expected, given the
  linear chain above — `graph_a` and `graph_b` are disjoint except for that
  one edge) would be a validation error for now: a node referenced from both
  sides of the split, other than the `SnaphuExport`/`SnaphuImport` pair
  itself, is rejected as unsupported rather than silently mis-executed.
  (If a real workflow needs that later — e.g. `SnaphuImport` also wanting
  the pre-Goldstein stack for baseline metadata — see Open Questions below;
  the validation is deliberately strict until that's confirmed necessary.)

### Execution: `run_graph()`

`runner.py::run_graph()` gains the three-phase path:

1. `OpsConfig.load(config).parse()` as today.
2. `split_at_snaphu(nodes)`. If `None`: existing single-`to_xml`/`execute_xml`
   path, byte-for-byte unchanged.
3. Otherwise:
   a. Build and run **Graph A** (`GraphProcessor`, same as today) through
      `SnaphuExport`. This writes the wrapped-phase product and
      `snaphu.conf` under `targetFolder`.
   b. Resolve `snaphu.conf`'s path from `targetFolder`, call
      `snaphu.run_snaphu(conf_path, snaphu_bin=..., quiet=...)`. This
      shells out via `subprocess.run(cmd, check=True, cwd=conf_path.parent,
      capture_output=quiet)`. `FileNotFoundError` (binary missing) and
      non-zero exit both raise a clear, specific error *before* Graph B
      starts — no wasted JVM work, no partial output silently mistaken for
      success.
   c. Build and run **Graph B** (`GraphProcessor`, second independent
      execution) starting from the injected `Read` node through the rest of
      the user's pipeline.
   d. Returns the combination of both graphs' XML (a small dataclass, not a
      bare `str`, so callers can tell single-graph and split runs apart
      without probing the return value's shape — exact shape is an
      implementation-plan decision, not fixed here).

### CLI (`dump-xml`)

`radar-snap dump-xml` on a split config has no single graph to dump. It
writes two files, suffixed (`graph.a.xml` / `graph.b.xml` next to the
requested `-o` path, or two clearly labeled sections to stdout when no `-o`
is given). Exact flag shape is an implementation-plan decision.

### Command extraction from `snaphu.conf`

SNAP's `SnaphuExport` writes `snaphu.conf` into `targetFolder` with a
header comment containing the exact recommended command line to invoke
`snaphu` with (this is documented, standard ESA STEP/SNAP InSAR tutorial
behavior). `snaphu.parse_command(conf_path)` reads that comment and returns
the argument list, so the runner never has to reconstruct tile counts,
image dimensions, or cost-mode flags itself — it just replays what SNAP
already computed. See **Open Questions** — the precise comment format is an
assumption to verify against a real `SnaphuExport` output before/while
implementing, not something to hardcode confidently today.

## Module layout

| File | Role |
| --- | --- |
| `snap_ops/snaphu.py` | new: `split_at_snaphu`, `parse_command`, `run_snaphu` |
| `snap_ops/runner.py` | `run_graph()` grows the three-phase path; single-graph path untouched |
| `snap_ops/cli.py` | `dump-xml` / `run` handle the two-graph-XML case |
| `examples/s1_slc_interferogram.yaml` | extended to the full chain through `Write` (currently stops at `GoldsteinPhaseFiltering` → `Write`) |

## Open questions / risks (to resolve during implementation)

1. **`SnaphuImport`'s actual source requirements.** The registry says it
   takes a `sourceProducts` array (min 1, unbounded). The standard SNAP
   tutorial workflow feeds it *two* sources — the re-read
   (now-unwrapped) exported product, and the original coregistered/
   interferogram stack for orbit and baseline metadata. This design assumes
   the single re-read source is sufficient (matching what the YAML author
   would naturally write: `sources: snaphu_export`), but that needs
   verification against a real SNAP run or authoritative docs before the
   implementation plan finalizes graph B's construction. If a second source
   turns out to be required, the "no node referenced from both sides of the
   split" validation above needs to be relaxed for exactly this case.
2. **`snaphu.conf` header format.** The exact comment syntax SNAP emits
   needs confirming (a real `SnaphuExport` run, or SNAP source, before
   `parse_command` is implemented) rather than assumed from memory. Fallback
   if the comment turns out to be unparseable or absent in some SNAP
   version: reconstruct the command from `SnaphuExport`'s own node
   parameters (`statCostMode`, `initMethod`, `numberOfTileRows/Cols`, tile
   overlaps) plus the wrapped-phase image width read from the accompanying
   ENVI `.hdr` file. Worth a small spike before committing to the
   comment-parsing approach as primary.
3. **Exported product naming convention.** The synthetic `Read` node in
   graph B needs to know the exact filename `SnaphuExport` writes under
   `targetFolder` (BEAM-DIMAP `.dim`/`.data` naming derived from the source
   product name). Needs confirming against a real run, same as above.

None of these block writing the implementation plan — they become the first
verification step(s) inside it — but they should not be silently guessed at
during implementation without a note back to the user.

## Testing

- `split_at_snaphu`: JVM-free unit tests on `ParsedNode` fixtures — happy
  path, missing `SnaphuImport`, multiple `SnaphuExport`, `SnaphuImport` not
  sourced from the `SnaphuExport` node id, a node referenced from both
  halves. Same style as `tests/test_ops_config.py`.
- `parse_command`: unit test against a small fixture `snaphu.conf` text
  (once its real format is confirmed per Open Question 2).
- Three-phase orchestration: `runner.py` test with `execute_xml` and
  `subprocess.run` monkeypatched, asserting call order (graph A → snaphu →
  graph B) and that a nonzero `snaphu` exit aborts before graph B runs. No
  SNAP or real `snaphu` binary needed.
- A `pytest.mark.snap` integration test alongside the existing ones in
  `tests/test_runner.py`, once Open Questions 1–3 are confirmed and a real
  `snaphu` binary is available in the test environment.
