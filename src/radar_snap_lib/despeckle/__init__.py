"""Despeckling outside SNAP, on BEAM-DIMAP products.

Usable on its own or as the ``DeepDespeckle`` node of a pipeline config::

    from radar_snap_lib.despeckle import despeckle_dimap

    despeckle_dimap("deburst.dim", "despeckled.dim", method="merlin")
"""

from radar_snap_lib.despeckle.deep import MERLIN_MODELS, METHODS, despeckle_dimap
from radar_snap_lib.despeckle.dimap import DimapProduct, copy_product

__all__ = [
    "MERLIN_MODELS",
    "METHODS",
    "DimapProduct",
    "copy_product",
    "despeckle_dimap",
]
