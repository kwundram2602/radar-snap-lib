"""Small OmegaConf helpers shared by ``SearchConfig`` and ``OpsConfig``."""

from __future__ import annotations

import difflib
from typing import Any

from omegaconf import DictConfig, ListConfig, OmegaConf

__all__ = ["_plain", "_register_resolvers", "_suggest"]


def _plain(value: Any) -> Any:
    """Convert OmegaConf containers to plain Python objects."""
    if isinstance(value, (DictConfig, ListConfig)):
        return OmegaConf.to_object(value)
    return value


def _resolve_aoi_wkt(source: Any) -> str:
    """Resolver body for ``${aoi_wkt:...}``, lazily importing the AOI parser."""
    from radar_snap_lib.search.aoi import aoi_to_wkt  # noqa: PLC0415

    return aoi_to_wkt(_plain(source))


def _register_resolvers() -> None:
    """Register the ``aoi_wkt`` interpolation resolver, once per process.

    Lets a config turn any AOI source (vector file, WKT, or a
    ``[lon_min, lat_min, lon_max, lat_max]`` list) into WKT inline, e.g.
    ``wktAoi: ${aoi_wkt:${vars.aoi}}``.
    """
    if not OmegaConf.has_resolver("aoi_wkt"):
        OmegaConf.register_new_resolver("aoi_wkt", _resolve_aoi_wkt)


def _suggest(name: str, candidates: Any) -> str:
    matches = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.6)
    return f" Did you mean: {', '.join(matches)}?" if matches else ""
