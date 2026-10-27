# Radar Snap Lib

Build and run ESA SNAP process graphs from YAML, with a typed Python builder API
for the radar operators.

## Set up

`esa_snappy` is not on PyPI -- SNAP's `snappy-conf` generates it into its own
virtual environment. Point this project at that environment:

```
cp .env.example .env    # then edit ESA_SNAPPY_VENV
uv sync
```

Resolution order is `ESA_SNAPPY_VENV` in the environment, then `.env`, then
`[tool.radar-snap-lib] esa-snappy-venv` in `pyproject.toml`.

## Searching the ASF archive

Finding scenes works like running a graph: describe it in YAML, validate it
offline, then execute. There are no command line flags -- every setting,
including the download target, lives in the config.

```yaml
# searches/testgebiet.yaml
aoi: aois/testgebiet.gpkg      # or [10.0, 50.0, 11.0, 51.0], or a WKT string
start: 2024-01-01
end: 2024-06-30
platform: SENTINEL-1
flight_direction: ASCENDING
processing_level: SLC
max_results: 100
dest: /data/s1
output: results/testgebiet.geojson
```

```console
radar-snap validate searches/testgebiet.yaml
radar-snap search   searches/testgebiet.yaml
radar-snap download searches/testgebiet.yaml
```

A GeoPackage, Shapefile or GeoJSON AOI has all its features unioned and
reprojected to WGS-84, then simplified as far as the ASF API requires.

