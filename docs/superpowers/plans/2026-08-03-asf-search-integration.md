# ASF Search Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `radar-snap-lib` a config-driven ASF search half that mirrors its existing config-driven SNAP half — YAML in, validated offline, GeoPackage AOIs converted to search WKT, results searched and downloaded without a single CLI flag.

**Architecture:** `search/` is laid out as a mirror of `snap_ops/`: a config front end (`SearchConfig.py` ↔ `OpsConfig.py`), an execution layer (`search.py` ↔ `runner.py`), and a package `__init__.py` of exports. Validation runs against `asf_search`'s own `validator_map` the same way graph validation runs against the committed `operators.json` — pure, offline, no network. A separate `aoi.py` converts vector files, bounding boxes and raw WKT into an ASF-accepted search geometry.

**Tech Stack:** Python 3.13, `asf_search` 12.x, OmegaConf, geopandas 1.x, shapely 2.x, pytest.

**Spec:** [docs/superpowers/specs/2026-08-03-asf-search-integration-design.md](../specs/2026-08-03-asf-search-integration-design.md)

## Global Constraints

- Target Python `>=3.13`; ruff `line-length = 88`, `target-version = "py313"`, lint rules `["E", "F", "I", "UP"]`.
- Every module starts with `from __future__ import annotations` and a module docstring, matching existing files.
- Every public module defines `__all__`.
- Run commands with `uv run` (e.g. `uv run pytest`), never a bare `python`.
- Tests must pass with no network access. Live ASF calls go behind the new `network` marker and are deselected by default.
- Never write Claude as co-author or contributor in commit messages.
- Error-collecting classes follow `GraphConfigError`: take `list[str]` plus an optional source, format as `"N problems in <source>:\n  - ...\n  - ..."`.
- Dependency `geopandas>=1.0` is **already added** to `pyproject.toml` and `uv.lock` (`uv add` was run during planning). Verify, do not re-add.
- Never place credentials in `pyproject.toml`. Credentials resolve from the environment and `.env` only.
- Import direction inside `search/` is strictly one-way: `aoi.py` → `SearchConfig.py` → `search.py` → `__init__.py`. `SearchBounds` therefore lives in `aoi.py` (it is geometry), not in `search.py`. Never import "upward"; that is how the cycle gets reintroduced.

---

### Task 1: Fix the esa_snappy path ordering and add Earthdata credential resolution

The SNAP venv ships `requests`, `urllib3`, `certifi`, `idna`, `charset_normalizer`, `attrs` and `lxml` alongside `esa_snappy`. `sys.path.insert(0, ...)` gives all of them precedence over the project's own locked versions, and `asf_search` depends on `requests`. Appending fixes it: `esa_snappy` and `jpy` live nowhere else, so they still resolve.

**Files:**
- Modify: `src/radar_snap_lib/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `radar_snap_lib.config.env_value(key: str) -> str | None` — resolve a setting from the environment, then `.env`. No `pyproject.toml` fallback.
  - `radar_snap_lib.config.EarthdataCredentials` — frozen dataclass with fields `token: str | None = None`, `username: str | None = None`, `password: str | None = None`.
  - `radar_snap_lib.config.earthdata_credentials() -> EarthdataCredentials | None` — `None` when nothing is configured.
  - `ensure_esa_snappy()` keeps its signature `() -> Path`; only the insertion position changes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
"""Tests for external-environment resolution and credential lookup."""

from __future__ import annotations

import sys

import pytest

from radar_snap_lib import config


class TestSnappyPathOrder:
    def test_site_packages_is_appended_not_prepended(self, monkeypatch, tmp_path):
        site_packages = tmp_path / "lib" / "python3.13" / "site-packages"
        site_packages.mkdir(parents=True)
        monkeypatch.setattr(config, "snappy_site_packages", lambda: site_packages)
        monkeypatch.setattr(sys, "path", ["/project/first", "/project/second"])

        config.ensure_esa_snappy()

        assert sys.path[-1] == str(site_packages)
        assert sys.path[0] == "/project/first"

    def test_is_idempotent(self, monkeypatch, tmp_path):
        site_packages = tmp_path / "lib" / "python3.13" / "site-packages"
        site_packages.mkdir(parents=True)
        monkeypatch.setattr(config, "snappy_site_packages", lambda: site_packages)
        monkeypatch.setattr(sys, "path", ["/project/first"])

        config.ensure_esa_snappy()
        config.ensure_esa_snappy()

        assert sys.path.count(str(site_packages)) == 1


class TestEnvValue:
    def test_environment_wins(self, monkeypatch):
        monkeypatch.setenv("EARTHDATA_TOKEN", "from-env")
        assert config.env_value("EARTHDATA_TOKEN") == "from-env"

    def test_falls_back_to_dotenv(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (tmp_path / ".env").write_text(
            "# a comment\nexport EARTHDATA_TOKEN='from-dotenv'\n"
        )
        monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
        assert config.env_value("EARTHDATA_TOKEN") == "from-dotenv"

    def test_missing_key_is_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
        assert config.env_value("EARTHDATA_TOKEN") is None


class TestEarthdataCredentials:
    def test_token_is_preferred(self, monkeypatch):
        monkeypatch.setenv("EARTHDATA_TOKEN", "tok")
        monkeypatch.setenv("EARTHDATA_USERNAME", "user")
        monkeypatch.setenv("EARTHDATA_PASSWORD", "pass")
        creds = config.earthdata_credentials()
        assert creds is not None
        assert creds.token == "tok"

    def test_username_and_password(self, monkeypatch):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        monkeypatch.setenv("EARTHDATA_USERNAME", "user")
        monkeypatch.setenv("EARTHDATA_PASSWORD", "pass")
        creds = config.earthdata_credentials()
        assert creds is not None
        assert (creds.username, creds.password) == ("user", "pass")
        assert creds.token is None

    def test_nothing_configured_is_none(self, monkeypatch, tmp_path):
        for name in (
            "EARTHDATA_TOKEN",
            "EARTHDATA_USERNAME",
            "EARTHDATA_PASSWORD",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
        assert config.earthdata_credentials() is None

    def test_username_without_password_is_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        monkeypatch.setenv("EARTHDATA_USERNAME", "user")
        monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
        monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
        assert config.earthdata_credentials() is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: module 'radar_snap_lib.config' has no attribute 'env_value'`, and `test_site_packages_is_appended_not_prepended` fails because the path lands at index 0.

- [ ] **Step 3: Generalise the dotenv reader**

In `src/radar_snap_lib/config.py`, replace `_from_env` and `_from_dotenv` with key-taking versions and add the public `env_value`:

```python
def _from_env(key: str) -> str | None:
    return os.environ.get(key) or None


def _from_dotenv(root: Path, key: str) -> str | None:
    dotenv = root / ".env"
    if not dotenv.is_file():
        return None
    for raw in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip().removeprefix("export ").strip() != key:
            continue
        return value.strip().strip("'\"") or None
    return None


def env_value(key: str) -> str | None:
    """Resolve a setting from the environment, then the project's ``.env``.

    Deliberately does not consult ``pyproject.toml`` -- that file is committed,
    and this is the path credentials travel.
    """
    value = _from_env(key)
    if value is not None:
        return value
    root = _project_root()
    return None if root is None else _from_dotenv(root, key)
```

Update `_from_pyproject` and `snappy_venv_path` to pass `ENV_VAR` through the new signatures:

