# Snaphu Subprocess Step — Design

Date: 2026-08-05
Status: approved

## Problem

The standard Sentinel-1 SLC interferogram workflow needs phase unwrapping via
`snaphu`, ESA's reference unwrapper. In SNAP's own GPF/InSAR toolbox this is a
three-step affair:

```text
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

## Confirmed against the local SNAP install

The three points flagged as open questions during design turned out to be
directly answered by files SNAP itself ships (this machine has SNAP
installed at `~/esa-snap`, confirmed working via `ensure_esa_snappy()`).
Two sources, read directly, no guessing required:

- `~/.snap/graphs/internal/insar/SnaphuExportGraph.xml` — SNAP's own
  reference graph for the export half.
- `~/.snap/graphs/internal/insar/SnaphuImportGraph.xml` — same, for import.
- `~/.snap/auxdata/tool-adapters/Snaphu-unwrapping/*.vm` — Velocity
  templates SNAP Desktop's "External Tools" wizard uses to drive `snaphu`
  from the GUI. Not the mechanism this design uses (we shell out from
  Python, not from a GUI tool adapter), but they encode the exact conf-file
  and output-naming conventions, which *are* what this design needs.

**1. `SnaphuImport` needs two sources**, confirmed by
`SnaphuImportGraph.xml`:

```xml
<node id="3-SnaphuImport">
    <operator>SnaphuImport</operator>
    <sources>
        <sourceProduct refid="1-Read-Phase"/>
        <sourceProduct.1 refid="2-Read-Unwrapped-Phase"/>
    </sources>
    ...
```

`sourceProduct` = a fresh `Read` of the *exported* wrapped-phase product
(the `.dim` `SnaphuExport` wrote to `targetFolder`). `sourceProduct.1` = a
fresh `Read` of the unwrapped output `snaphu` itself produced. So graph B's
injected node is not one `Read` but **two**, both feeding `SnaphuImport`.
The "no node referenced from both sides of the split" validation in
`split_at_snaphu` needs a narrow exception for this: `SnaphuImport` is
allowed exactly two inbound edges that resolve to synthetic reads, both
derived from the single `SnaphuExport` node it names in `sources`.

**2. `snaphu.conf`'s recommended command is a literal, greppable line.**
Confirmed by `Snaphu-unwrapping-template.vm`, which does exactly this in
Java reflection (the same operation `parse_command` will do in Python):
read the first 10 lines of `snaphu.conf`, find the one containing
`'snaphu -f snaphu.conf'`, take the substring from `'snaphu -f'` onward,
split on spaces to get the argument list. `parse_command` is a direct
Python port of this logic — no fallback/reconstruction path needed.

**3. The unwrapped output's naming convention is confirmed** by
`Snaphu-unwrapping-after.vm`: files named `UnwPhase_*.snaphu.hdr` (with a
matching `.img`) appear in the working directory `snaphu` was run in — i.e.
the runner's `cwd` for the subprocess call, which this design already sets
to `targetFolder` (the same folder `snaphu.conf` and the exported product
live in). So after the subprocess step, graph B's second synthetic `Read`
globs `targetFolder` for `UnwPhase_*.snaphu.hdr`; there must be exactly one
match, which is itself a useful post-condition to assert (zero means
`snaphu` didn't actually produce output despite exiting zero; more than one
means a stale file from a previous run wasn't cleaned up).

For the first synthetic `Read` (the re-read of `SnaphuExport`'s own
output), the exported product's exact filename is not hardcoded — it's
whatever `.dim` `SnaphuExport` wrote to `targetFolder` during graph A.
Rather than guess the naming convention, the runner globs `targetFolder`
for `*.dim` right after graph A completes and before the subprocess runs;
exactly one match is required (same fail-fast reasoning as above).

## Testing

- `split_at_snaphu`: JVM-free unit tests on `ParsedNode` fixtures — happy
  path, missing `SnaphuImport`, multiple `SnaphuExport`, `SnaphuImport` not
  sourced from the `SnaphuExport` node id, a node referenced from both
  halves. Same style as `tests/test_ops_config.py`.
- `parse_command`: unit test against a fixture `snaphu.conf` text built to
  match the confirmed parsing rule above (scan first 10 lines for one
  containing `'snaphu -f snaphu.conf'`).
- Three-phase orchestration: `runner.py` test with `execute_xml` and
  `subprocess.run` monkeypatched, asserting call order (graph A → snaphu →
  graph B) and that a nonzero `snaphu` exit aborts before graph B runs. No
  SNAP or real `snaphu` binary needed.

**No real end-to-end `SnaphuExport` test is planned.** Verified directly
against the local SNAP install: `SnaphuExport` rejects anything that isn't a
properly coregistered InSAR stack —

```text
RuntimeError: org.esa.snap.core.gpf.graph.GraphException:
[NodeId: SnaphuExport] Input should be a coregistered stack.
```

— confirmed by running it against a minimal synthetic two-band product,
which SNAP correctly refused. Building a fixture with valid abstracted
metadata (orbit state vectors, master/slave tags, etc.) to satisfy that
check is disproportionate effort for this feature, and the existing test
suite doesn't attempt full InSAR chains either (`tests/test_runner.py`
covers `Subset`+`BandMaths` only). Real end-to-end validation — with actual
Sentinel-1 data and a real `snaphu` binary — is a manual verification step
for whoever implements this, not part of the automated suite.