Any [`ASFSearchOptions`](https://docs.asf.alaska.edu/asf_search/searching/)
key is accepted, in ASF's own camelCase (`flightDirection`) or as a snake_case
alias (`flight_direction`). Validation runs against `asf_search`'s own option
table, so a typo is caught without a network connection:

```console
$ radar-snap validate searches/testgebiet.yaml
1 problem in searches/testgebiet.yaml:
  - Unknown key 'flightdirection'. Did you mean: flight_direction?
```

Searching needs no account. Downloading needs NASA Earthdata credentials --
set `EARTHDATA_TOKEN` in your environment or `.env` (see `.env.example`).

## Describing a pipeline

One key per operator, its parameters as subkeys. Each node takes the previous
one as its source, so a linear chain needs no wiring:

```yaml
vars:
  scene: /data/s1/S1A_IW_GRDH_1SDV_20240101T054321.zip

pipeline:
  Read:
    file: ${vars.scene}
  Apply-Orbit-File:
    orbitType: Sentinel Precise (Auto Download)
  Calibration:
    outputBetaBand: true
  Terrain-Flattening:
    demName: Copernicus 30m Global DEM
  Terrain-Correction:
    pixelSpacingInMeter: 10.0
  Write:
    file: /data/s1/out/gamma0.tif
    formatName: GeoTIFF
```

`vars` is plain [OmegaConf](https://omegaconf.readthedocs.io/), so `${vars.x}`
interpolation and `${oc.env:HOME}` work anywhere in the file.

Two reserved keys handle everything a straight chain cannot -- `op` when the node
id is not the operator alias (needed to use an operator twice), and `sources` to
wire inputs explicitly:

```yaml
pipeline:
  ref:   {op: Read, file: a.zip}
  sec:   {op: Read, sources: [], file: b.zip}
  coreg: {op: Back-Geocoding, sources: [ref, sec], demName: SRTM 3Sec}
  Write: {file: ifg.dim}
```

No SNAP operator has a parameter called `op` or `sources`, so the two never
collide with a real parameter name.

See [examples/](examples/) for a full GRD backscatter chain and an SLC
interferogram pair.

## Running it

```bash
radar-snap validate examples/s1_grd_gamma0.yaml       # no JVM needed
radar-snap dump-xml examples/s1_grd_gamma0.yaml       # inspect the GPF graph
radar-snap run examples/s1_grd_gamma0.yaml --dump-xml /tmp/graph.xml
```

Or from Python:

```python
from radar_snap_lib.snap_ops import run_graph

run_graph("pipeline.yaml", dump_xml="graph.xml")
```

`vars=` fills `${vars.*}` placeholders from code, merged over the config's own
`vars` section, so one config runs over many scenes without being rewritten:

```python
for scene in scenes:
    run_graph("pipeline.yaml", vars={"scene": scene, "output": out / scene.stem})
```

`OpsConfig.load(config, vars=...)` takes the same argument, e.g. to validate
every run before the first one starts.

The config is translated into SNAP's own graph XML and handed to
`GraphProcessor` -- the same path the `gpt` CLI takes, so the whole chain streams
tile by tile instead of materialising every intermediate product. The dumped
`graph.xml` opens directly in SNAP Desktop.

### Saving intermediates and the executed graph

A pipeline normally streams straight through without writing anything between
the input and the final `Write`. Set a top-level `output_dir` and mark any node
with `save:` to also materialise that node's output as BEAM-DIMAP, which opens
directly in SNAP Desktop:

```yaml
output_dir: /data/petacciato

pipeline:
  Interferogram:
    includeCoherence: true
    save: true              # -> /data/petacciato/Interferogram.dim
  GoldsteinPhaseFiltering:
    alpha: 1.0
    save: ifg_filtered      # -> /data/petacciato/ifg_filtered.dim
```

`save:` is opt-in per node because intermediates are large -- the coregistered
complex stack for a Sentinel-1 SLC pair runs to several GB per step.

With `output_dir` set, the graph XML that actually ran is written there too, as
`graph.xml` (or `graph.a.xml` and `graph.b.xml` for a config that splits at
snaphu). It is written *before* each graph executes, so a run that fails still
leaves the graph behind to inspect. Without `output_dir` nothing extra is
written, and `save:` becomes a config error. The separate `--dump-xml` flag
still works and is independent of this.

### SnaphuExport / SnaphuImport (phase unwrapping)

A pipeline containing a `SnaphuExport` -> `SnaphuImport` pair needs the
external `snaphu` binary on `PATH` -- SNAP's `GraphProcessor` cannot shell out
mid-graph, so `run_graph` transparently splits such a config into two SNAP
graphs with a `snaphu` subprocess call in between. `radar-snap validate` and
`radar-snap dump-xml` catch a malformed split (wrong node counts, bad wiring)
without needing SNAP or `snaphu` installed. To point at a `snaphu` build that
isn't on `PATH`, either set `SNAPHU_BIN` (as an environment variable or in
`.env`, resolved the same way as `ESA_SNAPPY_VENV`) or pass `--snaphu-bin` to
`radar-snap run` (which takes precedence over `SNAPHU_BIN`); the same value
can be passed as `snaphu_bin=` to `run_graph` from Python.

A `snaphu` that SNAP downloaded itself (into
`~/.snap/auxdata/snaphu-*/bin/snaphu`) usually arrives **without its executable
bit** -- SNAP's auxdata unpacker does not preserve Unix permissions -- so it
needs a one-off `chmod +x` before any run can use it. `run` checks the binary
before executing the first graph rather than at the point of invocation, so
this fails immediately instead of after a full coregistration has been
computed; `validate` reports the same problem as a warning without changing its
exit code, so it still works on machines with no `snaphu` at all.

For a split config, `dump-xml` only emits graph A's XML (graph B depends on
files `snaphu` writes at run time, so it can't be dumped statically) and
prints a note to use `run --dump-xml` instead. `run --dump-xml <path>` writes
both graphs for a split config, as `<stem>.a<suffix>` and `<stem>.b<suffix>`
(e.g. `graph.a.xml` and `graph.b.xml` for `--dump-xml graph.xml`).

### DeepDespeckle (deep-learning despeckling, outside SNAP)

`DeepDespeckle` despeckles with [deepdespeckling](https://github.com/kwundram2602/deepdespeckling)
(PyTorch) and is written in a config like any SNAP operator:

```yaml
output_dir: /data/s1/merlin

pipeline:
  ...
  TOPSAR-Deburst: {}
  DeepDespeckle:
    method: merlin          # or sar2sar
  Calibration:
    outputSigmaBand: true
  ...
```

It needs the optional `despeckle` extra (deepdespeckling and torch). In another
project, `uv add "radar-snap-lib[despeckle]"`; which torch build that brings
(CPU or CUDA) is up to that project's own torch index setup -- the extra pins
none, so it never clashes with it. In this repo:

```bash
uv sync --extra despeckle          # CPU torch (the default `cpu` group), ~200 MB
uv sync --extra despeckle --no-default-groups --group dev --group cu128
                                   # CUDA torch for an NVIDIA GPU, several GB
```

The model weights ship with deepdespeckling. On a CPU, MERLIN takes in the
order of half an hour to an hour for one polarisation of a full IW subswath.

| `method`  | input                                  | output                                       |
|-----------|----------------------------------------|----------------------------------------------|
| `merlin`  | complex SLC: every `i_*`/`q_*` pair    | `Amplitude_*` plus virtual `Intensity_*`, marked detected |
| `sar2sar` | detected: `Amplitude_*` bands, or intensity bands (despeckled as their square root) | the same bands, despeckled |

Parameters: `model` (MERLIN weights, `Sentinel-TOPS` default, or `spotlight`,
`stripmap` for TerraSAR-X), `symetrise` (MERLIN, default `true`), `bands`
(restrict to some bands), `patch_size`/`stride_size` (256/254), and
`block_size` (4096): the image is read block by block from memory maps,
so a full scene does not have to fit in memory. `radar-snap describe
DeepDespeckle` lists them; `validate` checks them like any operator's.

SNAP cannot run Python mid-graph, so `run` splits the pipeline around the node,
like the snaphu split, but with nothing to set up by hand:

1. graph a writes the node's source to `output_dir/DeepDespeckle__in.dim`
   (or reuses it if that node has `save:`),
2. DeepDespeckle writes `output_dir/DeepDespeckle.dim` (named by `save:` if
   given) -- everything except the despeckled bands, metadata included, is
   copied over, so SNAP carries on as usual,
3. graph b reads that product and runs the rest of the pipeline; the
   hand-over input is deleted.

A config with DeepDespeckle therefore needs `output_dir`. All graphs are known
before anything runs, so `dump-xml` and `run --dump-xml` write all of them
(`graph.a.xml`, `graph.b.xml`, ...). MERLIN works on the phase-preserving i/q
bands, so it has to come before anything that detects (Multilook, Calibration
to intensity). See [examples/s1_slc_merlin.yaml](examples/s1_slc_merlin.yaml)
and [examples/s1_grd_sar2sar.yaml](examples/s1_grd_sar2sar.yaml).

`despeckle_dimap(src, dst, method=...)` in `radar_snap_lib.despeckle` does the
same on a DIMAP product outside a pipeline.

## Visualising a graph

```bash
radar-snap visualize examples/s1_grd_gamma0.yaml            # -> s1_grd_gamma0.svg
radar-snap visualize results/s1/graph.xml -o graph.png      # a graph that ran
```

Draws a config -- as `run` executes it, `save:` outputs included -- or any GPF
graph XML, also one saved from SNAP Desktop, with Graphviz:

- every operator is a card, its header coloured by kind (input/output,
  Sentinel-1, orbit, calibration, geometry/terrain, SAR processing, InSAR,
  polarimetry, raster/utility, optical), with a legend of the kinds present;
- the card lists only the parameters that differ from SNAP's defaults, shortened
  (a WKT region reads `POLYGON (5 pts)`, a path its file name); hovering a card
  in the SVG shows the operator description and every parameter in full;
- input files and written products are notes at the top and bottom, a
  `.SAFE` product by its folder name; `save:` outputs hang off their node;
- the SnaphuExport -> SnaphuImport hand-over is a dashed edge, as are the
  BEAM-DIMAP hand-overs around a node that runs in Python (DeepDespeckle).

The suffix picks the format: `.svg`, `.png`, `.pdf` need Graphviz' `dot` on
`PATH`, `.dot` writes the Graphviz source without it. From Python:
`visualize("pipeline.yaml", vars={...})`, or `render(graph, "graph.svg")` for a
`Graph`; `graph_from_xml` reads graph XML into a `Graph`.

## The Python builder API

The same node model, built in code instead of YAML:

```python
from radar_snap_lib.snap_ops import Graph

g = Graph()
src   = g.read("S1A_IW_SLC.zip")
orbit = g.apply_orbit_file(src)
split = g.topsar_split(orbit, subswath="IW2", selectedPolarisations="VV")
tc    = g.terrain_correction(split, demName="Copernicus 30m Global DEM",
                             pixelSpacingInMeter=10.0)
g.write(tc, file="out.tif", formatName="GeoTIFF")

g.to_xml("graph.xml")   # inspect
g.run()                 # execute
```

Methods build nodes; nothing runs until `run()`. Both front ends produce
identical XML.

146 of SNAP's 439 operators get a generated method -- the SAR, core-GPF,
polarimetry, InSAR and DEM ones. Signatures carry SNAP's real defaults and
value sets, so the IDE completes them and `ty` checks them. Parameters SNAP
marks required are positional, the rest keyword-only. The remaining operators
(all optical) stay reachable:

```python
g.node("c2rcc.olci", src, salinity=31.0)
```

## Browsing operators

```bash
radar-snap ops                          # the 146 with builder methods
radar-snap ops --filter all             # all 439
radar-snap describe Terrain-Correction  # parameters, defaults, allowed values
```

[docs/operators.md](docs/operators.md) is the same information as a reference
document.

## Regenerating after a SNAP upgrade

`src/radar_snap_lib/snap_ops/operators.json` is a committed snapshot of SNAP's
operator metadata: every parameter's type, default, allowed values and required
flag. Validation and code generation both read it, which is why they need no
JVM. Regenerate it, the builder API and the docs in one step:

```bash
radar-snap gen-registry
git diff src/radar_snap_lib/snap_ops/ docs/operators.md
```

`op_funcs.py` and `operators.json` are generated -- do not edit them by hand. A
`snap`-marked test asserts the snapshot still matches the installed SNAP.

## Tests

```bash
uv run pytest -m "not snap"   # the bulk; no SNAP required
uv run pytest -m snap         # needs a configured esa_snappy environment
```
