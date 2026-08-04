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
        if not isinstance(resolved, dict):
            return {}
        return {str(key): value for key, value in resolved.items()}

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