```python
def snappy_venv_path() -> Path | None:
    """Return the configured ``esa_snappy`` venv, or ``None`` if unset."""
    root = _project_root()
    value = _from_env(ENV_VAR)
    if value is None and root is not None:
        value = _from_dotenv(root, ENV_VAR) or _from_pyproject(root)
    if value is None:
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute() and root is not None:
        path = root / path
    return path.resolve()
```

- [ ] **Step 4: Append instead of insert**

Still in `config.py`, change the body of `ensure_esa_snappy` and expand its docstring to record why:

```python
def ensure_esa_snappy() -> Path:
    """Put the ``esa_snappy`` venv on ``sys.path``. Idempotent.

    Appended, never prepended.  The SNAP venv ships ``requests``, ``urllib3``,
    ``certifi`` and friends alongside ``esa_snappy``; prepending would let those
    shadow the project's own locked versions, which ``asf_search`` depends on.
    ``esa_snappy`` and ``jpy`` exist nowhere else, so appending still finds them.

    Returns the ``site-packages`` directory that was added.
    """
    site_packages = snappy_site_packages()
    entry = str(site_packages)
    if entry not in sys.path:
        sys.path.append(entry)
    return site_packages
```

- [ ] **Step 5: Add the credential dataclass and lookup**

Add near the top of `config.py`, after the existing constants:

```python
EDL_TOKEN_VAR = "EARTHDATA_TOKEN"
EDL_USERNAME_VAR = "EARTHDATA_USERNAME"
EDL_PASSWORD_VAR = "EARTHDATA_PASSWORD"
```

And at the end of the module:

```python
@dataclass(frozen=True)
class EarthdataCredentials:
    """NASA Earthdata Login credentials, token preferred over username/password."""

    token: str | None = None
    username: str | None = None
    password: str | None = None


def earthdata_credentials() -> EarthdataCredentials | None:
    """Resolve Earthdata Login credentials, or ``None`` if none are configured.

    A token wins over a username/password pair.  A username without a password
    counts as unconfigured -- half a credential is not a credential.
    """
    token = env_value(EDL_TOKEN_VAR)
    if token is not None:
        return EarthdataCredentials(token=token)
    username = env_value(EDL_USERNAME_VAR)
    password = env_value(EDL_PASSWORD_VAR)
    if username is not None and password is not None:
        return EarthdataCredentials(username=username, password=password)
    return None
```

Add `from dataclasses import dataclass` to the imports.

- [ ] **Step 6: Update `.env.example`**

Append to `.env.example`:

```
# NASA Earthdata Login, needed only for `radar-snap download` -- searching works
# without an account. Register at https://urs.earthdata.nasa.gov/
# A token takes precedence over the username/password pair.
EARTHDATA_TOKEN=
# EARTHDATA_USERNAME=
# EARTHDATA_PASSWORD=
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS — 8 tests.

Then confirm nothing else broke: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 8: Lint and type-check**

Run: `uv run ruff format src/radar_snap_lib/config.py tests/test_config.py && uv run ruff check src tests && uv run ty check src`
Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add src/radar_snap_lib/config.py tests/test_config.py .env.example
git commit -m "fix: append esa_snappy site-packages instead of prepending

Prepending let the SNAP venv's requests/urllib3/certifi shadow the
project's locked versions, which asf_search depends on. Also generalises
the .env reader and adds Earthdata credential resolution."
```

---

### Task 2: AOI sources to search WKT

**Files:**
- Create: `src/radar_snap_lib/search/aoi.py`
- Create: `src/radar_snap_lib/search/__init__.py`
- Delete: `src/radar_snap_lib/search/search.py` (its `SearchBounds` moves into `aoi.py`; Task 4 creates the file fresh with the execution layer)
- Modify: `pyproject.toml` (add the `network` marker)
- Test: `tests/test_aoi.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `radar_snap_lib.search.aoi.SearchBounds` — the existing frozen dataclass, moved here unchanged: fields `lon_min`, `lat_min`, `lon_max`, `lat_max` (all `float`), method `as_wkt() -> str`.
  - `radar_snap_lib.search.aoi.AOIError(ValueError)`
  - `radar_snap_lib.search.aoi.aoi_to_wkt(source: str | Path | Sequence[float] | SearchBounds) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aoi.py`:

```python
"""Tests for turning AOI sources into ASF-accepted search WKT."""

from __future__ import annotations

import logging

import geopandas as gpd
import pytest
from shapely import wkt as shapely_wkt
from shapely.geometry import Polygon, box

from radar_snap_lib.search.aoi import AOIError, SearchBounds, aoi_to_wkt


@pytest.fixture
def gpkg(tmp_path):
    """A two-feature GeoPackage in EPSG:4326."""
    path = tmp_path / "aoi.gpkg"
    frame = gpd.GeoDataFrame(
        {"name": ["west", "east"]},
        geometry=[box(10.0, 50.0, 11.0, 51.0), box(12.0, 50.0, 13.0, 51.0)],
        crs="EPSG:4326",
    )
    frame.to_file(path, driver="GPKG")
    return path


class TestVectorFiles:
    def test_disjoint_features_become_one_hulled_polygon(self, gpkg):
        # ASF accepts exactly one geometry, so validate_wkt convex-hulls
        # disjoint parts together. The hull spans both boxes, gap included.
        geometry = shapely_wkt.loads(aoi_to_wkt(gpkg))
        assert geometry.geom_type == "Polygon"
        assert geometry.bounds == (10.0, 50.0, 13.0, 51.0)

    def test_a_contiguous_aoi_is_preserved_exactly(self, tmp_path):
        path = tmp_path / "concave.gpkg"
        shape = Polygon([(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)])
        gpd.GeoDataFrame(geometry=[shape], crs="EPSG:4326").to_file(
            path, driver="GPKG"
        )
        assert shapely_wkt.loads(aoi_to_wkt(path)).area == pytest.approx(shape.area)

    def test_merging_is_logged_so_it_is_never_silent(self, gpkg, caplog):
        with caplog.at_level(logging.WARNING):
            aoi_to_wkt(gpkg)
        assert any("CONVEX_HULL" in record.message for record in caplog.records)

    def test_accepts_a_string_path(self, gpkg):
        assert aoi_to_wkt(str(gpkg)) == aoi_to_wkt(gpkg)

    def test_reprojects_to_wgs84(self, tmp_path):
        path = tmp_path / "utm.gpkg"
        frame = gpd.GeoDataFrame(
            geometry=[box(500000.0, 5600000.0, 510000.0, 5610000.0)],
            crs="EPSG:32632",
        )
        frame.to_file(path, driver="GPKG")
        bounds = shapely_wkt.loads(aoi_to_wkt(path)).bounds
        assert 8.0 < bounds[0] < 10.0
        assert 50.0 < bounds[1] < 51.0

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(AOIError, match="not found"):
            aoi_to_wkt(tmp_path / "nope.gpkg")

    def test_missing_crs_raises(self, tmp_path):
        path = tmp_path / "nocrs.geojson"
        gpd.GeoDataFrame(geometry=[box(10.0, 50.0, 11.0, 51.0)]).to_file(path)
        with pytest.raises(AOIError, match="no CRS"):
            aoi_to_wkt(path)

    def test_empty_file_raises(self, tmp_path):
        path = tmp_path / "empty.gpkg"
        gpd.GeoDataFrame(
            {"name": []}, geometry=[], crs="EPSG:4326"
        ).to_file(path, driver="GPKG")
        with pytest.raises(AOIError, match="no features"):
            aoi_to_wkt(path)


