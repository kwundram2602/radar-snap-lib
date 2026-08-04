# ASF Search Integration — Design

Date: 2026-08-03
Status: approved

## Problem

`asf_search` is a declared dependency but is wired in only as a thin, hardcoded
helper module ([src/radar_snap_lib/search/search.py](../../../src/radar_snap_lib/search/search.py)).
Three things are wrong with the current state:

1. **The package is not importable.** `src/radar_snap_lib/search/` has no
   `__init__.py`. Nothing in the project can `from radar_snap_lib.search import ...`.
2. **No config layer.** Search parameters (platform, orbit direction, date range,
   processing level) are Python keyword arguments. The pipeline half of the
   library is YAML/OmegaConf-driven; the search half is not.
3. **No AOI handling.** The only geometry input is a four-number bounding box.
   Real areas of interest live in GeoPackages.

A fourth issue surfaced while investigating: the external `esa_snappy`
environment can shadow the project's own dependencies at runtime (see
"esa_snappy interaction" below).

## Goals

- A YAML front end for ASF searches that mirrors the existing `OpsConfig`
  design, validated offline.
- GeoPackage (and Shapefile / GeoJSON) AOI files converted to search WKT.
- Search **and** download driven entirely from config, no CLI flags.
- No regression in how `esa_snappy` is loaded; fix the latent shadowing bug.

## Non-goals

- Feeding search results automatically into a processing pipeline. Search
  configs and pipeline configs are separate files with separate commands; wiring
  them together is the user's job for now.
- Baseline / stack search (`asf.stack_from_id`, InSAR pair selection). Deferred.

## esa_snappy interaction

**Question answered:** `asf_search` remains fully runnable alongside the external
`esa_snappy` environment, but the current loader is fragile.

`ensure_esa_snappy()` in [src/radar_snap_lib/config.py](../../../src/radar_snap_lib/config.py)
does `sys.path.insert(0, site_packages)`. The SNAP venv
(`/home/kjell/projects/py_projects/esa_snappy_env/.venv`) contains more than
`esa_snappy`: `requests`, `urllib3`, `certifi`, `idna`, `charset_normalizer`,
`attrs` and `lxml`. Inserting at position 0 gives all of those precedence over
the project's own versions.

`asf_search` depends on `requests`. Once a process has called into SNAP, a
subsequent search runs against SNAP's `requests`, not the locked one. Measured
on 2026-08-03: both environments happen to hold `requests 2.34.2` and
`urllib3 2.7.0`, so it works today. That is coincidence, and it breaks silently
and order-dependently as soon as `uv lock` moves either version.

**Fix:** append instead of insert. `esa_snappy` and `jpy` exist only in the SNAP
venv, so they still resolve; everything else resolves to the project venv first.
A regression test asserts the SNAP site-packages entry lands after the project's
own entries.

This keeps both libraries in one process. A subprocess/RPC split was considered
and rejected as disproportionate — the conflict is one line of path ordering.

## Module layout

`search/` mirrors `snap_ops/` so both halves of the library read the same way.

| File | Role | Counterpart |
| --- | --- | --- |
| `search/__init__.py` | public exports (currently missing entirely) | `snap_ops/__init__.py` |
| `search/aoi.py` | AOI sources → search WKT | — |
| `search/SearchConfig.py` | OmegaConf front end, validation, → `ASFSearchOptions` | `snap_ops/OpsConfig.py` |
| `search/search.py` | execution: `search()`, `download()` | `snap_ops/runner.py` |

`SearchConfigError` follows `GraphConfigError`: it carries a list of every
problem found plus the source file, and formats them as a single message.

## Config schema

A search config is a flat mapping. Four keys are reserved (consumed by this
library, never forwarded to ASF):

| Key | Meaning |
| --- | --- |
| `aoi` | AOI source: vector file path, `[lon_min, lat_min, lon_max, lat_max]`, or a WKT string |
| `dest` | download target directory |
| `processes` | download parallelism (default 1) |
| `output` | where `search` writes its result table |

