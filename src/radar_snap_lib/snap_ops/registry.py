"""Typed model of the ESA SNAP operator catalogue.

SNAP describes its operators through ``OperatorDescriptor`` objects whose
``getDefaultValue()`` *always* returns a ``str`` (or ``None``) -- never a typed
value.  Anything that wants to generate Python code or validate a config must
therefore parse each default against its declared Java type.  This module does
that once and exposes the result as plain dataclasses.

Two ways in, one data model out:

* :func:`introspect_all` walks the live SPI registry (needs the JVM).
* :func:`load_registry` reads the committed ``operators.json`` snapshot (no JVM).

Everything downstream -- validation, codegen, the CLI -- consumes the dataclasses
and so never needs SNAP running.
"""

from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).with_name("operators.json")

#: Java type name -> canonical short name used in ``operators.json``.
_JAVA_TYPE_NAMES = {
    "boolean": "boolean",
    "class java.lang.Boolean": "Boolean",
    "int": "int",
    "class java.lang.Integer": "Integer",
    "short": "short",
    "long": "long",
    "double": "double",
    "class java.lang.Double": "Double",
    "float": "float",
    "class java.lang.Float": "Float",
    "class java.lang.String": "String",
    "class [Ljava.lang.String;": "String[]",
    "class [I": "int[]",
    "class [D": "double[]",
    "class [F": "float[]",
    "class java.io.File": "File",
    "class [Ljava.io.File;": "File[]",
}

_BOOL_TYPES = {"boolean", "Boolean"}
_INT_TYPES = {"int", "Integer", "short", "long"}
_FLOAT_TYPES = {"double", "Double", "float", "Float"}
_STR_TYPES = {"String", "File"}
_STR_SEQ_TYPES = {"String[]", "File[]"}
_INT_SEQ_TYPES = {"int[]"}
_FLOAT_SEQ_TYPES = {"double[]", "float[]"}

#: Values SNAP uses for booleans.  ``off``/``on`` and ``1``/``0`` really do occur.
_TRUE = {"true", "1", "on", "yes"}
_FALSE = {"false", "0", "off", "no"}

#: Java package prefixes kept when generating the builder API.  The optical
#: operators (``eu.esa.opt.*``, ~283 of them) are irrelevant to a radar library.
SAR_PACKAGE_PREFIXES = (
    "eu.esa.sar.",
    "org.esa.snap.core.gpf.",
    "org.csa.rstb.",
    "org.jlinda.",
    "org.esa.snap.raster.",
    "org.esa.snap.dem.",
    "org.esa.snap.classification.",
    "org.esa.snap.cluster.",
    "org.esa.snap.landcover.",
)


def java_type_name(raw: str) -> str:
    """Canonical short name for a Java type as reported by SNAP.

    Unrecognised types (the nested POJO parameters) keep their raw class name so
    the registry stays lossless.
    """
    if raw in _JAVA_TYPE_NAMES:
        return _JAVA_TYPE_NAMES[raw]
    return raw.removeprefix("class ").removeprefix("interface ")


def python_type_name(type_name: str, *, optional: bool) -> str:
    """Python annotation for a canonical SNAP type name."""
    if type_name in _BOOL_TYPES:
        base = "bool"
    elif type_name in _INT_TYPES:
        base = "int"
    elif type_name in _FLOAT_TYPES:
        base = "float"
    elif type_name in _STR_TYPES:
        base = "str"
    elif type_name in _STR_SEQ_TYPES:
        base = "tuple[str, ...]"
    elif type_name in _INT_SEQ_TYPES:
        base = "tuple[int, ...]"
    elif type_name in _FLOAT_SEQ_TYPES:
        base = "tuple[float, ...]"
    else:
        # Nested POJOs and enums: accept whatever the caller passes and hand it
        # to the XML serialiser untouched.
        return "Any"
    return f"{base} | None" if optional else base


