"""DEM (Digital Elevation Model) creation from ALOS data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject


@dataclass(frozen=True)
class DemConfig:
    """Parameters controlling DEM output."""

    target_crs: str = "EPSG:4326"
    resolution_degrees: float = 0.000277778  # ~30 m at equator (1 arc-second)
    nodata: float = -9999.0
    resampling: Resampling = Resampling.bilinear


AlosAffine = rasterio.transform.Affine


def load_alos_dem_tile(
    path: Path,
) -> tuple[npt.NDArray[np.float32], AlosAffine, CRS]:
    """Load a single ALOS World 3D (AW3D30) elevation tile.

    ALOS AW3D30 tiles are distributed as GeoTIFF with Int16 values in metres.

    Args:
        path: Path to the AW3D30 tile GeoTIFF.

    Returns:
        Tuple of (elevation_array, affine_transform, crs).

    Raises:
        FileNotFoundError: If the tile file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"ALOS DEM tile not found: {path}")

    with rasterio.open(path) as src:
        elevation = src.read(1).astype(np.float32)
        transform = src.transform
        crs = src.crs

    return elevation, transform, crs


def merge_dem_tiles(
    tile_paths: list[Path],
) -> tuple[npt.NDArray[np.float32], AlosAffine, CRS]:
    """Merge multiple ALOS DEM tiles into a single array.

    All tiles must share the same CRS and pixel spacing. The output extent is
    the union of all tile extents.

    Args:
        tile_paths: Ordered list of tile paths to merge.

    Returns:
        Tuple of (merged_elevation, affine_transform, crs).

    Raises:
        ValueError: If ``tile_paths`` is empty.
    """
    if not tile_paths:
        raise ValueError("tile_paths must not be empty")

    from rasterio.merge import merge

    datasets = [rasterio.open(p) for p in tile_paths]
    try:
        merged_array, merged_transform = merge(datasets)
    finally:
        for ds in datasets:
            ds.close()

    crs = datasets[0].crs
    return merged_array[0].astype(np.float32), merged_transform, crs


def create_dem_from_alos(
    tile_paths: list[Path],
    output_path: Path,
    *,
    config: DemConfig | None = None,
) -> Path:
    """Build a reprojected, merged DEM GeoTIFF from ALOS AW3D30 tiles.

    Merges the supplied tiles, reprojects to the target CRS, and writes the
    result to ``output_path``.

    Args:
        tile_paths: One or more paths to ALOS AW3D30 GeoTIFF tiles.
        output_path: Destination path for the output DEM GeoTIFF.
        config: DEM creation parameters; uses defaults if ``None``.

    Returns:
        Absolute path to the written output file.

    Raises:
        ValueError: If ``tile_paths`` is empty.
        FileNotFoundError: If any tile path does not exist.
    """
    if not tile_paths:
        raise ValueError("tile_paths must not be empty")

    missing = [p for p in tile_paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Missing ALOS tiles: {missing}")

    cfg = config or DemConfig()
    target_crs = CRS.from_user_input(cfg.target_crs)

    elevation, src_transform, src_crs = merge_dem_tiles(tile_paths)

    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs,
        target_crs,
        elevation.shape[1],
        elevation.shape[0],
        *rasterio.transform.array_bounds(
            elevation.shape[0], elevation.shape[1], src_transform
        ),
    )

    reprojected = np.full((dst_height, dst_width), cfg.nodata, dtype=np.float32)
    reproject(
        source=elevation,
        destination=reprojected,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=target_crs,
        resampling=cfg.resampling,
        src_nodata=cfg.nodata,
        dst_nodata=cfg.nodata,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=dst_height,
        width=dst_width,
        count=1,
        dtype=np.float32,
        crs=target_crs,
        transform=dst_transform,
        nodata=cfg.nodata,
        compress="lzw",
    ) as dst:
        dst.write(reprojected, 1)

    return output_path.resolve()