Every other key must be a valid `ASFSearchOptions` key. That means `bbox`,
`point`, `linestring`, `relativeOrbit`, `season` and the rest work automatically
without being enumerated here.

```yaml
# searches/testgebiet_s1.yaml
aoi: aois/testgebiet.gpkg

start: 2024-01-01
end: 2024-06-30

platform: SENTINEL-1
flight_direction: ASCENDING
processing_level: SLC
beam_mode: IW
polarization: VV+VH
max_results: 100

dest: /data/s1
processes: 4
output: results/testgebiet.geojson
```

`aoi` resolves to ASF's `intersectsWith`. Supplying both `aoi` and
`intersectsWith` is a validation error.

ASF's own `bbox` key still passes straight through, so `aoi: [w, s, e, n]` and
`bbox: [w, s, e, n]` both work and mean the same thing. `aoi` is the recommended
spelling because it is the one key that accepts every geometry source; `bbox`
survives only because forwarding unknown-to-us ASF keys is what keeps this
config layer from needing maintenance whenever ASF adds an option.

### Key naming

ASF's vocabulary is camelCase (`flightDirection`), the same way SNAP's is
(`pixelSpacingInMeter`). The existing pipeline configs use SNAP's names verbatim,
so search configs use ASF's names verbatim too — and additionally accept
snake_case aliases, translated by a single normalisation function
(`flight_direction` → `flightDirection`). Both spellings are valid; the ASF
documentation stays directly usable.

Mixing both spellings of the *same* key in one config is a validation error.

### Output formats

`output`'s suffix selects the writer, all of which `asf_search` already provides:

| Suffix | Writer |
| --- | --- |
| `.geojson` | `asf.results_to_geojson` |
| `.json` | `asf.results_to_json` |
| `.csv` | `asf.results_to_csv` |
| `.kml` | `asf.results_to_kml` |
| `.metalink` | `asf.results_to_metalink` |

An unknown suffix is a validation error. Omitting `output` prints a summary
table to stdout instead.

## Validation

`asf_search` exposes `asf_search.ASFSearchOptions.validator_map.validator_map`,
a dict of 58 valid option names to their parser functions. This plays the same
role for search configs that `operators.json` plays for graph configs: it makes
validation a pure, offline, network-free operation.

`SearchConfig.validate()` returns a list of every problem found:

- unknown key → error with a `difflib` suggestion, exactly as `OpsConfig._suggest` does
- value rejected by the key's own validator (dates, ints, floats, WKT) → error
  carrying the validator's message
- `aoi` file missing or unreadable → error
- `aoi` together with `intersectsWith` → error
- both spellings of one key → error
- unknown `output` suffix → error
- `dest` missing when `download` is invoked → error (not for `search`)

```text
searches/testgebiet.yaml: 2 problems:
  - unknown key 'flightdirection'. Did you mean: flight_direction?
  - 'aoi': file not found: aois/testgebiet.gpkg
```

## AOI handling (`aoi.py`)

```python
aoi_to_wkt(source: str | Path | Sequence[float] | SearchBounds) -> str
```

Dispatch by type:

- **vector file path** — `geopandas.read_file()`, union all features with
  `union_all()`, reproject to EPSG:4326 with `to_crs(4326)`, hand to
  `asf.validate_wkt()`
- **`SearchBounds` or a 4-sequence** — build the polygon via the existing
  `SearchBounds.as_wkt()`, then `asf.validate_wkt()`
- **string** — treated as WKT, straight to `asf.validate_wkt()`

`asf.validate_wkt(aoi)` returns `(wrapped, unwrapped, repairs)`. The design uses
`wrapped` (antimeridian-correct) and logs each `RepairEntry` at WARNING, so a
simplified or repaired geometry is visible rather than silent — ASF rejects
overly complex geometries, and this is where that gets resolved.

**What that costs, measured.** The ASF API accepts exactly one geometry, so
`validate_wkt` reduces whatever it is handed to a single shape:

| AOI | Result |
| --- | --- |
| one contiguous polygon | unchanged, no repairs |
| a concave outline | preserved exactly (area in = area out) |
| two touching parts | dissolved into one polygon, no area change |
| two disjoint parts | `CONVEX_HULL_INDIVIDUAL` — one polygon spanning both, **gap included** |

