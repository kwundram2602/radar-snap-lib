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

### Filters (scikit-image, after SNAP)

Edge detection and morphology on the GeoTIFF a pipeline produces, written as
nodes like any operator. Filters come at the end: they may read each other, but
no SNAP node may read a filter.

```yaml
output_dir: /data/s1/edges

pipeline:
  ...
  Terrain-Correction:
    pixelSpacingInMeter: 10.0
  Write:
    file: /data/s1/gamma0.tif
    formatName: GeoTIFF
  Canny:
    sigma: 2.0
    bands: [1]
  Closing:
    footprint: disk
    radius: 2                 # -> /data/s1/edges/Closing.tif
```

A filter finds its GeoTIFF in one of three ways:

- after a `Write` with `formatName: GeoTIFF` (or `GeoTIFF-BigTIFF`): the file it
  writes; any other format is a config error,
- after a plain `Read` of a `.tif`: that file -- a config of only such a
  `Read` and filters runs without SNAP, so it needs no JVM,
- after any other SNAP node: `run` writes that node as
  `output_dir/<node>.tif` first.

```yaml
output_dir: /data/s1/edges
pipeline:
  Read: {file: /data/s1/gamma0.tif}
  Canny: {sigma: 2.0}
```

Every filter no other filter reads is written to `output_dir/<id>.tif`, and so
is any filter with `save:` (`save: name` -> `name.tif`). The outputs keep the
input's CRS and transform, and each band is named after its input band and the
filters applied: `Gamma0_VV_db` -> `Gamma0_VV_db_Canny_Closing`.

SNAP's GeoTIFF writer keeps band names, units and the no-data value only in a
DIMAP header embedded in the file (TIFF tag 65000), which GDAL and QGIS ignore
-- they show "Band 1". So after SNAP finishes, `run` copies names and units of
every GeoTIFF `Write` into GDAL's band descriptions (existing ones are kept,
SNAP's header stays). Filters read the same header: a band without a GDAL
description takes its name from it, and a file without GDAL nodata its no-data
value, so SNAP's zero-filled border does not show up as an edge. For a file
from elsewhere:

```python
from radar_snap_lib.snap_tiff import label_geotiff

label_geotiff("gamma0_tc.tif")   # -> ["Gamma0_VV_db"]
```

| op         | parameters                                                          | output                       |
|------------|---------------------------------------------------------------------|------------------------------|
| `Canny`    | `sigma` (1.0), `low_threshold`, `high_threshold`, `use_quantiles`   | `uint8` 0/1, nodata 255      |
| `Erosion`  | `footprint` (`disk`, `square`, `diamond`), `radius` (1)             | input dtype and nodata       |
| `Dilation` | as `Erosion`                                                        | input dtype and nodata       |
| `Opening`  | as `Erosion`                                                        | input dtype and nodata       |
| `Closing`  | as `Erosion`                                                        | input dtype and nodata       |

Every op also takes `bands` (1-based, default all) when it reads the GeoTIFF.
Nodata and non-finite pixels never influence valid ones. Morphology on a
Canny output is binary morphology. Each band is held in memory whole, once per
filter whose result is still needed. `radar-snap describe Canny` lists the
parameters; `validate` checks them.

From Python, on any GeoTIFF:

```python
from radar_snap_lib.filters import FilterStep, run_filters

run_filters(
    "gamma0.tif",
    [
        FilterStep("edges", "Canny", params={"sigma": 2.0}, bands=[1]),
        FilterStep("closed", "Closing", source="edges", params={"radius": 2}),
    ],
    outputs={"closed": "edges_closed.tif"},
)
```

`run_graph` returns a `FilterRunResult` for a config with filters: the SNAP
part's result (or `None`) and the GeoTIFFs written.

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
  BEAM-DIMAP hand-overs around a node that runs in Python (DeepDespeckle),
  and the GeoTIFF hand-over into the filters, whose output files are notes too.

The suffix picks the format: `.svg`, `.png`, `.pdf` need Graphviz' `dot` on
`PATH`, `.dot` writes the Graphviz source without it. From Python:
`visualize("pipeline.yaml", vars={...})`, or `render(graph, "graph.svg")` for a
`Graph`; `graph_from_xml` reads graph XML into a `Graph`.

## The Python builder API

The same node model, built in code instead of YAML:

```python
from radar_snap_lib.snap_ops import Graph

g = Graph()
src = g.read("S1A_IW_SLC.zip")
orbit = g.apply_orbit_file(src)
split = g.topsar_split(orbit, subswath="IW2", selectedPolarisations="VV")
tc = g.terrain_correction(
    split, demName="Copernicus 30m Global DEM", pixelSpacingInMeter=10.0
)
g.write(tc, file="out.tif", formatName="GeoTIFF")

g.to_xml("graph.xml")  # inspect
g.run()  # execute
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
