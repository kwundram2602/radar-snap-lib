"""Tests for the search YAML front end: aliasing, validation, option building."""

from __future__ import annotations

import pytest

from radar_snap_lib.search.SearchConfig import (
    ALIASES,
    RESERVED_KEYS,
    SearchConfig,
    SearchConfigError,
)

BASE = {
    "aoi": "POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))",
    "platform": "SENTINEL-1",
    "start": "2024-01-01",
    "end": "2024-06-30",
}


class TestAliases:
    def test_snake_case_maps_to_camel_case(self):
        assert ALIASES["flight_direction"] == "flightDirection"
        assert ALIASES["max_results"] == "maxResults"
        assert ALIASES["processing_level"] == "processingLevel"

    def test_no_alias_collisions(self):
        assert len(set(ALIASES.values())) == len(ALIASES)

    def test_camel_case_still_accepted(self):
        config = SearchConfig.load({**BASE, "flightDirection": "ASCENDING"})
        assert config.search_options()["flightDirection"] == "ASCENDING"

    def test_snake_case_is_translated(self):
        config = SearchConfig.load({**BASE, "flight_direction": "ASCENDING"})
        assert config.search_options()["flightDirection"] == "ASCENDING"

    def test_both_spellings_of_one_key_is_an_error(self):
        config = SearchConfig.load(
            {**BASE, "flight_direction": "ASCENDING", "flightDirection": "DESCENDING"}
        )
        errors = config.validate()
        assert any("flightDirection" in e and "twice" in e for e in errors)


class TestValidation:
    def test_a_good_config_has_no_errors(self):
        assert SearchConfig.load(BASE).validate() == []

    def test_unknown_key_is_reported_with_a_suggestion(self):
        errors = SearchConfig.load({**BASE, "flightdirection": "ASCENDING"}).validate()
        assert len(errors) == 1
        assert "flightdirection" in errors[0]
        assert "flight_direction" in errors[0]

    def test_bad_date_is_reported(self):
        errors = SearchConfig.load({**BASE, "start": "not-a-date"}).validate()
        assert any("start" in e for e in errors)

    def test_bad_int_is_reported(self):
        errors = SearchConfig.load({**BASE, "max_results": "many"}).validate()
        assert any("maxResults" in e or "max_results" in e for e in errors)

    def test_all_errors_are_collected(self):
        errors = SearchConfig.load(
            {**BASE, "bogus": 1, "alsoBogus": 2, "start": "nope"}
        ).validate()
        assert len(errors) == 3

    def test_missing_aoi_and_geometry_is_an_error(self):
        errors = SearchConfig.load({"platform": "SENTINEL-1"}).validate()
        assert any("aoi" in e for e in errors)

    def test_aoi_plus_intersects_with_is_an_error(self):
        errors = SearchConfig.load(
            {**BASE, "intersectsWith": "POLYGON((0 0, 1 0, 1 1, 0 0))"}
        ).validate()
        assert any("intersectsWith" in e for e in errors)

    def test_bbox_alone_satisfies_the_geometry_requirement(self):
        assert (
            SearchConfig.load(
                {"platform": "SENTINEL-1", "bbox": [10, 50, 11, 51]}
            ).validate()
            == []
        )

    def test_missing_aoi_file_is_reported(self, tmp_path):
        errors = SearchConfig.load(
            {**BASE, "aoi": str(tmp_path / "no.gpkg")}
        ).validate()
        assert any("not found" in e for e in errors)

    def test_unknown_output_suffix_is_an_error(self):
        errors = SearchConfig.load({**BASE, "output": "results.txt"}).validate()
        assert any("output" in e and ".geojson" in e for e in errors)

    def test_root_must_be_a_mapping(self):
        with pytest.raises(SearchConfigError, match="mapping"):
            SearchConfig.load([1, 2, 3])


class TestSearchOptions:
    def test_reserved_keys_are_not_forwarded(self):
        options = SearchConfig.load(
            {**BASE, "dest": "/data", "processes": 4, "output": "r.geojson"}
        ).search_options()
        assert not RESERVED_KEYS & set(options)

    def test_aoi_becomes_intersects_with(self):
        options = SearchConfig.load(BASE).search_options()
        assert "POLYGON" in options["intersectsWith"]
        assert "aoi" not in options

    def test_invalid_config_raises_before_building_options(self):
        with pytest.raises(SearchConfigError) as excinfo:
            SearchConfig.load({**BASE, "bogus": 1}).search_options()
        assert excinfo.value.errors

    def test_accessors_expose_reserved_keys(self, tmp_path):
        config = SearchConfig.load(
            {**BASE, "dest": str(tmp_path), "processes": 4, "output": "r.geojson"}
        )
        assert config.dest == tmp_path
        assert config.processes == 4
        assert config.output.name == "r.geojson"

    def test_processes_defaults_to_one(self):
        assert SearchConfig.load(BASE).processes == 1

    def test_dest_is_none_when_unset(self):
        assert SearchConfig.load(BASE).dest is None


class TestYamlLoading:
    def test_loads_from_a_file(self, tmp_path):
        path = tmp_path / "s.yaml"
        path.write_text(
            "aoi: POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))\n"
            "platform: SENTINEL-1\n"
            "flight_direction: ASCENDING\n"
        )
        config = SearchConfig.load(path)
        assert config.source == str(path)
        assert config.validate() == []

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SearchConfig.load(tmp_path / "nope.yaml")

    def test_error_message_names_the_source(self, tmp_path):
        path = tmp_path / "s.yaml"
        path.write_text("platform: SENTINEL-1\nbogus: 1\n")
        with pytest.raises(SearchConfigError, match=str(path)):
            SearchConfig.load(path).search_options()
