"""SnaphuExport -> [snaphu CLI] -> SnaphuImport split.

SNAP's ``GraphProcessor`` runs one graph per JVM call and cannot shell out
mid-graph. The standard InSAR unwrapping workflow needs the ``snaphu``
binary between ``SnaphuExport`` and ``SnaphuImport``, so a pipeline
containing that pair has to become two independent graph executions joined
by a subprocess call. This module handles the JVM-free half of that: given
an already-parsed node list, detect the pair, validate the split is
well-formed, and partition the nodes into the two graphs.

See docs/superpowers/specs/2026-08-05-snaphu-subprocess-design.md for the
full design, including how the facts this module encodes (the two-source
``SnaphuImport`` wiring, the ``snaphu -f snaphu.conf ...`` conf-file line,
the ``UnwPhase_*.snaphu.hdr`` output naming) were confirmed against SNAP's
own bundled reference graphs and tool-adapter templates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, ParsedNode, ancestors

__all__ = [
    "EXPORT_OP",
    "IMPORT_OP",
    "SnaphuSplit",
    "check_binary",
    "check_corrfile",
    "find_conf",
    "find_exported_product",
    "find_unwrapped_product",
    "parse_command",
    "run_snaphu",
    "split_at_snaphu",
]

EXPORT_OP = "SnaphuExport"
IMPORT_OP = "SnaphuImport"

_COMMAND_MARKER = "snaphu -f snaphu.conf"


def parse_command(conf_path: Path) -> list[str]:
    """Extract the recommended ``snaphu`` invocation from a ``snaphu.conf``.

    SNAP's ``SnaphuExport`` writes the exact recommended command line as a
    comment near the top of the file. This is a direct port of the same
    scan SNAP Desktop's own "External Tools" adapter does (see
    ``~/.snap/auxdata/tool-adapters/Snaphu-unwrapping/*.vm`` on a machine
    with SNAP installed): look at the first 10 lines for one containing
    ``'snaphu -f snaphu.conf'``, then return everything from ``-f`` onward,
    split on whitespace.
    """
    with conf_path.open(encoding="utf-8") as handle:
        lines = [next(handle, "") for _ in range(10)]

    for line in lines:
        marker_index = line.find(_COMMAND_MARKER)
        if marker_index != -1:
            exec_line = line[marker_index + len("snaphu ") :]
            return exec_line.split()

    raise RuntimeError(
        f"{conf_path}: no {_COMMAND_MARKER!r} line found in the first 10 lines"
    )


def _one_match(target_folder: Path, pattern: str, *, what: str) -> Path:
    """Find exactly one match for ``pattern`` under ``target_folder``.

    Searches recursively (``rglob``): a real ``SnaphuExport`` run writes its
    output one directory level down, in a subdirectory named after the
    product, not directly into ``target_folder`` -- confirmed against a real
    SNAP run.
    """
    matches = sorted(target_folder.rglob(pattern))
    if not matches:
        raise RuntimeError(f"No {what} found in {target_folder} (pattern {pattern!r})")
    if len(matches) > 1:
        names = [m.name for m in matches]
        raise RuntimeError(f"Multiple {what} found in {target_folder}: {names}")
    return matches[0]


def find_conf(target_folder: Path) -> Path:
    """Locate the ``snaphu.conf`` a ``SnaphuExport`` run wrote."""
    return _one_match(target_folder, "snaphu.conf", what="snaphu.conf")


def find_exported_product(target_folder: Path) -> Path:
    """Locate the wrapped-phase product ``SnaphuExport`` wrote.

    ``SnaphuExport`` writes ENVI format (a ``.hdr``/``.img`` pair named
    ``Phase_*.snaphu.hdr``/``.img``), not BEAM-DIMAP -- confirmed against a
    real SNAP run. The ``Phase_`` prefix (rather than a bare ``*.snaphu.hdr``)
    matters: SNAP pre-writes the ``UnwPhase_*.snaphu.hdr`` stub (see
    :func:`find_unwrapped_product`) before ``snaphu`` ever runs, so both
    files coexist in the same directory at the point this is called.
    """
    return _one_match(
        target_folder, "Phase_*.snaphu.hdr", what="exported Phase_*.snaphu.hdr product"
    )


def find_unwrapped_product(target_folder: Path) -> Path:
    """Locate the unwrapped output the ``snaphu`` subprocess wrote.

    Naming convention confirmed against SNAP's own
    ``Snaphu-unwrapping-after.vm`` tool-adapter template. ``SnaphuExport``
    itself pre-writes the ``.hdr`` as an empty-data stub during graph A,
    before ``snaphu`` ever runs -- confirmed against a real SNAP run -- so
    the ``.hdr``'s mere existence does not prove ``snaphu`` succeeded. The
    sibling ``.img`` (the file ``snaphu`` itself writes, per its
    ``OUTFILE`` in ``snaphu.conf``) must also exist and be non-empty.
    """
    hdr = _one_match(
        target_folder,
        "UnwPhase_*.snaphu.hdr",
        what="unwrapped UnwPhase_*.snaphu.hdr output",
    )
    img = hdr.with_suffix(".img")
    if not img.is_file() or img.stat().st_size == 0:
        raise RuntimeError(
            f"{hdr}: header exists but snaphu did not write its data file "
            f"({img}) -- snaphu likely exited zero without actually "
            f"producing output"
        )
    return hdr


def check_corrfile(conf_path: Path) -> Path | None:
    """Verify the coherence file ``conf_path`` names actually exists.

    ``SnaphuExport`` writes a ``CORRFILE`` line whenever the product reaching
    it carries a coherence band, but it does not reliably write that band's
    raster alongside it. snaphu then aborts once per tile, deep in
    ``snaphu_tiles_*/tmptilelog_*`` files nobody reads, after graph A has
    already been computed in full.

    Returns the resolved path, or ``None`` when the conf names no
    ``CORRFILE`` at all -- snaphu treats that as "assume a constant
    correlation" (``snaphu_cost.c``) and runs, so it is not an error here.
    """
    for raw in conf_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] != "CORRFILE" or len(parts) < 2:
            continue
        # snaphu runs with the conf's directory as its working directory,
        # so a bare filename resolves against that.
        corrfile = Path(parts[1])
        resolved = corrfile if corrfile.is_absolute() else conf_path.parent / corrfile
        if not resolved.is_file():
            raise RuntimeError(
                f"{conf_path.name} names a correlation file that was never "
                f"written: {resolved}\n\n"
                f"SnaphuExport adds this CORRFILE line when the product "
                f"reaching it has a coherence band, but it did not write the "
                f"band's raster. snaphu would abort once per tile.\n\n"
                f"Make sure a coherence band survives to SnaphuExport "
                f"(Interferogram needs includeCoherence: true), or drop the "
                f"CORRFILE line from the conf and add 'DEFAULTCORR 0.4' to "
                f"unwrap with a constant correlation instead."
            )
        return resolved
    return None


def _not_found_message(snaphu_bin: str) -> str:
    return (
        f"snaphu binary not found: {snaphu_bin!r}. Install snaphu and ensure "
        f"it is on PATH, or pass a different snaphu_bin."
    )


def _not_executable_message(path: Path) -> str:
    return (
        f"snaphu binary is not executable: {path}\n\n"
        f"SNAP's auxdata unpacker does not preserve Unix permission bits, so a "
        f"snaphu it downloaded into ~/.snap/auxdata usually lands without one. "
        f"Make it executable with:\n\n"
        f"    chmod +x {path}"
    )


def check_binary(snaphu_bin: str) -> Path:
    """Resolve ``snaphu_bin`` to a runnable executable, or raise ``RuntimeError``.

    Called before the first graph of a split runs, so that a missing or
    non-executable binary fails in milliseconds instead of after a full
    coregistration graph has already been computed -- the failure mode this
    check exists to prevent.
    """
    has_separator = os.sep in snaphu_bin or (
        os.altsep is not None and os.altsep in snaphu_bin
    )
    if has_separator:
        path = Path(snaphu_bin)
        if not path.is_file():
            raise RuntimeError(_not_found_message(snaphu_bin))
        if not os.access(path, os.X_OK):
            raise RuntimeError(_not_executable_message(path))
        return path

    # A bare name: which() already filters on the executable bit, so a
    # non-executable hit is indistinguishable from no hit at all.
    found = shutil.which(snaphu_bin)
    if found is None:
        raise RuntimeError(_not_found_message(snaphu_bin))
    return Path(found)


def run_snaphu(
    conf_path: Path, *, snaphu_bin: str = "snaphu", quiet: bool = False
) -> subprocess.CompletedProcess[bytes]:
    """Run ``snaphu`` with the command recorded in ``conf_path``.

    Runs with ``conf_path.parent`` as the working directory, matching where
    ``SnaphuExport`` wrote the conf file and its referenced input. Raises
    ``RuntimeError`` -- not the raw ``subprocess`` exceptions -- so callers
    get one exception type to catch, with the binary name or captured
    stderr already in the message.
    """
    command = [snaphu_bin, *parse_command(conf_path)]
    try:
        return subprocess.run(
            command,
            cwd=conf_path.parent,
            check=True,
            capture_output=quiet,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(_not_found_message(snaphu_bin)) from exc
    except PermissionError as exc:
        raise RuntimeError(_not_executable_message(Path(snaphu_bin))) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
        raise RuntimeError(
            f"snaphu exited with code {exc.returncode}: {' '.join(command)}\n{stderr}"
        ) from exc


@dataclass
class SnaphuSplit:
    """A pipeline partitioned at its SnaphuExport/SnaphuImport boundary."""

    graph_a: list[ParsedNode]
    export_node: ParsedNode
    import_node: ParsedNode
    graph_b: list[ParsedNode]


def _descendants(node_id: str, by_id: dict[str, ParsedNode]) -> set[str]:
    children: dict[str, list[str]] = {n: [] for n in by_id}
    for node in by_id.values():
        for source in node.sources:
            if source in children:
                children[source].append(node.id)

    seen: set[str] = set()

    def visit(current: str) -> None:
        if current in seen or current not in by_id:
            return
        seen.add(current)
        for child in children[current]:
            visit(child)

    visit(node_id)
    return seen


def split_at_snaphu(nodes: list[ParsedNode]) -> SnaphuSplit | None:
    """Detect and validate a SnaphuExport/SnaphuImport split.

    Returns ``None`` when there is no ``SnaphuExport`` node -- the common
    case, meaning the caller should run ``nodes`` as one graph as before.
    Raises :class:`GraphConfigError` for any malformed split.
    """
    exports = [n for n in nodes if n.op == EXPORT_OP]
    if not exports:
        return None

    errors: list[str] = []
    if len(exports) != 1:
        errors.append(
            f"A pipeline must have exactly one {EXPORT_OP!r} node, "
            f"found {len(exports)}: {[n.id for n in exports]}"
        )

    imports = [n for n in nodes if n.op == IMPORT_OP]
    if len(imports) != 1:
        errors.append(
            f"A pipeline with a {EXPORT_OP!r} node must have exactly one "
            f"{IMPORT_OP!r} node, found {len(imports)}: {[n.id for n in imports]}"
        )

    if errors:
        raise GraphConfigError(errors)

    export_node = exports[0]
    import_node = imports[0]

    if not export_node.params.get("targetFolder"):
        raise GraphConfigError(
            [
                f"Node {export_node.id!r}: {EXPORT_OP!r} requires a "
                f"'targetFolder' parameter -- the runner needs it to locate "
                f"snaphu.conf and the exported product"
            ]
        )

    if export_node.id not in import_node.sources:
        raise GraphConfigError(
            [
                f"Node {import_node.id!r}: a {IMPORT_OP!r} node must list "
                f"{export_node.id!r} in its sources to be wired to the matching "
                f"{EXPORT_OP!r} node"
            ]
        )
    if import_node.sources != [export_node.id]:
        raise GraphConfigError(
            [
                f"Node {import_node.id!r}: sources must be only "
                f"{export_node.id!r} -- the runner injects the two real "
                f"SnaphuImport inputs (the re-read export and the unwrapped "
                f"result) itself, got {import_node.sources!r}"
            ]
        )

    by_id = {n.id: n for n in nodes}
    graph_a_ids = ancestors(export_node.id, by_id)
    graph_b_ids = _descendants(import_node.id, by_id)

    overlap = graph_a_ids & graph_b_ids
    if overlap:
        raise GraphConfigError(
            [
                f"Node(s) {sorted(overlap)!r} are reachable from both sides of "
                f"the {EXPORT_OP!r}/{IMPORT_OP!r} split, which is not supported"
            ]
        )

    all_ids = {n.id for n in nodes}
    uncovered = all_ids - graph_a_ids - graph_b_ids
    if uncovered:
        raise GraphConfigError(
            [
                f"Node(s) {sorted(uncovered)!r} are not reachable from either "
                f"side of the {EXPORT_OP!r}/{IMPORT_OP!r} split"
            ]
        )

    for node_id in graph_b_ids:
        if node_id == import_node.id:
            continue
        node = by_id[node_id]
        crossing = [s for s in node.sources if s in graph_a_ids]
        if crossing:
            raise GraphConfigError(
                [
                    f"Node {node.id!r}: sources {crossing!r} cross from the "
                    f"pre-snaphu half of the pipeline into the post-snaphu half "
                    f"directly, which is not supported -- only "
                    f"{IMPORT_OP!r} may reference {EXPORT_OP!r}"
                ]
            )

    graph_a = [n for n in nodes if n.id in graph_a_ids]
    graph_b = [n for n in nodes if n.id in graph_b_ids]
    return SnaphuSplit(
        graph_a=graph_a,
        export_node=export_node,
        import_node=import_node,
        graph_b=graph_b,
    )
