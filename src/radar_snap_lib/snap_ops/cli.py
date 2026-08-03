"""Command line entry point: ``radar-snap``.

Only ``run`` and ``gen-registry`` need SNAP.  Listing, describing, validating and
dumping XML all work off the committed registry, so they are fast and usable on
a machine without SNAP installed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from radar_snap_lib.snap_ops.registry import Registry, load_registry

__all__ = ["main"]


def _filtered(registry: Registry, which: str) -> list[str]:
    if which == "sar":
        return [alias for alias, spec in registry.items() if spec.is_sar]
    if which == "core":
        return [
            alias
            for alias, spec in registry.items()
            if spec.cls.startswith("org.esa.snap.core.gpf.")
        ]
    return list(registry)


def _cmd_ops(args: argparse.Namespace) -> int:
    registry = load_registry()
    for alias in sorted(_filtered(registry, args.filter)):
        print(alias)
    return 0


def _cmd_describe(args: argparse.Namespace) -> int:
    registry = load_registry()
    spec = registry.get(args.operator)
    if spec is None:
        print(f"Unknown operator: {args.operator}", file=sys.stderr)
        return 2

    print(f"{spec.alias}  ({spec.cls})")
    if spec.description:
        print(f"  {spec.description}")

    if spec.takes_source_array:
        print("\nSources: any number (sourceProducts)")
    elif spec.sources:
        names = ", ".join(
            f"{s.name}{' [optional]' if s.optional else ''}" for s in spec.sources
        )
        print(f"\nSources: {names}")
    else:
        print("\nSources: none")

    if not spec.params:
        print("\nNo parameters.")
        return 0

    width = max(len(name) for name in spec.params)
    print(f"\nParameters ({len(spec.params)}):")
    for param in spec.params.values():
        default = "" if param.default is None else f" = {param.default!r}"
        flags = " [required]" if param.required else ""
        print(f"  {param.name:<{width}}  {param.type}{default}{flags}")
        if param.value_set:
            print(f"  {'':<{width}}  one of: {', '.join(param.value_set)}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, OpsConfig

    try:
        config = OpsConfig.load(args.config)
        errors = config.validate()
    except GraphConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    if errors:
        print(GraphConfigError(errors, str(args.config)), file=sys.stderr)
        return 1
    print(f"{args.config}: OK")
    return 0


def _cmd_dump_xml(args: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, OpsConfig

    try:
        xml = OpsConfig.load(args.config).to_xml(args.output)
    except GraphConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.output:
        print(f"Wrote {args.output}")
    else:
        print(xml, end="")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError
    from radar_snap_lib.snap_ops.runner import run_graph

    try:
        run_graph(args.config, dump_xml=args.dump_xml, quiet=args.quiet)
    except GraphConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"{args.config}: done")
    return 0


def _cmd_gen_registry(_: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.codegen import generate_all

    for label, path in generate_all().items():
        print(f"{label:>9}: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar-snap",
        description="Build and run ESA SNAP process graphs from YAML.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ops = sub.add_parser("ops", help="list SNAP operators")
    ops.add_argument(
        "--filter",
        choices=("sar", "core", "all"),
        default="sar",
        help="which operators to list (default: sar)",
    )
    ops.set_defaults(func=_cmd_ops)

    describe = sub.add_parser("describe", help="show an operator's parameters")
    describe.add_argument("operator")
    describe.set_defaults(func=_cmd_describe)

    validate = sub.add_parser("validate", help="check a config without running it")
    validate.add_argument("config", type=Path)
    validate.set_defaults(func=_cmd_validate)

    dump = sub.add_parser("dump-xml", help="emit the GPF graph XML for a config")
    dump.add_argument("config", type=Path)
    dump.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    dump.set_defaults(func=_cmd_dump_xml)

    run = sub.add_parser("run", help="execute a config through SNAP")
    run.add_argument("config", type=Path)
    run.add_argument("--dump-xml", type=Path, help="also write the graph XML here")
    run.add_argument(
        "--quiet", action="store_true", help="suppress SNAP progress output"
    )
    run.set_defaults(func=_cmd_run)

    gen = sub.add_parser(
        "gen-registry", help="regenerate operators.json, op_funcs.py and the docs"
    )
    gen.set_defaults(func=_cmd_gen_registry)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
