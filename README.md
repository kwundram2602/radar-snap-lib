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

The config is translated into SNAP's own graph XML and handed to
`GraphProcessor` -- the same path the `gpt` CLI takes, so the whole chain streams
tile by tile instead of materialising every intermediate product. The dumped
`graph.xml` opens directly in SNAP Desktop.

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
