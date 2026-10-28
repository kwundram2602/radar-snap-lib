"""Filter nodes: scikit-image filters at the end of a pipeline.

A filter node (``Canny``, ``Erosion``, ``Dilation``, ``Opening``, ``Closing``) is
written in a config like any SNAP operator and validated against an
:class:`OperatorSpec` like one, but it runs in Python on the GeoTIFF the SNAP
part of the pipeline leaves behind (see
:mod:`radar_snap_lib.snap_ops.filter_tail`). Filters come last: they may read
from each other, but no SNAP node may read from them.

Like :mod:`radar_snap_lib.snap_ops.external`, this module stays free of the
heavy imports; scikit-image is only loaded when a filter runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from radar_snap_lib.snap_ops.external import _param
from radar_snap_lib.snap_ops.registry import OperatorSpec, Registry, SourceSpec

if TYPE_CHECKING:
    from radar_snap_lib.snap_ops.OpsConfig import ParsedNode

__all__ = ["FILTER_CLS_PREFIX", "FILTER_OPS", "FilterOp", "is_filter", "with_filters"]

#: ``OperatorSpec.cls`` prefix of filter ops; also what visualisation keys on.
FILTER_CLS_PREFIX = "radar_snap_lib.filters."

#: Kept in step with :data:`radar_snap_lib.filters.ops.FOOTPRINTS`, which
#: cannot be imported here without loading scikit-image.
_FOOTPRINTS = ("disk", "square", "diamond")


@dataclass(frozen=True)
class FilterOp:
    """A filter node type."""

    spec: OperatorSpec
    #: Checks beyond the spec's per-parameter ones. Returns error messages.
    check: Callable[[ParsedNode], list[str]]


_BANDS = _param(
    "bands",
    "int[]",
    "tuple[int, ...] | None",
    description=(
        "1-based bands of the input GeoTIFF to filter (default: all); only on "
        "a filter that reads the GeoTIFF, not on one after another filter"
    ),
)


def _spec(alias: str, description: str, *params: Any) -> OperatorSpec:
    return OperatorSpec(
        alias=alias,
        cls=f"{FILTER_CLS_PREFIX}{alias}",
        description=f"{description} (scikit-image, runs in Python after SNAP)",
        sources=(SourceSpec(name="sourceProduct"),),
        params={p.name: p for p in (*params, _BANDS)},
    )


def _check_bands(node: ParsedNode) -> list[str]:
    bands = node.params.get("bands")
    if bands is None:
        return []
    items = bands if isinstance(bands, (list, tuple)) else [bands]
    if not items or not all(isinstance(b, int) and b >= 1 for b in items):
        return [
            f"Node {node.id!r}: bands must be a list of band numbers >= 1, "
            f"got {bands!r}"
        ]
    return []


def _check_canny(node: ParsedNode) -> list[str]:
    errors = _check_bands(node)
    params = node.params
    sigma = params.get("sigma", 1.0)
    if isinstance(sigma, (int, float)) and sigma <= 0:
        errors.append(f"Node {node.id!r}: sigma must be > 0, got {sigma}")
    low, high = params.get("low_threshold"), params.get("high_threshold")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low > high:
        errors.append(
            f"Node {node.id!r}: low_threshold ({low}) is above high_threshold ({high})"
        )
    if params.get("use_quantiles"):
        for name, value in (("low_threshold", low), ("high_threshold", high)):
            if isinstance(value, (int, float)) and not 0 <= value <= 1:
                errors.append(
                    f"Node {node.id!r}: with use_quantiles, {name} must be "
                    f"between 0 and 1, got {value}"
                )
    return errors


def _check_morphology(node: ParsedNode) -> list[str]:
    errors = _check_bands(node)
    radius = node.params.get("radius", 1)
    if not isinstance(radius, int) or isinstance(radius, bool) or radius < 1:
        errors.append(
            f"Node {node.id!r}: radius must be an integer >= 1, got {radius!r}"
        )
    return errors


CANNY = FilterOp(
    spec=_spec(
        "Canny",
        "Canny edge detection; writes 0/1 (uint8, nodata 255)",
        _param("sigma", "double", "float", 1.0, description="Gaussian smoothing sigma"),
        _param(
            "low_threshold",
            "double",
            "float | None",
            description="hysteresis low threshold on the gradient (default 0.1)",
        ),
        _param(
            "high_threshold",
            "double",
            "float | None",
            description="hysteresis high threshold on the gradient (default 0.2)",
        ),
        _param(
            "use_quantiles",
            "boolean",
            "bool",
            False,
            description="read the thresholds as quantiles of the gradient (0..1)",
        ),
    ),
    check=_check_canny,
)


def _morphology(alias: str, description: str) -> FilterOp:
    return FilterOp(
        spec=_spec(
            alias,
            description,
            _param(
                "footprint",
                "String",
                "str",
                "disk",
                value_set=_FOOTPRINTS,
                description="shape of the structuring element",
            ),
            _param(
                "radius",
                "int",
                "int",
                1,
                description="pixels from the footprint's centre to its edge",
            ),
        ),
        check=_check_morphology,
    )


FILTER_OPS: dict[str, FilterOp] = {
    op.spec.alias: op
    for op in (
        CANNY,
        _morphology("Erosion", "Grey-value (or binary) erosion, a minimum filter"),
        _morphology("Dilation", "Grey-value (or binary) dilation, a maximum filter"),
        _morphology("Opening", "Erosion then dilation: removes small bright details"),
        _morphology("Closing", "Dilation then erosion: fills small dark gaps"),
    )
}


def is_filter(op: str) -> bool:
    return op in FILTER_OPS


def with_filters(registry: Registry) -> Registry:
    """``registry`` plus the filter ops' specs, for validation and drawing."""
    return {**registry, **{alias: op.spec for alias, op in FILTER_OPS.items()}}
