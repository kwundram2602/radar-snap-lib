"""scikit-image filters on GeoTIFFs, outside SNAP.

Usable on its own or as the filter nodes (``Canny``, ``Erosion``, ...) at the end
of a pipeline config::

    from radar_snap_lib.filters import FilterStep, run_filters

    run_filters(
        "gamma0.tif",
        [
            FilterStep("edges", "Canny", params={"sigma": 2.0}, bands=[1]),
            FilterStep("closed", "Closing", source="edges", params={"radius": 2}),
        ],
        outputs={"closed": "edges_closed.tif"},
    )
"""

from typing import Any

from radar_snap_lib.filters.raster import CANNY_NODATA, FilterStep, run_filters

#: Loaded on first access: importing scikit-image takes over a second, and
#: ``snap_ops`` imports this package for every config.
_FROM_OPS = frozenset(
    {
        "FOOTPRINTS",
        "OPS",
        "canny",
        "closing",
        "dilation",
        "erosion",
        "footprint",
        "opening",
    }
)


def __getattr__(name: str) -> Any:
    if name in _FROM_OPS:
        from radar_snap_lib.filters import ops  # noqa: PLC0415

        return getattr(ops, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CANNY_NODATA",
    "FOOTPRINTS",
    "OPS",
    "FilterStep",
    "canny",
    "closing",
    "dilation",
    "erosion",
    "footprint",
    "opening",
    "run_filters",
]
