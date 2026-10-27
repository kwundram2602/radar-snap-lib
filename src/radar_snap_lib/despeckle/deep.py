"""Deep-learning despeckling of BEAM-DIMAP products with deepdespeckling.

Two methods (see https://github.com/kwundram2602/deepdespeckling):

``merlin``
    Complex SLC data. Every ``i_<x>``/``q_<x>`` band pair is replaced by one
    detected band ``Amplitude_<x>``, and the virtual ``Intensity_<x>`` is
    redefined as its square -- the layout of a GRD product. The phase is gone
    afterwards, so the product is marked ``sample_type = DETECTED``.
``sar2sar``
    Detected amplitude (GRD). Every ``Amplitude_*`` band is replaced by its
    despeckled version; an intensity band without amplitude is despeckled as
    its square root and squared back.

The image is processed block by block from memory maps, so a full scene does
not need to fit in memory. Importing this module does not import torch;
deepdespeckling is only loaded when a product is despeckled.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from radar_snap_lib.despeckle.dimap import Band, DimapProduct, copy_product

__all__ = ["MERLIN_MODELS", "METHODS", "despeckle_dimap"]

log = logging.getLogger(__name__)

METHODS = ("merlin", "sar2sar")
MERLIN_MODELS = ("Sentinel-TOPS", "spotlight", "stripmap")

INSTALL_HINT = (
    "Deep-learning despeckling needs deepdespeckling and torch: "
    "uv sync --extra despeckle (CPU) or --extra despeckle-cu128 (NVIDIA GPU)"
)


class _IQ:
    """Read-only ``[height, width, 2]`` view of an i/q band pair."""

    def __init__(self, i: np.ndarray, q: np.ndarray) -> None:
        self.i, self.q = i, q
        self.shape = (*i.shape, 2)

    def __getitem__(self, key: Any) -> np.ndarray:
        return np.stack([self.i[key], self.q[key]], axis=-1).astype(np.float32)


class _Mapped:
    """An array-like applying ``read`` to every block read and ``write`` to every
    block written, e.g. intensity <-> amplitude."""

    def __init__(self, array: np.ndarray, read: Any = None, write: Any = None) -> None:
        self.array, self.read, self.write = array, read, write
        self.shape = array.shape

    def __getitem__(self, key: Any) -> np.ndarray:
        return self.read(self.array[key])

    def __setitem__(self, key: Any, value: np.ndarray) -> None:
        self.array[key] = self.write(value)


def _denoiser(method: str, model: str, symetrise: bool) -> Any:
    try:
        if method == "merlin":
            from deepdespeckling.merlin.merlin_denoiser import (  # noqa: PLC0415
                MerlinDenoiser,
            )

            return MerlinDenoiser(model_name=model, symetrise=symetrise)
        from deepdespeckling.sar2sar.sar2sar_denoiser import (  # noqa: PLC0415
            Sar2SarDenoiser,
        )

        return Sar2SarDenoiser()
    except ImportError as exc:
        raise RuntimeError(f"{INSTALL_HINT}\n({exc})") from exc


def _iq_pairs(product: DimapProduct, bands: list[str] | None) -> list[str]:
    """Suffixes ``x`` of the ``i_x``/``q_x`` pairs to despeckle."""
    names = {b.name for b in product.bands if not b.virtual}
    suffixes = sorted(
        n.removeprefix("i_")
        for n in names
        if n.startswith("i_") and f"q_{n[2:]}" in names
    )
    if bands:
        wanted = {b.removeprefix("i_").removeprefix("q_") for b in bands}
        unknown = wanted - set(suffixes)
        if unknown:
            raise ValueError(
                f"{product.path.name}: no i/q band pair for {sorted(unknown)} "
                f"(pairs: {suffixes})"
            )
        suffixes = [s for s in suffixes if s in wanted]
    if not suffixes:
        raise ValueError(
            f"{product.path.name}: method 'merlin' needs complex i_*/q_* bands, "
            f"found {sorted(names)}"
        )
    return suffixes


def _detected_bands(product: DimapProduct, bands: list[str] | None) -> list[Band]:
    """The amplitude (or, failing that, intensity) bands to despeckle."""
    real = [b for b in product.bands if not b.virtual]
    if bands:
        chosen = [product.band(name) for name in bands]
    else:
        chosen = [b for b in real if b.unit == "amplitude"] or [
            b for b in real if b.unit == "intensity"
        ]
    if not chosen:
        raise ValueError(
            f"{product.path.name}: method 'sar2sar' needs amplitude or intensity "
            f"bands, found {[b.name for b in product.bands]}"
        )
    for band in chosen:
        if band.virtual or band.unit not in ("amplitude", "intensity"):
            raise ValueError(
                f"{product.path.name}: band {band.name!r} is not a stored amplitude "
                f"or intensity band (unit {band.unit!r})"
            )
    return chosen


def despeckle_dimap(
    src: Path | str,
    dst: Path | str,
    *,
    method: str,
    model: str = "Sentinel-TOPS",
    symetrise: bool = True,
    bands: list[str] | None = None,
    patch_size: int = 256,
    stride_size: int = 254,
    block_size: int = 4096,
) -> Path:
    """Despeckle a BEAM-DIMAP product into a new one.

    Args:
        src: Input ``.dim``.
        dst: Output ``.dim``; replaced if it exists. All bands that are not
            despeckled, and all metadata, are copied over.
        method: ``merlin`` (complex i/q bands) or ``sar2sar`` (amplitude).
        model: MERLIN weights: ``Sentinel-TOPS`` (Sentinel-1 IW/EW SLC),
            ``spotlight`` or ``stripmap`` (TerraSAR-X).
        symetrise: MERLIN only -- symmetrise the spectrum of the real and
            imaginary parts first, as the model assumes.
        bands: Restrict to these bands (``i_x``/``q_x`` or the suffix ``x`` for
            merlin, band names for sar2sar). Default: all that qualify.
        patch_size: Model input size, a power of two.
        stride_size: Step between patches, below ``patch_size``.
        block_size: Pixels per block read into memory, per side.

    Returns:
        ``dst`` as a :class:`~pathlib.Path`.
    """
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}, got {method!r}")
    source = DimapProduct(src)
    denoiser = _denoiser(method, model, symetrise)
    tiling = {
        "block_size": block_size,
        "patch_size": patch_size,
        "stride_size": stride_size,
    }

    if method == "merlin":
        suffixes = _iq_pairs(source, bands)
        skip = tuple(n for s in suffixes for n in (f"i_{s}", f"q_{s}"))
        product = copy_product(src, dst, skip_bands=skip)
        names = {b.name for b in product.bands}
        for suffix in suffixes:
            amplitude = f"Amplitude_{suffix}"
            log.info(
                "MERLIN (%s): %s -> %s", model, f"i_{suffix}/q_{suffix}", amplitude
            )
            iq = _IQ(source.read_band(f"i_{suffix}"), source.read_band(f"q_{suffix}"))
            out = product.create_band_file(amplitude, "amplitude")
            denoiser.denoise_image_tiled(iq, out, **tiling)
            out.flush()
            product.retarget_band(f"i_{suffix}", amplitude, "amplitude")
            product.remove_band(f"q_{suffix}")
            if f"Intensity_{suffix}" in names:
                product.set_expression(
                    f"Intensity_{suffix}", f"{amplitude} * {amplitude}"
                )
        product.set_metadata("sample_type", "DETECTED")
    else:
        chosen = _detected_bands(source, bands)
        product = copy_product(src, dst, skip_bands=tuple(b.name for b in chosen))
        for band in chosen:
            log.info("SAR2SAR: %s", band.name)
            data = source.read_band(band.name)
            out = product.create_band_file(band.name, band.unit or "amplitude")
            if band.unit == "intensity":
                image = _Mapped(data, read=lambda b: np.sqrt(b, dtype=np.float32))
                target = _Mapped(out, write=np.square)
            else:
                image, target = data, out
            denoiser.denoise_image_tiled(image, target, **tiling)
            out.flush()
            product.retarget_band(band.name, band.name, band.unit or "amplitude")

    product.save()
    return product.path
