"""Tests for the operator registry and its Java -> Python conversion.

The conversion cases below are not hypothetical: every raw value asserted here
was observed in the defaults SNAP actually reports.  Getting them wrong is what
made the previous generator emit code that would not run.
"""

from __future__ import annotations

import json
import math

import pytest

from radar_snap_lib.snap_ops.registry import (
    REGISTRY_PATH,
    java_type_name,
    load_registry,
    parse_default,
    python_type_name,
    registry_from_json,
    registry_to_json,
)


def _reject_constant(name: str) -> float:
    raise AssertionError(f"non-standard JSON constant in operators.json: {name}")


class TestParseDefault:
    @pytest.mark.parametrize(
        ("raw", "type_name", "expected"),
        [
            ("true", "boolean", True),
            ("false", "boolean", False),
            # SNAP really does use 'off' for a boolean default.
            ("off", "boolean", False),
            ("on", "boolean", True),
            # ...and '1' for a boxed Boolean.
            ("1", "Boolean", True),
            ("0", "Boolean", False),
        ],
    )
    def test_booleans(self, raw, type_name, expected):
        assert parse_default(raw, type_name) is expected

    @pytest.mark.parametrize(
        ("raw", "type_name", "expected"),
        [
            ("0", "double", 0.0),
            ("350.0", "double", 350.0),
            # Java float literals carry an f/F suffix.
            ("-999.0f", "float", -999.0),
            ("1.0F", "float", 1.0),
            ("0.05", "float", 0.05),
        ],
    )
    def test_floats(self, raw, type_name, expected):
        assert parse_default(raw, type_name) == pytest.approx(expected)

    def test_ints(self):
        assert parse_default("10", "int") == 10
        assert parse_default("3", "Integer") == 3

    def test_string_none_stays_a_string(self):
        """'None' is a genuine String default and must not become ``None``.

        Several operators default a String parameter to the four-character
        text 'None'.  Collapsing it to Python's ``None`` silently changes the
        meaning of the generated code.
        """
        assert parse_default("None", "String") == "None"

    def test_string_is_verbatim(self):
        assert parse_default("SRTM 3Sec", "String") == "SRTM 3Sec"
        assert parse_default("WGS84(DD)", "String") == "WGS84(DD)"

    def test_sequences(self):
        assert parse_default("All", "String[]") == ("All",)
        assert parse_default("VV,VH", "String[]") == ("VV", "VH")
        assert parse_default("90,95", "int[]") == (90, 95)

    def test_missing_default(self):
        assert parse_default(None, "String") is None
        assert parse_default("", "String") is None
        assert parse_default("   ", "double") is None

    def test_unparseable_default_warns_and_keeps_raw(self):
        with pytest.warns(UserWarning, match="keeping raw default"):
            assert (
                parse_default("not-a-number", "int", context="Op.p") == "not-a-number"
            )

    def test_unknown_type_passes_through(self):
        assert parse_default("whatever", "com.example.Pojo") == "whatever"


class TestTypeNames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("boolean", "boolean"),
            ("class java.lang.Boolean", "Boolean"),
            ("class java.lang.String", "String"),
            ("class [Ljava.lang.String;", "String[]"),
            ("class [I", "int[]"),
            ("class java.io.File", "File"),
        ],
    )
    def test_known_types(self, raw, expected):
        assert java_type_name(raw) == expected

    def test_unknown_type_keeps_class_name(self):
        assert java_type_name("class com.example.Thing") == "com.example.Thing"

    @pytest.mark.parametrize(
        ("type_name", "expected"),
        [
            ("boolean", "bool"),
            ("int", "int"),
            ("double", "float"),
            ("String", "str"),
            ("String[]", "tuple[str, ...]"),
            ("File", "str"),
        ],
    )
    def test_python_types(self, type_name, expected):
        assert python_type_name(type_name, optional=False) == expected

    def test_optional_widens(self):
        assert python_type_name("String", optional=True) == "str | None"

    def test_pojo_becomes_any(self):
        assert python_type_name("com.example.Pojo", optional=False) == "Any"


class TestCommittedRegistry:
    def test_loads(self, registry):
        assert len(registry) > 400

    def test_terrain_correction(self, registry):
        spec = registry["Terrain-Correction"]
        assert spec.cls.endswith("RangeDopplerGeocodingOp")
        assert spec.params["demName"].default == "SRTM 3Sec"
        assert spec.params["nodataValueAtSea"].default is True
        assert spec.params["pixelSpacingInMeter"].default == 0.0
        assert spec.min_sources == 1
        assert spec.max_sources == 1

    def test_value_set_present(self, registry):
        spec = registry["Terrain-Correction"]
        assert "BILINEAR_INTERPOLATION" in spec.params["demResamplingMethod"].value_set

    def test_required_parameter(self, registry):
        assert registry["Read"].params["file"].required is True
        assert registry["Write"].params["file"].required is False

    def test_array_source_operator(self, registry):
        spec = registry["Back-Geocoding"]
        assert spec.takes_source_array is True
        assert spec.max_sources is None
        assert spec.min_sources == 1

    def test_source_arity(self, registry):
        assert registry["Read"].min_sources == 0
        assert registry["Read"].max_sources == 0

    def test_parameter_alias_resolution(self, registry):
        spec = registry["Terrain-Correction"]
        assert spec.resolve_param("sourceBands") is spec.params["sourceBandNames"]
        assert spec.resolve_param("sourceBandNames") is spec.params["sourceBandNames"]
        assert spec.resolve_param("nonsense") is None

    def test_sar_classification(self, registry):
        assert registry["Terrain-Correction"].is_sar is True
        assert registry["Read"].is_sar is True
        assert registry["c2rcc.olci"].is_sar is False

    def test_json_round_trip(self, registry):
        """Compared as documents: two NaN defaults would never compare equal."""
        document = registry_to_json(registry, version="test")
        restored = registry_to_json(registry_from_json(document), version="test")
        assert restored == document

    def test_committed_file_is_valid_json(self):
        document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        assert document["_meta"]["operator_count"] == len(document["operators"])

    def test_committed_file_is_standards_compliant(self):
        """JSON has no NaN literal; two operators default a float to NaN."""
        text = REGISTRY_PATH.read_text(encoding="utf-8")
        json.loads(text, parse_constant=_reject_constant)

    def test_non_finite_defaults_survive_the_round_trip(self, registry):
        spec = registry["FlhMci"].params["invalidFlhMciValue"]
        assert math.isnan(spec.default)

    def test_explicit_path_bypasses_cache(self, registry):
        reloaded = load_registry(REGISTRY_PATH)
        assert set(reloaded) == set(registry)
        assert reloaded["Terrain-Correction"] == registry["Terrain-Correction"]


@pytest.mark.snap
class TestAgainstLiveSnap:
    def test_committed_registry_matches_installed_snap(self, registry):
        """The snapshot must not drift from the SNAP that is installed.

        Compared as documents so the two NaN defaults do not spuriously differ.
        """
        from radar_snap_lib.snap_ops.registry import introspect_all

        live = registry_to_json(introspect_all(), version="x")
        committed = registry_to_json(registry, version="x")
        assert live == committed