Only the last row loses fidelity, and it is an ASF constraint rather than a
choice this library makes. Ruled on 2026-08-04: keep `validate_wkt`, and log
every repair at WARNING so a merged AOI is never silent. A test that expects a
`MultiPolygon` to survive `aoi_to_wkt` contradicts this and must not be written.

A file whose CRS is undefined is a validation error, not an assumption of 4326.

`SearchBounds` is kept as-is. `search_scenes()` becomes internal to
`SearchConfig`. `search_alos_slc()` is removed — it is now
`platform: ALOS` plus `processing_level: L1.1` in a config.

New dependency: `geopandas>=1.0`, matching the sibling InnoLabDL project.

## Authentication

`config.py` already resolves `ESA_SNAPPY_VENV` from environment → `.env` →
`pyproject.toml`. Its `_from_dotenv` is generalised to look up an arbitrary key,
and the same resolution order serves Earthdata credentials:

- `EARTHDATA_TOKEN` (preferred), else
- `EARTHDATA_USERNAME` + `EARTHDATA_PASSWORD`

`ASFSession().auth_with_token(...)` or `.auth_with_creds(...)` accordingly.
`.env.example` gains both.

The session is built **only** in `download()`. `search()` needs no credentials
and stays usable without any Earthdata account.

## Execution (`search/search.py`)

```python
def search(config) -> ASFSearchResults      # validates, then asf.search(**opts)
def download(config) -> list[Path]          # search(), then results.download(dest, session, processes)
```

Both accept the same input types as `OpsConfig.load`: a path, a mapping, or a
`DictConfig`. Both raise `SearchConfigError` before touching the network if the
config is invalid. `download()` creates `dest` if it does not exist and reports
the downloaded paths.

## CLI

Three subcommands on the existing `radar-snap` entry point, no flags — every
knob lives in the config, `dest` included:

```console
radar-snap search   searches/testgebiet_s1.yaml
radar-snap download searches/testgebiet_s1.yaml
radar-snap validate searches/testgebiet_s1.yaml
```

`validate` sniffs the config: a `pipeline` key means a graph config and routes to
`OpsConfig`, otherwise it routes to `SearchConfig`. No fourth command.

## Testing

Everything runs offline in CI.

- `aoi.py` — write a temporary GPKG with geopandas, assert the WKT; assert
  reprojection from a non-4326 CRS; assert multi-feature union; assert the
  missing-CRS error
- `SearchConfig` — unknown key with suggestion, bad date, missing AOI file,
  `aoi` + `intersectsWith` conflict, duplicate-spelling conflict, unknown output
  suffix, snake_case → camelCase normalisation, reserved keys excluded from the
  ASF options
- `search()` / `download()` — `asf.search` and `ASFSearchResults.download`
  mocked; assert the options passed and that validation runs first
- `config.py` — regression test that the SNAP site-packages entry is appended,
  not prepended
- CLI — `validate` routes correctly for both config kinds

A new `network` pytest marker (alongside the existing `snap` marker) covers the
optional live ASF queries, deselected by default.

## Files touched

| File | Change |
| --- | --- |
| `src/radar_snap_lib/search/__init__.py` | new — exports |
| `src/radar_snap_lib/search/aoi.py` | new |
| `src/radar_snap_lib/search/SearchConfig.py` | new |
| `src/radar_snap_lib/search/search.py` | rewritten |
| `src/radar_snap_lib/config.py` | append not insert; generalise dotenv lookup; Earthdata credentials |
| `src/radar_snap_lib/snap_ops/cli.py` | `search` / `download` subcommands; `validate` sniffing |
| `pyproject.toml` | `geopandas>=1.0`; `network` marker |
| `.env.example` | Earthdata credentials |
| `examples/search_s1_slc.yaml` | new — worked example |
| `tests/test_aoi.py`, `tests/test_search_config.py`, `tests/test_search.py`, `tests/test_config.py` | new |
