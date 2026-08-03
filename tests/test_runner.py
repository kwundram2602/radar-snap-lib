"""End-to-end execution through SNAP's ``GraphProcessor``.

These tests build a small product, run a real graph over it and check the
pixels, so they cover the one part of the pipeline that the JVM-free tests
cannot reach.
"""

from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.snap


@pytest.fixture(scope="module")
def snappy():
    from radar_snap_lib.config import ensure_esa_snappy

    ensure_esa_snappy()
    import esa_snappy

    return esa_snappy


@pytest.fixture(scope="module")
def source_product(snappy, tmp_path_factory):
    """A 120x120 single-band product with a known ramp."""
    path = tmp_path_factory.mktemp("snap") / "src.dim"
    width = height = 120

    product = snappy.Product("src", "BEAM-DIMAP", width, height)
    band = product.addBand("band_1", snappy.ProductData.TYPE_FLOAT32)
    ramp = np.arange(width * height, dtype=np.float32)
    band.setData(snappy.ProductData.createInstance(ramp))
    band.setModified(True)
    snappy.ProductIO.writeProduct(product, str(path), "BEAM-DIMAP")
    product.closeIO()
    return path


def _read_band(snappy, path, name):
    product = snappy.ProductIO.readProduct(str(path))
    width = product.getSceneRasterWidth()
    height = product.getSceneRasterHeight()
    pixels = np.zeros(width * height, dtype=np.float32)
    product.getBand(name).readPixels(0, 0, width, height, pixels)
    return pixels.reshape(height, width)


def test_runs_a_graph_and_writes_correct_pixels(snappy, source_product, tmp_path):
    """Subset then BandMaths, checked pixel for pixel.

    Also covers the nested-POJO parameter path: ``targetBandDescriptors`` has
    to reach SNAP as nested XML, which is why the runner emits graph XML rather
    than calling ``GPF.createProduct`` with a flat map.
    """
    from radar_snap_lib.snap_ops import run_graph

    output = tmp_path / "out.dim"
    config = {
        "pipeline": {
            "Read": {"file": str(source_product)},
            "Subset": {"region": "10,10,50,40"},
            "BandMaths": {
                "targetBandDescriptors": [
                    {"name": "doubled", "type": "float32", "expression": "band_1 * 2"}
                ]
            },
            "Write": {"file": str(output), "formatName": "BEAM-DIMAP"},
        }
    }

    run_graph(config, quiet=True)
    assert output.is_file()

    result = _read_band(snappy, output, "doubled")
    assert result.shape == (40, 50)

    expected = np.arange(120 * 120, dtype=np.float32).reshape(120, 120)
    expected = expected[10:50, 10:60] * 2
    assert np.array_equal(result, expected)


def test_dump_xml_writes_the_executed_graph(source_product, tmp_path):
    from radar_snap_lib.snap_ops import run_graph

    dumped = tmp_path / "graph.xml"
    config = {
        "pipeline": {
            "Read": {"file": str(source_product)},
            "Write": {"file": str(tmp_path / "copy.dim"), "formatName": "BEAM-DIMAP"},
        }
    }

    xml = run_graph(config, dump_xml=dumped, quiet=True)
    assert dumped.read_text(encoding="utf-8") == xml
    assert "<operator>Read</operator>" in xml