class TestOtherSources:
    def test_search_bounds(self):
        geometry = shapely_wkt.loads(
            aoi_to_wkt(SearchBounds(10.0, 50.0, 11.0, 51.0))
        )
        assert geometry.bounds == (10.0, 50.0, 11.0, 51.0)

    def test_four_number_sequence(self):
        assert aoi_to_wkt([10.0, 50.0, 11.0, 51.0]) == aoi_to_wkt(
            SearchBounds(10.0, 50.0, 11.0, 51.0)
        )

    def test_wrong_length_sequence_raises(self):
        with pytest.raises(AOIError, match="four numbers"):
            aoi_to_wkt([10.0, 50.0, 11.0])

    def test_wkt_string_passes_through(self):
        source = "POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))"
        assert shapely_wkt.loads(aoi_to_wkt(source)).bounds == (10.0, 50.0, 11.0, 51.0)

    def test_invalid_wkt_string_raises(self):
        with pytest.raises(AOIError):
            aoi_to_wkt("POLYGON((oops))")

    def test_unsupported_type_raises(self):
        with pytest.raises(AOIError, match="Unsupported AOI"):
            aoi_to_wkt(42)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_aoi.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'radar_snap_lib.search.aoi'`.

- [ ] **Step 3: Delete the old `search/search.py`**

```bash
git rm src/radar_snap_lib/search/search.py
```

`SearchBounds` moves into `aoi.py` in the next step. `search_scenes` and `search_alos_slc` are gone for good — the config layer replaces them (`platform: ALOS` plus `processing_level: L1.1` is what `search_alos_slc` did). Task 4 creates `search.py` again as the execution layer.

- [ ] **Step 4: Write `aoi.py`**

Create `src/radar_snap_lib/search/aoi.py`:

```python
"""Turn an area of interest into WKT that ASF will accept.

Four kinds of source are supported: a vector file (GeoPackage, Shapefile,
GeoJSON -- anything GDAL reads), a :class:`SearchBounds`, a four-number
sequence, and a raw WKT string.  All of them end up in ``asf.validate_wkt``,
which repairs and simplifies geometry until the ASF API will take it -- ASF
rejects overly complex outlines, and this is where that gets resolved.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asf_search as asf
from shapely.geometry.base import BaseGeometry

__all__ = ["AOIError", "SearchBounds", "aoi_to_wkt"]

_LOG = logging.getLogger(__name__)

#: Leading tokens that mark a string as WKT rather than a file path.
_WKT_PREFIXES = (
    "POINT",
    "LINESTRING",
    "POLYGON",
    "MULTIPOINT",
    "MULTILINESTRING",
    "MULTIPOLYGON",
    "GEOMETRYCOLLECTION",
)


class AOIError(ValueError):
    """Raised when an AOI source cannot be turned into search geometry."""


@dataclass(frozen=True)
class SearchBounds:
    """Bounding box in WGS-84 degrees."""

    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float

    def as_wkt(self) -> str:
        """Return a WKT polygon string for the bounding box."""
        corners = [
            (self.lon_min, self.lat_min),
            (self.lon_max, self.lat_min),
            (self.lon_max, self.lat_max),
            (self.lon_min, self.lat_max),
            (self.lon_min, self.lat_min),
        ]
        coords = ", ".join(f"{lon} {lat}" for lon, lat in corners)
        return f"POLYGON(({coords}))"


def aoi_to_wkt(source: str | Path | Sequence[float] | SearchBounds) -> str:
    """Return ASF-ready WKT for ``source``.

    Args:
        source: A vector file path, a :class:`SearchBounds`, a
            ``[lon_min, lat_min, lon_max, lat_max]`` sequence, or a WKT string.

    Returns:
        WKT in EPSG:4326, wrapped at the antimeridian and simplified as far as
        ASF requires.

    Raises:
        AOIError: If the source cannot be read or is not valid geometry.
    """
    geometry = _to_geometry(source)
    try:
        wrapped, _unwrapped, repairs = asf.validate_wkt(geometry)
    except Exception as exc:  # asf raises ASFWKTError and shapely errors alike
        raise AOIError(f"Invalid AOI geometry: {exc}") from exc

    for repair in repairs:
        _LOG.warning("AOI adjusted for ASF: %s", repair)
    return str(wrapped.wkt)


def _to_geometry(source: Any) -> BaseGeometry | str:
    """Normalise any supported source to geometry or a WKT string."""
    if isinstance(source, SearchBounds):
        return source.as_wkt()
    if isinstance(source, (str, Path)):
        if isinstance(source, str) and _looks_like_wkt(source):
            return source
        return _read_vector(Path(source))
    if isinstance(source, Sequence):
        return _from_sequence(source)
    raise AOIError(
        f"Unsupported AOI source of type {type(source).__name__}. Give a vector "
        "file path, a SearchBounds, four numbers, or a WKT string."
    )


def _looks_like_wkt(source: str) -> bool:
    return source.lstrip().upper().startswith(_WKT_PREFIXES)


def _from_sequence(source: Sequence[Any]) -> str:
    values = list(source)
    if len(values) != 4:
        raise AOIError(
            f"A bounding-box AOI needs four numbers "
            f"(lon_min, lat_min, lon_max, lat_max), got {len(values)}."
        )
    try:
        return SearchBounds(*(float(value) for value in values)).as_wkt()
    except (TypeError, ValueError) as exc:
        raise AOIError(f"Bounding-box AOI must be numeric: {exc}") from exc


def _read_vector(path: Path) -> BaseGeometry:
    """Read every feature from a vector file as one WGS-84 geometry."""
    import geopandas as gpd  # noqa: PLC0415 -- keeps import cost off the CLI

    if not path.is_file():
        raise AOIError(f"AOI file not found: {path}")
    try:
        frame = gpd.read_file(path)
    except Exception as exc:
        raise AOIError(f"Cannot read AOI file {path}: {exc}") from exc

    if frame.empty:
        raise AOIError(f"AOI file has no features: {path}")
    if frame.crs is None:
        raise AOIError(
            f"AOI file has no CRS: {path}. Assign one (EPSG:4326 is expected) "
            "and try again."
        )
    return frame.to_crs(4326).union_all()
```

- [ ] **Step 5: Write the package `__init__.py`**

Create `src/radar_snap_lib/search/__init__.py`. The `search`/`download` entries are added in Task 4; for now export what exists:

```python
"""Find and fetch SAR scenes from the ASF archive.

The search half of the library mirrors the SNAP half: a YAML config describes
what to look for, validation runs offline, and one call executes it.

    from radar_snap_lib.search import search

    results = search("searches/testgebiet_s1.yaml")
