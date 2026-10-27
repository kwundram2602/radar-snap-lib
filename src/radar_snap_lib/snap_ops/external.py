"""Pipeline nodes that run in Python instead of SNAP.

SNAP's ``GraphProcessor`` only runs its own operators. An external node -- for
now ``DeepDespeckle`` -- is written in a config like any other node, validated
against an :class:`OperatorSpec` like any other node, and drawn like any other
node, but at run time the pipeline is split around it (see
:mod:`radar_snap_lib.snap_ops.segments`): SNAP writes the node's source as
BEAM-DIMAP, the node turns that into a new DIMAP product, and the next graph
reads it back.

This module stays free of torch and of the JVM: an op's ``run`` imports its
heavy dependencies only when it is called.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from radar_snap_lib.snap_ops.registry import (
    OperatorSpec,
    ParamSpec,
    Registry,
    SourceSpec,
)

if TYPE_CHECKING:
    from radar_snap_lib.snap_ops.OpsConfig import ParsedNode

__all__ = ["EXTERNAL_OPS", "ExternalOp", "is_external", "with_external"]

#: ``OperatorSpec.cls`` prefix of external ops; also what visualisation keys on.
EXTERNAL_CLS_PREFIX = "radar_snap_lib."


@dataclass(frozen=True)
class ExternalOp:
    """A node type that runs in Python between two SNAP graphs."""

    spec: OperatorSpec
    #: Importable module the op needs at run time; checked before any graph runs.
    requires: str
    #: Hint shown when ``requires`` is missing.
    install_hint: str
    #: Checks beyond the spec's per-parameter ones, e.g. parameters that only
    #: apply to one method. Returns error messages.
    check: Callable[[ParsedNode], list[str]]
    #: ``run(src_dim, dst_dim, params)`` with the spec's defaults filled in.
    run: Callable[[Path, Path, dict[str, Any]], None]

    def params(self, node: ParsedNode) -> dict[str, Any]:
        """The node's parameters over the spec's defaults."""
        values = {
            name: param.default
            for name, param in self.spec.params.items()
            if param.default is not None
        }
        values.update(node.params)
        return values


def _param(
    name: str,
    type_name: str,
    python_type: str,
    default: Any = None,
    *,
    value_set: tuple[str, ...] = (),
    required: bool = False,
    description: str,
) -> ParamSpec:
    return ParamSpec(
        name=name,
        type=type_name,
        python_type=python_type,
        default=default,
        value_set=value_set,
        not_null=required,
        description=description,
    )


# --------------------------------------------------------------------------- #
# DeepDespeckle
# --------------------------------------------------------------------------- #

#: Parameters that only mean something to MERLIN.
_MERLIN_ONLY = ("model", "symetrise")


def _check_deep_despeckle(node: ParsedNode) -> list[str]:
    errors: list[str] = []
    params = node.params
    if params.get("method") == "sar2sar":
        given = [key for key in _MERLIN_ONLY if key in params]
        if given:
            errors.append(f"Node {node.id!r}: {given!r} only apply to method 'merlin'")

    patch = params.get("patch_size", 256)
    stride = params.get("stride_size", 254)
    if isinstance(patch, int) and (patch < 32 or patch & (patch - 1)):
        errors.append(
            f"Node {node.id!r}: patch_size must be a power of two >= 32, got {patch}"
        )
    if isinstance(patch, int) and isinstance(stride, int) and not 0 < stride < patch:
        errors.append(
            f"Node {node.id!r}: stride_size must be between 0 and patch_size "
            f"({patch}), got {stride}"
        )
    block = params.get("block_size", 4096)
    if isinstance(block, int) and isinstance(patch, int) and block < patch:
        errors.append(
            f"Node {node.id!r}: block_size must be at least patch_size ({patch}), "
            f"got {block}"
        )
    return errors


def _run_deep_despeckle(src: Path, dst: Path, params: dict[str, Any]) -> None:
    from radar_snap_lib.despeckle.deep import despeckle_dimap  # noqa: PLC0415

    bands = params.get("bands")
    despeckle_dimap(
        src,
        dst,
        method=params["method"],
        model=params["model"],
        symetrise=params["symetrise"],
        bands=list(bands) if bands else None,
        patch_size=params["patch_size"],
        stride_size=params["stride_size"],
        block_size=params["block_size"],
    )


DEEP_DESPECKLE = ExternalOp(
    spec=OperatorSpec(
        alias="DeepDespeckle",
        cls="radar_snap_lib.despeckle.DeepDespeckle",
        description=(
            "Deep-learning despeckling with deepdespeckling (runs in Python, "
            "between two SNAP graphs): MERLIN on complex SLC i/q bands, "
            "SAR2SAR on detected amplitude."
        ),
        sources=(SourceSpec(name="sourceProduct"),),
        params={
            p.name: p
            for p in (
                _param(
                    "method",
                    "String",
                    "str",
                    value_set=("merlin", "sar2sar"),
                    required=True,
                    description="merlin (complex SLC) or sar2sar (GRD amplitude)",
                ),
                _param(
                    "model",
                    "String",
                    "str",
                    "Sentinel-TOPS",
                    value_set=("Sentinel-TOPS", "spotlight", "stripmap"),
                    description="MERLIN weights: Sentinel-1 TOPS or TerraSAR-X modes",
                ),
                _param(
                    "symetrise",
                    "Boolean",
                    "bool",
                    True,
                    description="MERLIN: symmetrise the real/imaginary spectra",
                ),
                _param(
                    "bands",
                    "String[]",
                    "tuple[str, ...] | None",
                    description="bands to despeckle (default: all that qualify)",
                ),
                _param(
                    "patch_size",
                    "int",
                    "int",
                    256,
                    description="model input size, a power of two",
                ),
                _param(
                    "stride_size",
                    "int",
                    "int",
                    254,
                    description="step between patches, below patch_size",
                ),
                _param(
                    "block_size",
                    "int",
                    "int",
                    4096,
                    description="pixels per side read into memory at a time",
                ),
            )
        },
    ),
    requires="deepdespeckling",
    install_hint=(
        "DeepDespeckle needs deepdespeckling and torch: uv sync --extra despeckle "
        "(CPU) or --extra despeckle-cu128 (NVIDIA GPU)"
    ),
    check=_check_deep_despeckle,
    run=_run_deep_despeckle,
)

EXTERNAL_OPS: dict[str, ExternalOp] = {DEEP_DESPECKLE.spec.alias: DEEP_DESPECKLE}


def is_external(op: str) -> bool:
    return op in EXTERNAL_OPS


def with_external(registry: Registry) -> Registry:
    """``registry`` plus the external ops' specs, for validation and drawing."""
    return {**registry, **{alias: op.spec for alias, op in EXTERNAL_OPS.items()}}
