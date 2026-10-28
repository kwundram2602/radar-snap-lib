"""Run a chain (or tree) of filters over a GeoTIFF, band by band.

Each band is read whole, every filter step runs on it in memory, and each step
named in ``outputs`` writes its result as one band of its own GeoTIFF, with the
input's georeferencing. Memory therefore scales with one band times the steps
still needed at a time, not with the whole raster.

scikit-image and rasterio are imported when filters run, so that
:class:`FilterStep` costs nothing to import for a config that only describes
them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["CANNY_NODATA", "FilterStep", "run_filters"]

#: Nodata of a Canny output, which is 0/1 everywhere else.
CANNY_NODATA = 255


@dataclass
class FilterStep:
    """One filter applied to the input raster or to another step's result."""

    id: str
    #: Key of :data:`radar_snap_lib.filters.ops.OPS`, e.g. ``"Canny"``.
    op: str
    #: The step it filters, ``None`` for the input raster.
    source: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    #: 1-based input bands, for a step on the input raster; ``None`` for all.
    bands: Sequence[int] | None = None


def _out_kind(
    step: FilterStep, by_id: Mapping[str, FilterStep], dtype: str, nodata: Any
) -> tuple[str, Any]:
    """(dtype, nodata) of a step's output."""
    if step.op == "Canny":
        return "uint8", CANNY_NODATA
    if step.source is not None:
        return _out_kind(by_id[step.source], by_id, dtype, nodata)
    if nodata is None and np.issubdtype(np.dtype(dtype), np.floating):
        return dtype, float("nan")
    return dtype, nodata


def run_filters(
    src: Path | str, steps: Sequence[FilterStep], outputs: Mapping[str, Path | str]
) -> list[Path]:
    """Filter ``src`` and write the results of the steps in ``outputs``.

    Args:
        src: A raster rasterio can read, usually a GeoTIFF.
        steps: In dependency order: a step's source comes before it.
        outputs: Step id -> GeoTIFF to write that step's result to.

    Returns:
        The files written.
    """
    import rasterio  # noqa: PLC0415

    from radar_snap_lib.filters.ops import OPS  # noqa: PLC0415

    by_id = {step.id: step for step in steps}
    unknown = [step.op for step in steps if step.op not in OPS]
    if unknown:
        raise ValueError(
            f"unknown filter op(s) {unknown!r}, expected one of {list(OPS)}"
        )

    root: dict[str, str] = {}
    for step in steps:
        root[step.id] = step.id if step.source is None else root[step.source]
    consumers = {step.id: 0 for step in steps}
    for step in steps:
        if step.source is not None:
            consumers[step.source] += 1

    with rasterio.open(src) as dataset:
        bands_of = {
            step.id: list(step.bands or range(1, dataset.count + 1))
            for step in steps
            if step.source is None
        }
        for step_id, bands in bands_of.items():
            bad = [b for b in bands if not 1 <= b <= dataset.count]
            if bad:
                raise ValueError(
                    f"Step {step_id!r}: band(s) {bad} not in {src} "
                    f"(bands 1..{dataset.count})"
                )

        written: dict[str, Path] = {}
        targets: dict[str, Any] = {}
        try:
            for step_id, file in outputs.items():
                bands = bands_of[root[step_id]]
                dtype, nodata = _out_kind(
                    by_id[step_id], by_id, dataset.dtypes[bands[0] - 1], dataset.nodata
                )
                profile = {
                    **dataset.profile,
                    "driver": "GTiff",
                    "count": len(bands),
                    "dtype": dtype,
                    "nodata": nodata,
                    "BIGTIFF": "IF_SAFER",
                }
                path = Path(file)
                path.parent.mkdir(parents=True, exist_ok=True)
                targets[step_id] = rasterio.open(path, "w", **profile)
                written[step_id] = path

            for band in sorted({b for bands in bands_of.values() for b in bands}):
                data = dataset.read(band)
                valid = (
                    np.isfinite(data)
                    if data.dtype.kind == "f"
                    else np.ones(data.shape, bool)
                )
                if dataset.nodata is not None and not np.isnan(dataset.nodata):
                    valid &= data != dataset.nodata
                _run_band(
                    band,
                    data,
                    valid,
                    steps,
                    root,
                    bands_of,
                    consumers,
                    targets,
                    dataset.descriptions[band - 1],
                )
        finally:
            for target in targets.values():
                target.close()
    return list(written.values())


def _run_band(
    band: int,
    data: np.ndarray,
    valid: np.ndarray,
    steps: Sequence[FilterStep],
    root: Mapping[str, str],
    bands_of: Mapping[str, list[int]],
    consumers: Mapping[str, int],
    targets: Mapping[str, Any],
    description: str | None,
) -> None:
    from radar_snap_lib.filters.ops import OPS  # noqa: PLC0415

    results: dict[str, np.ndarray] = {}
    left = dict(consumers)
    for step in steps:
        bands = bands_of[root[step.id]]
        if band not in bands:
            continue
        if step.source is None:
            source = data
        else:
            source = results[step.source]
            left[step.source] -= 1
            if not left[step.source]:
                del results[step.source]
        result = OPS[step.op](source, valid, **step.params)

        target = targets.get(step.id)
        if target is not None:
            out = result.astype(target.dtypes[0], copy=False)
            if target.nodata is not None:
                out = np.where(valid, out, target.nodata).astype(target.dtypes[0])
            index = bands.index(band) + 1
            target.write(out, index)
            if description:
                target.set_band_description(index, description)
        if left[step.id]:
            results[step.id] = result