"""

from radar_snap_lib.search.aoi import AOIError, SearchBounds, aoi_to_wkt

__all__ = ["AOIError", "SearchBounds", "aoi_to_wkt"]
```

- [ ] **Step 6: Register the `network` marker**

In `pyproject.toml`, extend `[tool.pytest.ini_options] markers`:

```toml
markers = [
    "snap: requires a working esa_snappy / SNAP install (boots the JVM)",
    "network: hits the live ASF API (deselected by default, see addopts)",
]
addopts = "-m 'not network'"
```

Also confirm `geopandas>=1.0` is present in `[project] dependencies`. If it is missing, run `uv add "geopandas>=1.0"`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_aoi.py -v`
Expected: PASS — 12 tests.

Then: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 8: Lint and type-check**

Run: `uv run ruff format src/radar_snap_lib/search tests/test_aoi.py && uv run ruff check src tests && uv run ty check src`
Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add src/radar_snap_lib/search tests/test_aoi.py pyproject.toml uv.lock
git commit -m "feat: convert vector AOIs, bounding boxes and WKT to search geometry

Adds radar_snap_lib.search.aoi and makes search/ an importable package --
it had no __init__.py at all."
```

---

### Task 3: The search config front end

`asf_search.ASFSearchOptions.validator_map.validator_map` maps 58 valid option names to their parser functions. `validate(key, value)` raises `KeyError` for an unknown key and `ValueError` for a bad value. That is the offline registry this config layer validates against, exactly as `OpsConfig` validates against `operators.json`.

**Files:**
- Create: `src/radar_snap_lib/search/SearchConfig.py`
- Modify: `src/radar_snap_lib/search/__init__.py`
- Test: `tests/test_search_config.py` (create)

**Interfaces:**
- Consumes: `aoi_to_wkt`, `AOIError` from Task 2.
- Produces:
  - `SearchConfigError(Exception)` with `.errors: list[str]` and `.source: str | None`
  - `SearchConfig.load(config: str | Path | DictConfig | dict, *, source: str | None = None) -> SearchConfig`
  - `SearchConfig.validate() -> list[str]`
  - `SearchConfig.search_options() -> dict[str, Any]` — validated ASF kwargs, reserved keys removed, `aoi` resolved into `intersectsWith`
  - `SearchConfig.dest -> Path | None`, `.processes -> int`, `.output -> Path | None`
  - `SearchConfig.write_results(results, path) -> None`
  - Module constants `RESERVED_KEYS: frozenset[str]`, `OUTPUT_WRITERS: dict[str, Callable]`, `ALIASES: dict[str, str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_config.py`:

```python
"""Tests for the search YAML front end: aliasing, validation, option building."""

from __future__ import annotations

import pytest

from radar_snap_lib.search.SearchConfig import (
    ALIASES,
    RESERVED_KEYS,
    SearchConfig,
    SearchConfigError,
)

BASE = {
    "aoi": "POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))",
    "platform": "SENTINEL-1",
    "start": "2024-01-01",
    "end": "2024-06-30",
}


class TestAliases:
    def test_snake_case_maps_to_camel_case(self):
        assert ALIASES["flight_direction"] == "flightDirection"
        assert ALIASES["max_results"] == "maxResults"
        assert ALIASES["processing_level"] == "processingLevel"

    def test_every_asf_key_kept_its_own_alias(self):
        # A dict comprehension cannot show a collision in its own values --
        # a clash silently overwrites the earlier entry instead. Only the
        # count against the source map catches that.
        from asf_search.ASFSearchOptions.validator_map import validator_map

        assert len(ALIASES) == len(validator_map)

    def test_camel_case_still_accepted(self):
        config = SearchConfig.load({**BASE, "flightDirection": "ASCENDING"})
        assert config.search_options()["flightDirection"] == "ASCENDING"

    def test_snake_case_is_translated(self):
        config = SearchConfig.load({**BASE, "flight_direction": "ASCENDING"})
        assert config.search_options()["flightDirection"] == "ASCENDING"

    def test_both_spellings_of_one_key_is_an_error(self):
        config = SearchConfig.load(
            {**BASE, "flight_direction": "ASCENDING", "flightDirection": "DESCENDING"}
        )
        errors = config.validate()
        assert any("flightDirection" in e and "twice" in e for e in errors)


class TestValidation:
    def test_a_good_config_has_no_errors(self):
        assert SearchConfig.load(BASE).validate() == []

    def test_unknown_key_is_reported_with_a_suggestion(self):
        errors = SearchConfig.load({**BASE, "flightdirection": "ASCENDING"}).validate()
        assert len(errors) == 1
        assert "flightdirection" in errors[0]
        assert "flight_direction" in errors[0]

    def test_bad_date_is_reported(self):
        errors = SearchConfig.load({**BASE, "start": "not-a-date"}).validate()
        assert any("start" in e for e in errors)

    def test_bad_int_is_reported(self):
        errors = SearchConfig.load({**BASE, "max_results": "many"}).validate()
        assert any("maxResults" in e or "max_results" in e for e in errors)

    def test_all_errors_are_collected(self):
        errors = SearchConfig.load(
            {**BASE, "bogus": 1, "alsoBogus": 2, "start": "nope"}
        ).validate()
        assert len(errors) == 3

    def test_missing_aoi_and_geometry_is_an_error(self):
        errors = SearchConfig.load({"platform": "SENTINEL-1"}).validate()
        assert any("aoi" in e for e in errors)

    def test_aoi_plus_intersects_with_is_an_error(self):
        errors = SearchConfig.load(
            {**BASE, "intersectsWith": "POLYGON((0 0, 1 0, 1 1, 0 0))"}
        ).validate()
        assert any("intersectsWith" in e for e in errors)

    def test_bbox_alone_satisfies_the_geometry_requirement(self):
        assert SearchConfig.load(
            {"platform": "SENTINEL-1", "bbox": [10, 50, 11, 51]}
        ).validate() == []

    def test_missing_aoi_file_is_reported(self, tmp_path):
        errors = SearchConfig.load({**BASE, "aoi": str(tmp_path / "no.gpkg")}).validate()
        assert any("not found" in e for e in errors)

    def test_unknown_output_suffix_is_an_error(self):
        errors = SearchConfig.load({**BASE, "output": "results.txt"}).validate()
        assert any("output" in e and ".geojson" in e for e in errors)

    def test_root_must_be_a_mapping(self):
        with pytest.raises(SearchConfigError, match="mapping"):
            SearchConfig.load([1, 2, 3])


class TestSearchOptions:
    def test_reserved_keys_are_not_forwarded(self):
        options = SearchConfig.load(
            {**BASE, "dest": "/data", "processes": 4, "output": "r.geojson"}
        ).search_options()
        assert not RESERVED_KEYS & set(options)

    def test_aoi_becomes_intersects_with(self):
        options = SearchConfig.load(BASE).search_options()
        assert "POLYGON" in options["intersectsWith"]
        assert "aoi" not in options

    def test_invalid_config_raises_before_building_options(self):
        with pytest.raises(SearchConfigError) as excinfo:
            SearchConfig.load({**BASE, "bogus": 1}).search_options()
        assert excinfo.value.errors

    def test_accessors_expose_reserved_keys(self, tmp_path):
        config = SearchConfig.load(
            {**BASE, "dest": str(tmp_path), "processes": 4, "output": "r.geojson"}
        )
        assert config.dest == tmp_path
        assert config.processes == 4
        assert config.output.name == "r.geojson"

    def test_processes_defaults_to_one(self):
        assert SearchConfig.load(BASE).processes == 1

    def test_dest_is_none_when_unset(self):
        assert SearchConfig.load(BASE).dest is None


class TestYamlLoading:
    def test_loads_from_a_file(self, tmp_path):
        path = tmp_path / "s.yaml"
        path.write_text(
            "aoi: POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))\n"
            "platform: SENTINEL-1\n"
            "flight_direction: ASCENDING\n"
        )
        config = SearchConfig.load(path)
        assert config.source == str(path)
        assert config.validate() == []

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SearchConfig.load(tmp_path / "nope.yaml")

    def test_error_message_names_the_source(self, tmp_path):
        path = tmp_path / "s.yaml"
        path.write_text("platform: SENTINEL-1\nbogus: 1\n")
        with pytest.raises(SearchConfigError, match=str(path)):
            SearchConfig.load(path).search_options()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_search_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'radar_snap_lib.search.SearchConfig'`.

- [ ] **Step 3: Write `SearchConfig.py`**

Create `src/radar_snap_lib/search/SearchConfig.py`:

```python
"""YAML front end for ASF archive searches.