def parse_default(raw: str | None, type_name: str, *, context: str = "") -> Any:
    """Turn SNAP's stringly-typed default into a real Python value.

    ``raw`` is what ``ParameterDescriptor.getDefaultValue()` returned: a ``str``
    or ``None``.  Returns ``None`` when there is no default.  A value that will
    not parse is kept as its raw string and warned about rather than aborting
    generation of the whole registry.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    try:
        if type_name in _BOOL_TYPES:
            lowered = text.lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise ValueError(f"not a boolean literal: {text!r}")
        if type_name in _INT_TYPES:
            return int(text)
        if type_name in _FLOAT_TYPES:
            # Java float literals carry an 'f'/'F' suffix, e.g. '-999.0f'.
            return float(text.rstrip("fFdD"))
        if type_name in _STR_TYPES:
            # Deliberately verbatim.  'None' is a genuine String default for
            # several operators and must stay the four-character string.
            return text
        if type_name in _STR_SEQ_TYPES:
            return tuple(part.strip() for part in text.split(","))
        if type_name in _INT_SEQ_TYPES:
            return tuple(int(part) for part in text.split(","))
        if type_name in _FLOAT_SEQ_TYPES:
            return tuple(float(part.rstrip("fFdD")) for part in text.split(","))
    except ValueError as exc:
        warnings.warn(
            f"{context or type_name}: keeping raw default {text!r} ({exc})",
            stacklevel=2,
        )
        return text

    # Unknown type: keep the raw string, the XML serialiser passes it through.
    return text


@dataclass(frozen=True)
class SourceSpec:
    """One source-product slot of an operator."""

    name: str
    alias: str | None = None
    optional: bool = False
    is_array: bool = False

    @property
    def xml_name(self) -> str:
        """Element name to use inside ``<sources>`` in a GPF graph."""
        return self.name


@dataclass(frozen=True)
class ParamSpec:
    """One parameter of an operator."""

    name: str
    type: str
    python_type: str
    default: Any = None
    alias: str | None = None
    value_set: tuple[str, ...] = ()
    not_null: bool = False
    not_empty: bool = False
    description: str | None = None

    @property
    def required(self) -> bool:
        """True when the parameter must be given and has no default."""
        return self.not_null and self.default is None


@dataclass(frozen=True)
class OperatorSpec:
    """Everything known about a single SNAP operator."""

    alias: str
    cls: str
    description: str | None = None
    sources: tuple[SourceSpec, ...] = ()
    params: dict[str, ParamSpec] = field(default_factory=dict)

    @property
    def takes_source_array(self) -> bool:
        """True when the operator declares a ``sourceProducts`` array slot."""
        return any(src.is_array for src in self.sources)

    @property
    def min_sources(self) -> int:
        if self.takes_source_array:
            return 1
        return sum(1 for src in self.sources if not src.optional)

    @property
    def max_sources(self) -> int | None:
        """Upper bound on source count, or ``None`` when unbounded."""
        if self.takes_source_array:
            return None
        return len(self.sources)

    @property
    def is_sar(self) -> bool:
        """True for operators worth generating a builder method for."""
        return self.cls.startswith(SAR_PACKAGE_PREFIXES)

    def resolve_param(self, key: str) -> ParamSpec | None:
        """Look a parameter up by its name or its alias."""
        if key in self.params:
            return self.params[key]
        for spec in self.params.values():
            if spec.alias == key:
                return spec
        return None


Registry = dict[str, OperatorSpec]


# --------------------------------------------------------------------------- #
# Live introspection (requires the JVM)
# --------------------------------------------------------------------------- #


def _iter_operator_spis() -> list[Any]:
    from radar_snap_lib.config import ensure_esa_snappy

    ensure_esa_snappy()
    from esa_snappy import GPF  # noqa: PLC0415  (must follow ensure_esa_snappy)

    spi_registry = GPF.getDefaultInstance().getOperatorSpiRegistry()
    spi_registry.loadOperatorSpis()
    iterator = spi_registry.getOperatorSpis().iterator()
    spis = []
    while iterator.hasNext():
        spis.append(iterator.next())
    return spis


def _text(value: Any) -> str | None:
    """Java string -> Python string, mapping empty to None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_specs(descriptor: Any) -> tuple[SourceSpec, ...]:
    specs = [
        SourceSpec(
            name=str(src.getName()),
            alias=_text(src.getAlias()),
            optional=bool(src.isOptional()),
            is_array=False,
        )
        for src in descriptor.getSourceProductDescriptors()
    ]
    array_descriptor = descriptor.getSourceProductsDescriptor()
    if array_descriptor is not None:
        specs.append(
            SourceSpec(
                name=str(array_descriptor.getName()),
                alias=_text(array_descriptor.getAlias()),
                optional=False,
                is_array=True,
            )
        )
    return tuple(specs)


def _param_specs(descriptor: Any, alias: str) -> dict[str, ParamSpec]:
    params: dict[str, ParamSpec] = {}
    for param in descriptor.getParameterDescriptors():
        name = str(param.getName())
        type_name = java_type_name(str(param.getDataType()))
        raw_default = _text(param.getDefaultValue())
        default = parse_default(raw_default, type_name, context=f"{alias}.{name}")
        value_set = tuple(str(v) for v in (param.getValueSet() or ()))
        param_alias = _text(param.getAlias())
        not_null = bool(param.isNotNull())
        # A parameter with no default that SNAP marks non-null must always be
        # supplied, so its annotation stays non-optional.
        required = not_null and default is None
        params[name] = ParamSpec(
            name=name,
            type=type_name,
            python_type=python_type_name(
                type_name, optional=default is None and not required
            ),
            default=default,
            alias=param_alias if param_alias != name else None,
            value_set=value_set,
            not_null=bool(param.isNotNull()),
            not_empty=bool(param.isNotEmpty()),
            description=_text(param.getDescription()),
        )
    return params


