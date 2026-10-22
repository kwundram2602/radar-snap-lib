"""Command line entry point: ``radar-snap``.

Only ``run`` and ``gen-registry`` need SNAP.  Listing, describing, validating and
dumping XML all work off the committed registry, so they are fast and usable on
a machine without SNAP installed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from radar_snap_lib.snap_ops.registry import Registry, load_registry

if TYPE_CHECKING:
    from omegaconf import DictConfig

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


class ConfigLoadError(Exception):
    """Raised when a config file cannot be found or parsed."""


def _load_source(path: Path) -> DictConfig:
    """Read and parse a config file, or raise ConfigLoadError with a clean message."""
    from omegaconf import DictConfig, OmegaConf  # noqa: PLC0415

    if not path.is_file():
        raise ConfigLoadError(f"Config not found: {path}")
    try:
        loaded = OmegaConf.load(path)
    except Exception as exc:  # yaml.YAMLError and friends
        raise ConfigLoadError(f"Cannot parse {path}: {exc}") from exc
    if not isinstance(loaded, DictConfig):
        raise ConfigLoadError(f"Config root must be a mapping: {path}")
    return loaded


def _is_pipeline_config(source: Path | DictConfig) -> bool:
    """A ``pipeline`` key means a graph config; anything else is a search."""
    from omegaconf import DictConfig, OmegaConf  # noqa: PLC0415

    loaded = source if isinstance(source, DictConfig) else OmegaConf.load(source)
    return isinstance(loaded, DictConfig) and "pipeline" in loaded


def _resolve_snaphu_bin(args: argparse.Namespace) -> str:
    """Resolve the snaphu binary the same way for every subcommand."""
    from radar_snap_lib.config import SNAPHU_BIN_VAR, env_value  # noqa: PLC0415

    return getattr(args, "snaphu_bin", None) or env_value(SNAPHU_BIN_VAR) or "snaphu"


def _warn_if_snaphu_unusable(snaphu_bin: str) -> None:
    """Report an unusable snaphu binary without failing the command.

    ``validate`` is documented to work without SNAP or snaphu installed, so
    this stays a warning and leaves the exit code alone -- it only buys the
    user the earliest possible notice that a later ``run`` would abort.
    """
    from radar_snap_lib.snap_ops.snaphu import check_binary  # noqa: PLC0415

    try:
        check_binary(snaphu_bin)
    except RuntimeError as exc:
        print(
            f"warning: this config has a SnaphuExport/SnaphuImport pair, but "
            f"`radar-snap run` would not get past it:\n{exc}",
            file=sys.stderr,
        )


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        loaded = _load_source(Path(args.config))
    except ConfigLoadError as exc:
        print(exc, file=sys.stderr)
        return 2

    if _is_pipeline_config(loaded):
        from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, OpsConfig
        from radar_snap_lib.snap_ops.snaphu import split_at_snaphu

        split = None
        try:
            config = OpsConfig.load(loaded, source=str(args.config))
            nodes = config.parse()
            errors = config.validate(nodes)
            if not errors:
                split = split_at_snaphu(nodes)
        except GraphConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        if errors:
            print(GraphConfigError(errors, str(args.config)), file=sys.stderr)
            return 1
        if split is not None:
            _warn_if_snaphu_unusable(_resolve_snaphu_bin(args))
    else:
        from radar_snap_lib.search.SearchConfig import SearchConfig, SearchConfigError

        try:
            config = SearchConfig.load(loaded, source=str(args.config))
            errors = config.validate()
        except SearchConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        if errors:
            print(SearchConfigError(errors, str(args.config)), file=sys.stderr)
            return 1

    print(f"{args.config}: OK")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    import radar_snap_lib.search as search_pkg  # noqa: PLC0415
    from radar_snap_lib.search.runner import product_file_name  # noqa: PLC0415
    from radar_snap_lib.search.SearchConfig import (  # noqa: PLC0415
        SearchConfig,
        SearchConfigError,
    )

    try:
        loaded = _load_source(Path(args.config))
    except ConfigLoadError as exc:
        print(exc, file=sys.stderr)
        return 2

    config = SearchConfig.load(loaded, source=str(args.config))
    try:
        results = search_pkg.search(config)
    except SearchConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    output = config.output
    where = f", written to {output}" if output is not None else ""
    print(f"{args.config}: {len(results)} scene(s){where}")
    if output is None:
        for product in results:
            print(f"  {product_file_name(product, default='?')}")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    import radar_snap_lib.search as search_pkg  # noqa: PLC0415
    from radar_snap_lib.search.SearchConfig import (  # noqa: PLC0415
        SearchConfig,
        SearchConfigError,
    )

    try:
        loaded = _load_source(Path(args.config))
    except ConfigLoadError as exc:
        print(exc, file=sys.stderr)
        return 2

    config = SearchConfig.load(loaded, source=str(args.config))
    try:
        paths = search_pkg.download(config)
    except SearchConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"{args.config}: {len(paths)} file(s)")
    for path in paths:
        print(f"  {path}")
    return 0


def _cmd_dump_xml(args: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.OpsConfig import (
        GraphConfigError,
        OpsConfig,
        build_graph,
        dependency_order,
        save_write_nodes,
    )
    from radar_snap_lib.snap_ops.snaphu import split_at_snaphu

    try:
        loaded = _load_source(Path(args.config))
    except ConfigLoadError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        config = OpsConfig.load(loaded, source=str(args.config))
        nodes = config.parse()
        errors = config.validate(nodes)
        if errors:
            raise GraphConfigError(errors, config.source)
        # Mirror what `run` executes: it injects a Write per `save:` node.
        output_dir = config.output_dir
        saves = save_write_nodes(nodes, output_dir) if output_dir is not None else []

        split = split_at_snaphu(nodes)
        if split is not None:
            graph_a_ids = {n.id for n in split.graph_a}
            saves_a = [n for n in saves if n.sources[0] in graph_a_ids]
            xml = build_graph(
                dependency_order([*split.graph_a, *saves_a]),
                config.registry,
                graph_id="radar-snap-a",
            ).to_xml(args.output)
        else:
            xml = build_graph(
                dependency_order([*nodes, *saves]), config.registry
            ).to_xml(args.output)
    except GraphConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.output:
        print(f"Wrote {args.output}")
    else:
        print(xml, end="")

    if split is not None:
        print(
            "\nThis config also has a SnaphuExport/SnaphuImport pair -- graph B "
            "depends on files snaphu writes at run time, so it can't be dumped "
            "statically. Use `radar-snap run --dump-xml <path>` instead, which "
            "writes both graphs after actually running.",
            file=sys.stderr,
        )
    return 0


def _cmd_visualize(args: argparse.Namespace) -> int:
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    from radar_snap_lib.snap_ops.graph import GraphError  # noqa: PLC0415
    from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError  # noqa: PLC0415
    from radar_snap_lib.snap_ops.visualization import visualize  # noqa: PLC0415

    source = Path(args.source)
    if not source.is_file():
        print(f"Not found: {source}", file=sys.stderr)
        return 2
    try:
        output = visualize(source, args.output)
    except (GraphConfigError, GraphError, ET.ParseError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:  # no Graphviz, or dot failed
        print(exc, file=sys.stderr)
        return 1
    print(f"Wrote {output}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError
    from radar_snap_lib.snap_ops.runner import run_graph

    snaphu_bin = _resolve_snaphu_bin(args)

    try:
        run_graph(
            args.config, dump_xml=args.dump_xml, snaphu_bin=snaphu_bin, quiet=args.quiet
        )
    except GraphConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"{args.config}: done")
    _report_written(args.config)
    return 0


def _report_written(config_path: Path) -> None:
    """List the graph XML and saved intermediates, if the config asked for any."""
    from radar_snap_lib.snap_ops.OpsConfig import OpsConfig  # noqa: PLC0415

    try:
        output_dir = OpsConfig.load(config_path).output_dir
    except Exception:  # noqa: BLE001 - reporting must never fail a finished run
        return
    if output_dir is None:
        return
    written = sorted(p for p in output_dir.glob("graph*.xml")) + sorted(
        output_dir.glob("*.dim")
    )
    for path in written:
        print(f"  wrote {path}")


def _cmd_gen_registry(_: argparse.Namespace) -> int:
    from radar_snap_lib.snap_ops.codegen import generate_all

    for label, path in generate_all().items():
        print(f"{label:>9}: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar-snap",
        description=(
            "Search the ASF archive and run ESA SNAP process graphs, from YAML."
        ),
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

    validate = sub.add_parser(
        "validate", help="check a config (graph or search) without running it"
    )
    validate.add_argument("config", type=Path)
    validate.add_argument(
        "--snaphu-bin",
        help=(
            "path to the snaphu binary to check, for configs with a "
            "SnaphuExport/SnaphuImport pair (default: $SNAPHU_BIN env/.env "
            "var, or 'snaphu' on PATH); an unusable binary is only a warning"
        ),
    )
    validate.set_defaults(func=_cmd_validate)

    search_cmd = sub.add_parser(
        "search", help="run an ASF archive search from a config"
    )
    search_cmd.add_argument("config", type=Path)
    search_cmd.set_defaults(func=_cmd_search)

    download_cmd = sub.add_parser(
        "download", help="search, then download the hits into the config's dest"
    )
    download_cmd.add_argument("config", type=Path)
    download_cmd.set_defaults(func=_cmd_download)

    dump = sub.add_parser("dump-xml", help="emit the GPF graph XML for a config")
    dump.add_argument("config", type=Path)
    dump.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    dump.set_defaults(func=_cmd_dump_xml)

    visualize = sub.add_parser(
        "visualize",
        help="draw a config or a graph XML with Graphviz (svg, png, pdf or dot)",
    )
    visualize.add_argument(
        "source", type=Path, help="pipeline config (YAML) or GPF graph XML"
    )
    visualize.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output file, format from the suffix (default: <source>.svg)",
    )
    visualize.set_defaults(func=_cmd_visualize)

    run = sub.add_parser("run", help="execute a config through SNAP")
    run.add_argument("config", type=Path)
    run.add_argument("--dump-xml", type=Path, help="also write the graph XML here")
    run.add_argument(
        "--snaphu-bin",
        help=(
            "path to the snaphu binary, for configs with a SnaphuExport/"
            "SnaphuImport pair (default: $SNAPHU_BIN env/.env var, or "
            "'snaphu' on PATH)"
        ),
    )
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
