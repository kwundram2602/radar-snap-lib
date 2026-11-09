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
offline, then execute.
Searching needs no account. Downloading needs NASA Earthdata credentials --
set `EARTHDATA_TOKEN` in your environment or `.env` (see `.env.example`).

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

## Browsing operators

```bash
radar-snap ops                          # the 146 with builder methods
radar-snap ops --filter all             # all 439
radar-snap describe Terrain-Correction  # parameters, defaults, allowed values
```

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

## Running it

```bash
radar-snap validate examples/s1_grd_gamma0.yaml       # no JVM needed
radar-snap dump-xml examples/s1_grd_gamma0.yaml       # inspect the GPF graph
radar-snap run examples/s1_grd_gamma0.yaml --dump-xml /tmp/graph.xml
```
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
is any filter with `save:` (`save: name` -> `name.tif`). Each band is named after its input band and the
filters applied: `Gamma0_VV_db` -> `Gamma0_VV_db_Canny_Closing`.

## Visualising a graph

```bash
radar-snap visualize examples/s1_grd_gamma0.yaml            # -> s1_grd_gamma0.svg
radar-snap visualize results/s1/graph.xml -o graph.png      # a graph that ran
```

## Tests

```bash
uv run pytest -m "not snap"   # no SNAP required
uv run pytest -m snap         # needs a configured esa_snappy environment
```