def introspect_all() -> Registry:
    """Build the registry by walking the live SNAP SPI registry."""
    registry: Registry = {}
    for spi in _iter_operator_spis():
        descriptor = spi.getOperatorDescriptor()
        alias = str(descriptor.getAlias())
        registry[alias] = OperatorSpec(
            alias=alias,
            cls=str(descriptor.getName()),
            description=_text(descriptor.getDescription()),
            sources=_source_specs(descriptor),
            params=_param_specs(descriptor, alias),
        )
    return dict(sorted(registry.items()))


def snap_version() -> str:
    """Version string of the SNAP engine currently on the path."""
    from radar_snap_lib.config import ensure_esa_snappy

    ensure_esa_snappy()
    from esa_snappy import jpy  # noqa: PLC0415

    try:
        version_class = jpy.get_type("org.esa.snap.core.util.VersionChecker")
        return str(version_class.getInstance().getLocalVersion())
    except Exception:  # pragma: no cover - depends on the SNAP build
        return "unknown"


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


#: JSON has no NaN or Infinity literal, so non-finite float defaults are stored
#: as strings and decoded back using the parameter's declared type.
def _encode_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_encode_default(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _decode_default(value: Any, type_name: str) -> Any:
    if isinstance(value, list):
        return tuple(_decode_default(item, type_name) for item in value)
    if isinstance(value, str) and type_name in _FLOAT_TYPES | _FLOAT_SEQ_TYPES:
        return float(value)
    return value


def _param_to_json(spec: ParamSpec) -> dict[str, Any]:
    data: dict[str, Any] = {"type": spec.type, "python_type": spec.python_type}
    if spec.alias:
        data["alias"] = spec.alias
    data["default"] = _encode_default(spec.default)
    if spec.value_set:
        data["value_set"] = list(spec.value_set)
    if spec.not_null:
        data["not_null"] = True
    if spec.not_empty:
        data["not_empty"] = True
    if spec.description:
        data["description"] = spec.description
    return data


def registry_to_json(registry: Registry, *, version: str = "unknown") -> dict[str, Any]:
    """Serialise a registry into the ``operators.json`` document shape."""
    return {
        "_meta": {"snap_version": version, "operator_count": len(registry)},
        "operators": {
            alias: {
                "class": spec.cls,
                "description": spec.description,
                "sources": [
                    {
                        "name": src.name,
                        "alias": src.alias,
                        "optional": src.optional,
                        "is_array": src.is_array,
                    }
                    for src in spec.sources
                ],
                "params": {
                    name: _param_to_json(param) for name, param in spec.params.items()
                },
            }
            for alias, spec in sorted(registry.items())
        },
    }


def _param_from_json(name: str, data: dict[str, Any]) -> ParamSpec:
    default = _decode_default(data.get("default"), data["type"])
    return ParamSpec(
        name=name,
        type=data["type"],
        python_type=data["python_type"],
        default=default,
        alias=data.get("alias"),
        value_set=tuple(data.get("value_set", ())),
        not_null=data.get("not_null", False),
        not_empty=data.get("not_empty", False),
        description=data.get("description"),
    )


def registry_from_json(document: dict[str, Any]) -> Registry:
    """Inverse of :func:`registry_to_json`."""
    return {
        alias: OperatorSpec(
            alias=alias,
            cls=entry["class"],
            description=entry.get("description"),
            sources=tuple(
                SourceSpec(
                    name=src["name"],
                    alias=src.get("alias"),
                    optional=src.get("optional", False),
                    is_array=src.get("is_array", False),
                )
                for src in entry.get("sources", ())
            ),
            params={
                name: _param_from_json(name, data)
                for name, data in entry.get("params", {}).items()
            },
        )
        for alias, entry in document["operators"].items()
    }


_CACHED: Registry | None = None


def load_registry(path: Path | None = None) -> Registry:
    """Load the committed operator snapshot.  No JVM required.

    The default snapshot is cached; an explicit ``path`` bypasses the cache.
    """
    global _CACHED
    if path is not None:
        return registry_from_json(json.loads(path.read_text(encoding="utf-8")))
    if _CACHED is None:
        if not REGISTRY_PATH.is_file():
            raise RuntimeError(
                f"Operator registry missing: {REGISTRY_PATH}. "
                "Generate it with `radar-snap gen-registry` (requires SNAP)."
            )
        _CACHED = registry_from_json(
            json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        )
    return _CACHED