A config is a flat mapping.  Four keys belong to this library and are never
forwarded to ASF::

    aoi        AOI source: vector file, [lon_min, lat_min, lon_max, lat_max], or WKT
    dest       download target directory
    processes  download parallelism
    output     where to write the result table

Everything else must be a valid ``ASFSearchOptions`` key.  Both ASF's own
camelCase spelling and a snake_case alias are accepted::

    aoi: aois/testgebiet.gpkg
    platform: SENTINEL-1
    flight_direction: ASCENDING
    processing_level: SLC
    start: 2024-01-01
    end: 2024-06-30
    dest: /data/s1
    output: results/testgebiet.geojson

Validation runs entirely against ``asf_search``'s own validator map, so a config
can be checked without a network connection or an Earthdata account.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import asf_search as asf
from asf_search.ASFSearchOptions.validator_map import validate, validator_map
from omegaconf import DictConfig, ListConfig, OmegaConf

from radar_snap_lib.search.aoi import AOIError, aoi_to_wkt

__all__ = [
    "ALIASES",
    "OUTPUT_WRITERS",
    "RESERVED_KEYS",
    "SearchConfig",
    "SearchConfigError",
]

#: Keys this library consumes; never forwarded to ASF.
RESERVED_KEYS = frozenset({"aoi", "dest", "processes", "output"})

#: ASF option keys that already supply a search geometry.
GEOMETRY_KEYS = frozenset({"intersectsWith", "bbox", "point", "linestring", "circle"})

#: Output suffix -> the ``asf_search`` writer that produces it.
OUTPUT_WRITERS: dict[str, Callable[[Any], Any]] = {
    ".geojson": asf.results_to_geojson,
    ".json": asf.results_to_json,
    ".csv": asf.results_to_csv,
    ".kml": asf.results_to_kml,
    ".metalink": asf.results_to_metalink,
}


def _snake(name: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


#: snake_case alias -> ASF's own camelCase key.  Verified collision-free.
ALIASES: dict[str, str] = {_snake(key): key for key in validator_map}


class SearchConfigError(Exception):
    """Raised when a config does not describe a valid search.

    Carries every problem found, not just the first.
    """

    def __init__(self, errors: list[str], source: str | None = None) -> None:
        self.errors = errors
        self.source = source
        location = f" in {source}" if source else ""
        body = "\n".join(f"  - {error}" for error in errors)
        plural = "s" if len(errors) != 1 else ""
        super().__init__(f"{len(errors)} problem{plural}{location}:\n{body}")


def _suggest(name: str, candidates: Any) -> str:
    matches = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.6)
    return f" Did you mean: {', '.join(matches)}?" if matches else ""


class SearchConfig:
    """A loaded and (optionally) validated ASF search config."""

    def __init__(self, config: DictConfig, *, source: str | None = None) -> None:
        self.config = config
        self.source = source

    # -- loading ----------------------------------------------------------- #

    @classmethod
    def load(
        cls,
        config: str | Path | DictConfig | dict[str, Any] | Any,
        *,
        source: str | None = None,
    ) -> SearchConfig:
        """Load a config from a YAML path, a mapping, or an existing DictConfig."""
        if isinstance(config, (str, Path)):
            path = Path(config)
            if not path.is_file():
                raise FileNotFoundError(f"Config not found: {path}")
            source = source or str(path)
            loaded = OmegaConf.load(path)
        elif isinstance(config, DictConfig):
            loaded = config
        else:
            loaded = OmegaConf.create(config)

        if not isinstance(loaded, DictConfig):
            raise SearchConfigError(["Config root must be a mapping"], source)
        return cls(loaded, source=source)

    # -- key handling ------------------------------------------------------ #

    def _raw(self) -> dict[str, Any]:
        resolved = OmegaConf.to_object(self.config)
        return dict(resolved) if isinstance(resolved, dict) else {}

    def _canonical(self) -> tuple[dict[str, Any], list[str]]:
        """Split the config into ASF keys and reserved keys, canonicalising names.

        Returns the ASF keys (camelCase) and any naming errors found.
        """
        options: dict[str, Any] = {}
        errors: list[str] = []
        seen: dict[str, str] = {}

        for key, value in self._raw().items():
            name = str(key)
            if name in RESERVED_KEYS:
                continue
            canonical = name if name in validator_map else ALIASES.get(name)
            if canonical is None:
                errors.append(
                    f"Unknown key {name!r}.{_suggest(name, [*ALIASES, *validator_map])}"
                )
                continue
            if canonical in seen:
                errors.append(
                    f"Key {canonical!r} is set twice, as {seen[canonical]!r} "
                    f"and {name!r}. Pick one spelling."
                )
                continue
            seen[canonical] = name
            options[canonical] = value
        return options, errors

    # -- reserved-key accessors -------------------------------------------- #

    @property
    def dest(self) -> Path | None:
        """Download target directory, or ``None`` if unset."""
        value = self._raw().get("dest")
        return None if value is None else Path(str(value)).expanduser()

    @property
    def processes(self) -> int:
        """Download parallelism. Defaults to 1."""
        return int(self._raw().get("processes", 1))

    @property
    def output(self) -> Path | None:
        """Where to write the result table, or ``None`` for stdout."""
        value = self._raw().get("output")
        return None if value is None else Path(str(value)).expanduser()

    # -- validation -------------------------------------------------------- #

    def validate(self) -> list[str]:
        """Check the config offline. An empty list means it is valid."""
        return self._check()[1]

    def _check(self) -> tuple[str | None, list[str]]:
        """Validate, returning the resolved AOI WKT alongside the errors.

        Resolving an AOI means reading a vector file, reprojecting it and
        handing it to ASF for repair -- too expensive to do twice, and doing
        it twice would log every geometry repair twice as well.  So the one
        resolution happens here and ``search_options`` reuses the result.
        """
        options, errors = self._canonical()
        raw = self._raw()

        for key, value in options.items():
            try:
                validate(key, _plain(value))
            except ValueError as exc:
                errors.append(f"{key}: {exc}")

        wkt, geometry_errors = self._check_geometry(raw, options)
        errors.extend(geometry_errors)
        errors.extend(self._check_output(raw))
        return wkt, errors

    def _check_geometry(
        self, raw: dict[str, Any], options: dict[str, Any]
    ) -> tuple[str | None, list[str]]:
        aoi = raw.get("aoi")
        overlap = sorted(GEOMETRY_KEYS & set(options))

        if aoi is not None and overlap:
            return None, [
                f"'aoi' cannot be combined with {', '.join(overlap)}. "
                "Use one geometry source."
            ]
        if aoi is None:
            if overlap:
                return None, []
            return None, [
                "No search geometry: set 'aoi' to a vector file, four numbers, "
                "or a WKT string."
            ]
        try:
            return aoi_to_wkt(_plain(aoi)), []
        except AOIError as exc:
            return None, [f"'aoi': {exc}"]

    def _check_output(self, raw: dict[str, Any]) -> list[str]:
        value = raw.get("output")
        if value is None:
            return []
        suffix = Path(str(value)).suffix.lower()
        if suffix in OUTPUT_WRITERS:
            return []
        allowed = ", ".join(sorted(OUTPUT_WRITERS))
        return [f"'output': unsupported suffix {suffix!r}. Use one of: {allowed}"]

    # -- option building --------------------------------------------------- #

    def search_options(self) -> dict[str, Any]:
        """Validate, then return the keyword arguments for ``asf.search``."""
        wkt, errors = self._check()
        if errors:
            raise SearchConfigError(errors, self.source)

        options, _ = self._canonical()
        options = {key: _plain(value) for key, value in options.items()}
        if wkt is not None:
            options["intersectsWith"] = wkt
        return options

    # -- result output ----------------------------------------------------- #

    @staticmethod
    def write_results(results: Any, path: Path) -> None:
        """Write search results to ``path``, choosing the writer by suffix."""
        writer = OUTPUT_WRITERS[path.suffix.lower()]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(writer(results)), encoding="utf-8")


