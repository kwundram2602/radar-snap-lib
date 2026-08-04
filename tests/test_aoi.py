"""Tests for turning AOI sources into ASF-accepted search WKT."""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely import wkt as shapely_wkt
from shapely.geometry import box

from radar_snap_lib.search.aoi import AOIError, SearchBounds, aoi_to_wkt


@pytest.fixture
def gpkg(tmp_path):
    """A two-feature GeoPackage in EPSG:4326."""
    path = tmp_path / "aoi.gpkg"
    frame = gpd.GeoDataFrame(
        {"name": ["west", "east"]},
        geometry=[box(10.0, 50.0, 11.0, 51.0), box(12.0, 50.0, 13.0, 51.0)],
        crs="EPSG:4326",
    )
    frame.to_file(path, driver="GPKG")
    return path


class TestVectorFiles:
    def test_features_are_unioned(self, gpkg):
        geometry = shapely_wkt.loads(aoi_to_wkt(gpkg))
        assert geometry.geom_type == "MultiPolygon"
        assert len(geometry.geoms) == 2

    def test_accepts_a_string_path(self, gpkg):
        assert aoi_to_wkt(str(gpkg)) == aoi_to_wkt(gpkg)

    def test_reprojects_to_wgs84(self, tmp_path):
        path = tmp_path / "utm.gpkg"
        frame = gpd.GeoDataFrame(
            geometry=[box(500000.0, 5600000.0, 510000.0, 5610000.0)],
            crs="EPSG:32632",
        )
        frame.to_file(path, driver="GPKG")
        bounds = shapely_wkt.loads(aoi_to_wkt(path)).bounds
        assert 8.0 < bounds[0] < 10.0
        assert 50.0 < bounds[1] < 51.0

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(AOIError, match="not found"):
            aoi_to_wkt(tmp_path / "nope.gpkg")

    def test_missing_crs_raises(self, tmp_path):
        path = tmp_path / "nocrs.geojson"
        gpd.GeoDataFrame(geometry=[box(10.0, 50.0, 11.0, 51.0)]).to_file(path)
        with pytest.raises(AOIError, match="no CRS"):
            aoi_to_wkt(path)

    def test_empty_file_raises(self, tmp_path):
        path = tmp_path / "empty.gpkg"
        gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:4326").to_file(
            path, driver="GPKG"
        )
        with pytest.raises(AOIError, match="no features"):
            aoi_to_wkt(path)


class TestOtherSources:
    def test_search_bounds(self):
        geometry = shapely_wkt.loads(aoi_to_wkt(SearchBounds(10.0, 50.0, 11.0, 51.0)))
        assert geometry.bounds == (10.0, 50.0, 11.0, 51.0)

    def test_four_number_sequence(self):
        assert aoi_to_wkt([10.0, 50.0, 11.0, 51.0]) == aoi_to_wkt(
            SearchBounds(10.0, 50.0, 11.0, 51.0)
        )

    def test_wrong_length_sequence_raises(self):
        with pytest.raises(AOIError, match="four numbers"):
            aoi_to_wkt([10.0, 50.0, 11.0])

    def test_wkt_string_passes_through(self):
        source = "POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))"
        assert shapely_wkt.loads(aoi_to_wkt(source)).bounds == (10.0, 50.0, 11.0, 51.0)

    def test_invalid_wkt_string_raises(self):
        with pytest.raises(AOIError):
            aoi_to_wkt("POLYGON((oops))")

    def test_unsupported_type_raises(self):
        with pytest.raises(AOIError, match="Unsupported AOI"):
            aoi_to_wkt(42)