def _plain(value: Any) -> Any:
    """Convert OmegaConf containers to plain Python objects."""
    if isinstance(value, (DictConfig, ListConfig)):
        return OmegaConf.to_object(value)
    return value
```

- [ ] **Step 4: Export it**

Update `src/radar_snap_lib/search/__init__.py` imports and `__all__`:

```python
from radar_snap_lib.search.aoi import AOIError, SearchBounds, aoi_to_wkt
from radar_snap_lib.search.SearchConfig import SearchConfig, SearchConfigError

__all__ = [
    "AOIError",
    "SearchBounds",
    "SearchConfig",
    "SearchConfigError",
    "aoi_to_wkt",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_search_config.py -v`
Expected: PASS — 25 tests.

Then: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff format src/radar_snap_lib/search tests/test_search_config.py && uv run ruff check src tests && uv run ty check src`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/radar_snap_lib/search tests/test_search_config.py
git commit -m "feat: YAML front end for ASF searches

Validates offline against asf_search's own validator map, the same way
graph configs validate against operators.json. Accepts ASF's camelCase
keys and snake_case aliases."
```

---

### Task 4: Search and download execution

**Files:**
- Create: `src/radar_snap_lib/search/runner.py` (the execution layer; named to mirror `snap_ops/runner.py`)
- Modify: `src/radar_snap_lib/search/__init__.py`
- Test: `tests/test_search_runner.py` (create)

**Interfaces:**
- Consumes: `SearchConfig`, `SearchConfigError` from Task 3; `earthdata_credentials`, `EDL_TOKEN_VAR`, `EDL_USERNAME_VAR` from Task 1.
- Produces:
  - `search(config, *, write_output: bool = True) -> ASFSearchResults`
  - `download(config) -> list[Path]`
  - `earthdata_session() -> ASFSession`
- Does **not** import from `aoi.py`; `SearchBounds` is re-exported by `__init__.py` straight from `aoi.py`.
- The module is `runner.py`, **not** `search.py`. `__init__.py` exports a function named `search`, and `from radar_snap_lib.search.search import search` would bind that function over the submodule of the same name in the package namespace — after which `from radar_snap_lib.search import search` yields the function and the module becomes unreachable by that path, so no test can patch it. `runner.py` also makes the mirror of `snap_ops/runner.py` literal.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search_runner.py`:

```python
"""Tests for search and download execution, with ASF mocked out."""

from __future__ import annotations

import asf_search as asf
import pytest

from radar_snap_lib.search import runner as search_module
from radar_snap_lib.search.SearchConfig import SearchConfigError

BASE = {
    "aoi": "POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))",
    "platform": "SENTINEL-1",
}


class FakeResults(asf.ASFSearchResults):
    """A real ASFSearchResults (so the geojson writer works) that records
    download calls instead of making them."""

    def __init__(self, items=()):
        super().__init__(items)
        self.download_calls = []

    def download(self, path, session=None, processes=1, **kwargs):
        self.download_calls.append({"path": path, "processes": processes})


@pytest.fixture
def captured(monkeypatch):
    """Capture the options handed to asf.search and return canned results."""
    calls = {}
    results = FakeResults()

    def fake_search(**kwargs):
        calls.update(kwargs)
        return results

    monkeypatch.setattr(search_module.asf, "search", fake_search)
    return calls, results


class TestSearch:
    def test_options_reach_asf(self, captured):
        calls, _ = captured
        search_module.search({**BASE, "flight_direction": "ASCENDING"})
        assert calls["platform"] == "SENTINEL-1"
        assert calls["flightDirection"] == "ASCENDING"
        assert "POLYGON" in calls["intersectsWith"]

    def test_reserved_keys_do_not_reach_asf(self, captured):
        calls, _ = captured
        search_module.search({**BASE, "dest": "/data", "processes": 4})
        assert "dest" not in calls
        assert "processes" not in calls

    def test_results_are_returned(self, captured):
        _, results = captured
        assert search_module.search(BASE) is results

    def test_invalid_config_never_reaches_the_network(self, monkeypatch):
        def explode(**kwargs):
            raise AssertionError("asf.search must not be called")

        monkeypatch.setattr(search_module.asf, "search", explode)
        with pytest.raises(SearchConfigError):
            search_module.search({**BASE, "bogus": 1})

    def test_output_file_is_written(self, captured, tmp_path):
        target = tmp_path / "out" / "results.geojson"
        search_module.search({**BASE, "output": str(target)})
        assert target.is_file()
        assert "FeatureCollection" in target.read_text()

    def test_write_output_false_skips_the_file(self, captured, tmp_path):
        target = tmp_path / "results.geojson"
        search_module.search({**BASE, "output": str(target)}, write_output=False)
        assert not target.exists()


class TestDownload:
    def test_downloads_to_dest(self, captured, monkeypatch, tmp_path):
        _, results = captured
        monkeypatch.setattr(
            search_module, "earthdata_session", lambda: "session-object"
        )
        dest = tmp_path / "scenes"
        search_module.download({**BASE, "dest": str(dest), "processes": 3})

        assert dest.is_dir()
        assert results.download_calls == [{"path": str(dest), "processes": 3}]

    def test_missing_dest_is_a_config_error(self, captured):
        with pytest.raises(SearchConfigError, match="dest"):
            search_module.download(BASE)


class TestSession:
    def test_token_credentials(self, monkeypatch):
        from radar_snap_lib.config import EarthdataCredentials

        monkeypatch.setattr(
            search_module,
            "earthdata_credentials",
            lambda: EarthdataCredentials(token="tok"),
        )
        calls = {}

        class FakeSession:
            def auth_with_token(self, token):
                calls["token"] = token
                return self

            def auth_with_creds(self, username, password):
                raise AssertionError("token should win")

        monkeypatch.setattr(search_module.asf, "ASFSession", FakeSession)
        search_module.earthdata_session()
        assert calls == {"token": "tok"}

    def test_username_password_credentials(self, monkeypatch):
        from radar_snap_lib.config import EarthdataCredentials

        monkeypatch.setattr(
            search_module,
            "earthdata_credentials",
            lambda: EarthdataCredentials(username="u", password="p"),
        )
        calls = {}

        class FakeSession:
            def auth_with_creds(self, username, password):
                calls["creds"] = (username, password)
                return self

        monkeypatch.setattr(search_module.asf, "ASFSession", FakeSession)
        search_module.earthdata_session()
        assert calls == {"creds": ("u", "p")}

    def test_no_credentials_raises(self, monkeypatch):
        monkeypatch.setattr(search_module, "earthdata_credentials", lambda: None)
        with pytest.raises(RuntimeError, match="EARTHDATA_TOKEN"):
            search_module.earthdata_session()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_search_runner.py -v`
Expected: FAIL — `ImportError: cannot import name 'runner' from 'radar_snap_lib.search'`.

- [ ] **Step 3: Write `search/runner.py`**

Create the file:

```python
"""Execute ASF archive searches and downloads.

The counterpart to ``snap_ops.runner``: a config goes in, it is validated
offline first, and only then does anything touch the network.  Searching needs
no credentials; downloading does, and only ``download`` builds a session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asf_search as asf
from omegaconf import DictConfig

from radar_snap_lib.config import (
    EDL_TOKEN_VAR,
    EDL_USERNAME_VAR,
    earthdata_credentials,
)
from radar_snap_lib.search.SearchConfig import SearchConfig, SearchConfigError

__all__ = ["download", "earthdata_session", "search"]

ConfigSource = str | Path | DictConfig | dict[str, Any]


def earthdata_session() -> asf.ASFSession:
    """Build an authenticated ASF session from the configured credentials.

    Raises:
        RuntimeError: If no Earthdata credentials are configured.
    """
    credentials = earthdata_credentials()
    session = asf.ASFSession()

    if credentials is not None:
        if credentials.token is not None:
            return session.auth_with_token(credentials.token)
        username, password = credentials.username, credentials.password
        if username is not None and password is not None:
            return session.auth_with_creds(username, password)

    raise RuntimeError(
        f"No Earthdata credentials configured. Set {EDL_TOKEN_VAR} (or "
        f"{EDL_USERNAME_VAR} and its password) in your environment or .env "
        "file. Register at https://urs.earthdata.nasa.gov/"
    )


def search(config: ConfigSource, *, write_output: bool = True) -> Any:
    """Validate a search config, then run it against the ASF archive.

    Args:
        config: Path to a YAML config, a mapping, or a ``DictConfig``.
        write_output: Write the result table to the config's ``output`` path.

    Returns:
        The ``ASFSearchResults`` for the query.

    Raises:
        SearchConfigError: If the config does not describe a valid search.
    """
    loaded = SearchConfig.load(config)
    results = asf.search(**loaded.search_options())

    if write_output and loaded.output is not None:
        loaded.write_results(results, loaded.output)
    return results


def download(config: ConfigSource) -> list[Path]:
    """Search, then download every hit into the config's ``dest`` directory.

    Args:
        config: Path to a YAML config, a mapping, or a ``DictConfig``.

    Returns:
        The paths of the downloaded files.

    Raises:
        SearchConfigError: If the config is invalid or sets no ``dest``.
        RuntimeError: If no Earthdata credentials are configured.
    """
    loaded = SearchConfig.load(config)
    dest = loaded.dest
    if dest is None:
        raise SearchConfigError(
            ["'dest' is required to download; set it to a target directory"],
            loaded.source,
        )

    results = search(config)
    dest.mkdir(parents=True, exist_ok=True)
    results.download(str(dest), session=earthdata_session(), processes=loaded.processes)

    return [dest / _file_name(product) for product in results]


def _file_name(product: Any) -> str:
    """The archive file name of a hit, whether it is an ASFProduct or a mapping."""
    properties = getattr(product, "properties", product)
    return str(properties["fileName"])
```

- [ ] **Step 4: Export the new functions**

Update `src/radar_snap_lib/search/__init__.py`:

```python
from radar_snap_lib.search.aoi import AOIError, SearchBounds, aoi_to_wkt
from radar_snap_lib.search.SearchConfig import SearchConfig, SearchConfigError
from radar_snap_lib.search.runner import download, earthdata_session, search

__all__ = [
    "AOIError",
    "SearchBounds",
    "SearchConfig",
    "SearchConfigError",
    "aoi_to_wkt",
    "download",
    "earthdata_session",
    "search",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_search_runner.py -v`
Expected: PASS — 11 tests.

Then: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff format src/radar_snap_lib/search tests/test_search_runner.py && uv run ruff check src tests && uv run ty check src`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/radar_snap_lib/search tests/test_search_runner.py
git commit -m "feat: run ASF searches and downloads from a config

Searching needs no credentials; only download builds an authenticated
Earthdata session. Validation always runs before anything hits the network."
```

---

### Task 5: CLI subcommands, example config and README

`validate` must now handle both kinds of config. A `pipeline` key means a graph config; anything else is a search config. No flags anywhere — `dest`, `processes` and `output` live in the config file.

**Files:**
- Modify: `src/radar_snap_lib/snap_ops/cli.py`
- Create: `examples/search_s1_slc.yaml`
- Modify: `README.md`
- Test: `tests/test_cli.py` (create)

**Interfaces:**
- Consumes: `search`, `download` from Task 4; `SearchConfig`, `SearchConfigError` from Task 3.
- Produces: `radar-snap search CONFIG`, `radar-snap download CONFIG`, and `validate` routing by config kind.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
"""Tests for the radar-snap command line, with execution mocked out."""

from __future__ import annotations

import pytest

from radar_snap_lib.snap_ops import cli

SEARCH_YAML = (
    "aoi: POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))\n"
    "platform: SENTINEL-1\n"
    "flight_direction: ASCENDING\n"
)

PIPELINE_YAML = (
    "pipeline:\n"
    "  Read:\n"
    "    file: in.zip\n"
    "  Write:\n"
    "    file: out.tif\n"
    "    formatName: GeoTIFF\n"
)


class TestValidateRouting:
    def test_search_config_validates(self, tmp_path, capsys):
        path = tmp_path / "s.yaml"
        path.write_text(SEARCH_YAML)
        assert cli.main(["validate", str(path)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_pipeline_config_validates(self, tmp_path, capsys):
        path = tmp_path / "p.yaml"
        path.write_text(PIPELINE_YAML)
        assert cli.main(["validate", str(path)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_bad_search_config_fails(self, tmp_path, capsys):
        path = tmp_path / "s.yaml"
        path.write_text("platform: SENTINEL-1\nbogus: 1\n")
        assert cli.main(["validate", str(path)]) == 1
        assert "bogus" in capsys.readouterr().err


class TestSearchCommand:
    def test_calls_search_and_reports_the_count(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "s.yaml"
        path.write_text(SEARCH_YAML)
        seen = {}

        def fake_search(config, **kwargs):
            seen["config"] = config
            return [{"fileName": "a.zip"}, {"fileName": "b.zip"}]

        monkeypatch.setattr("radar_snap_lib.search.search", fake_search)
        assert cli.main(["search", str(path)]) == 0
        assert str(seen["config"]) == str(path)
        assert "2" in capsys.readouterr().out

    def test_config_error_returns_one(self, tmp_path, capsys):
        path = tmp_path / "s.yaml"
        path.write_text("platform: SENTINEL-1\nbogus: 1\n")
        assert cli.main(["search", str(path)]) == 1
        assert "bogus" in capsys.readouterr().err


class TestDownloadCommand:
    def test_calls_download_and_lists_paths(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "s.yaml"
        path.write_text(SEARCH_YAML + f"dest: {tmp_path / 'scenes'}\n")
        monkeypatch.setattr(
            "radar_snap_lib.search.download",
            lambda config: [tmp_path / "scenes" / "a.zip"],
        )
        assert cli.main(["download", str(path)]) == 0
        assert "a.zip" in capsys.readouterr().out


class TestNoFlags:
    @pytest.mark.parametrize("command", ["search", "download"])
    def test_flags_are_rejected(self, command, tmp_path):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([command, str(tmp_path / "s.yaml"), "--dest", "/data"])

    @pytest.mark.parametrize("command", ["search", "download"])
    def test_config_argument_is_required(self, command):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([command])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `argparse` errors with "invalid choice: 'search'".

- [ ] **Step 3: Add config-kind sniffing and the two commands**

In `src/radar_snap_lib/snap_ops/cli.py`, replace `_cmd_validate` and add the new handlers above `build_parser`:

```python
def _is_pipeline_config(path: Path) -> bool:
    """A ``pipeline`` key means a graph config; anything else is a search."""
    from omegaconf import DictConfig, OmegaConf  # noqa: PLC0415

    loaded = OmegaConf.load(path)
    return isinstance(loaded, DictConfig) and "pipeline" in loaded


def _cmd_validate(args: argparse.Namespace) -> int:
    if not Path(args.config).is_file():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2

    if _is_pipeline_config(args.config):
        from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, OpsConfig

        try:
            errors = OpsConfig.load(args.config).validate()
        except GraphConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        if errors:
            print(GraphConfigError(errors, str(args.config)), file=sys.stderr)
            return 1
    else:
        from radar_snap_lib.search.SearchConfig import SearchConfig, SearchConfigError

        try:
            errors = SearchConfig.load(args.config).validate()
        except SearchConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        if errors:
            print(SearchConfigError(errors, str(args.config)), file=sys.stderr)
            return 1

    print(f"{args.config}: OK")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    import radar_snap_lib.search as search_pkg  # noqa: PLC0415
    from radar_snap_lib.search.SearchConfig import (  # noqa: PLC0415
        SearchConfig,
        SearchConfigError,
    )

    try:
        results = search_pkg.search(args.config)
    except SearchConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    output = SearchConfig.load(args.config).output
    where = f", written to {output}" if output is not None else ""
    print(f"{args.config}: {len(results)} scene(s){where}")
    if output is None:
        for product in results:
            properties = getattr(product, "properties", product)
            print(f"  {properties.get('fileName', properties.get('sceneName', '?'))}")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    import radar_snap_lib.search as search_pkg  # noqa: PLC0415
    from radar_snap_lib.search.SearchConfig import SearchConfigError  # noqa: PLC0415

    try:
        paths = search_pkg.download(args.config)
    except SearchConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"{args.config}: {len(paths)} file(s)")
    for path in paths:
        print(f"  {path}")
    return 0
```

The `search_pkg.search` indirection (rather than `from ... import search`) is what makes `monkeypatch.setattr("radar_snap_lib.search.search", ...)` in the tests take effect.

- [ ] **Step 4: Register the subparsers**

In `build_parser`, after the `validate` block, add:

```python
    search_cmd = sub.add_parser(
        "search", help="run an ASF archive search from a config"
    )
    search_cmd.add_argument("config", type=Path)
    search_cmd.set_defaults(func=_cmd_search)

    download_cmd = sub.add_parser(
        "download", help="search, then download the hits into the config's dest"
    )
    download_cmd.add_argument("config", type=Path)
    download_cmd.set_defaults(func=_cmd_download)
```

Update the `validate` help text to `"check a config (graph or search) without running it"`, and the parser description to `"Search the ASF archive and run ESA SNAP process graphs, from YAML."`.

- [ ] **Step 5: Write the example config**

Create `examples/search_s1_slc.yaml`:

```yaml
# Sentinel-1 SLC scenes over an area of interest, ascending passes only.
#
#   radar-snap validate examples/search_s1_slc.yaml
#   radar-snap search   examples/search_s1_slc.yaml
#   radar-snap download examples/search_s1_slc.yaml
#
# Every setting lives here -- the commands take no flags. Downloading needs
# Earthdata credentials; see .env.example. Searching does not.

# AOI: a vector file (GeoPackage, Shapefile, GeoJSON -- all features are
# unioned and reprojected to WGS-84), four numbers [W, S, E, N], or a WKT
# string. Uncomment whichever you need.
aoi: aois/testgebiet.gpkg
# aoi: [10.0, 50.0, 11.0, 51.0]
# aoi: POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))

start: 2024-01-01
end: 2024-06-30

platform: SENTINEL-1
flight_direction: ASCENDING
processing_level: SLC
beam_mode: IW
polarization: VV+VH
max_results: 100

# Any ASFSearchOptions key works here, in ASF's own camelCase or snake_case:
# relative_orbit: 44
# season: [90, 270]

dest: /data/s1
processes: 4
output: results/testgebiet.geojson
```

- [ ] **Step 6: Update the README**

Insert this section into `README.md` immediately before the existing section that documents the pipeline/graph side:

````markdown
## Searching the ASF archive

Finding scenes works like running a graph: describe it in YAML, validate it
offline, then execute. There are no command line flags — every setting,
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

Searching needs no account. Downloading needs NASA Earthdata credentials —
set `EARTHDATA_TOKEN` in your environment or `.env` (see `.env.example`).
````

Then check the surrounding heading levels still nest correctly, and fix the
table of contents if the README has one.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS — 10 tests.

Then the full suite: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 8: Check the example config validates**

Run: `uv run radar-snap validate examples/search_s1_slc.yaml`
Expected: one error — `'aoi': AOI file not found: aois/testgebiet.gpkg` — which proves routing and AOI checking both work. Then temporarily swap the `aoi` line for the WKT variant, re-run, and confirm `OK`. Restore the file afterwards.

Also confirm the graph configs still validate:
Run: `uv run radar-snap validate examples/s1_grd_gamma0.yaml`
Expected: `OK`.

- [ ] **Step 9: Lint and type-check**

Run: `uv run ruff format src tests && uv run ruff check src tests && uv run ty check src`
Expected: no errors.

- [ ] **Step 10: Commit**

```bash
git add src/radar_snap_lib/snap_ops/cli.py tests/test_cli.py examples/search_s1_slc.yaml README.md
git commit -m "feat: radar-snap search and download subcommands

No flags -- dest, processes and output all live in the config. validate
routes by config kind: a 'pipeline' key means a graph, anything else is
a search."
```

---

## Verification

After all five tasks:

```bash
uv run pytest -q                  # full suite, no network
uv run ruff check src tests
uv run ruff format --check src tests
uv run ty check src
uv run radar-snap validate examples/s1_grd_gamma0.yaml
uv run radar-snap --help
```

An optional live check, run by hand only:

```bash
uv run python -c "
from radar_snap_lib.search import search
print(len(search({'aoi': [10, 50, 11, 51], 'platform': 'SENTINEL-1', 'max_results': 5})))
"
```
