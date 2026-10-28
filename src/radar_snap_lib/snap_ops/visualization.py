"""Draw a SNAP graph with Graphviz.

Every operator becomes a card: its name in a header coloured by what kind of
operator it is (I/O, Sentinel-1, calibration, geometry, InSAR, ...), the node id
when it differs, and the parameters that differ from SNAP's defaults, shortened
for reading. Hovering a card in the SVG shows the operator's description and the
parameters in full. Input and output files are drawn as notes, so a graph reads
from the files it starts with to the files it writes::

    from radar_snap_lib.snap_ops import visualize

    visualize("pipeline.yaml")             # -> pipeline.svg next to it
    visualize("graph.xml", "graph.png")    # a graph SNAP or radar-snap wrote

The layout is Graphviz' (``dot`` on ``PATH``); a ``.dot`` output needs none.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from radar_snap_lib.snap_ops.external import EXTERNAL_CLS_PREFIX, is_external
from radar_snap_lib.snap_ops.filter_ops import FILTER_CLS_PREFIX, is_filter
from radar_snap_lib.snap_ops.graph import Graph
from radar_snap_lib.snap_ops.nodes import Node
from radar_snap_lib.snap_ops.OpsConfig import OpsConfig
from radar_snap_lib.snap_ops.registry import Registry, load_registry, parse_default

__all__ = ["graph_from_xml", "render", "to_dot", "visualize"]

#: Output formats by file suffix; ``.dot`` is the Graphviz source itself.
FORMATS = {".svg": "svg", ".png": "png", ".pdf": "pdf"}
PNG_DPI = 150

READERS = frozenset({"Read", "ProductSet-Reader"})
#: Reader parameters drawn as file notes instead of card rows.
FILE_PARAMS = ("file", "fileList")
#: Suffix of the Write nodes the runner injects for ``save:``.
SAVE_SUFFIX = "__save"

#: (category, header colour, Java package prefixes); the first match wins, so
#: the specific SAR packages come before the generic ``eu.esa.sar``.
CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # before the Python category, whose prefix also matches the filters
    ("Filter (scikit-image)", "#f7efb2", (FILTER_CLS_PREFIX,)),
    ("Python (outside SNAP)", "#fde2c8", (EXTERNAL_CLS_PREFIX,)),
    ("Sentinel-1", "#cfe2ff", ("eu.esa.sar.sentinel1",)),
    ("Orbit", "#dcd5f7", ("eu.esa.sar.orbits",)),
    ("Calibration", "#ffe2ad", ("eu.esa.sar.calibration",)),
    (
        "Geometry / terrain",
        "#c9ecca",
        ("eu.esa.sar.sar.gpf.geometric", "org.esa.snap.dem"),
    ),
    ("InSAR", "#ecd3f6", ("eu.esa.sar.insar", "org.jlinda")),
    ("Polarimetry", "#f9d2e4", ("org.csa.rstb",)),
    ("SAR processing", "#c6ecea", ("eu.esa.sar",)),
    ("Optical", "#e8f1c4", ("eu.esa.opt",)),
    ("Raster / utility", "#e3e5e8", ("org.esa.snap",)),
)
IO_CATEGORY = ("Input / output", "#d3dae3")
OTHER_CATEGORY = ("Other", "#eeeeee")

INPUT_FILL = "#eaf2fb"
OUTPUT_FILL = "#e6f4e7"
BORDER = "#5f6b7a"
MUTED = "#5f6b7a"
FONT = "Helvetica"

#: Card rows before the rest is summarised as "+N more".
MAX_ROWS = 8
#: Longest value shown on a card; tooltips keep the full text.
MAX_VALUE = 40
#: File names are wrapped (at "_") to lines of about this length instead.
FILE_LINE = 34
#: A file SNAP reads for a whole product folder; the folder names the product.
PRODUCT_FILES = frozenset({"manifest.safe", "manifest.xml"})

_WKT = re.compile(
    r"^\s*(MULTI)?(POLYGON|POINT|LINESTRING)\s*(Z|M|ZM)?\s*\(", re.IGNORECASE
)
_COORD_PAIR = re.compile(r"-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------- #
# Reading graph XML
# --------------------------------------------------------------------------- #


def graph_from_xml(xml: str | Path, *, registry: Registry | None = None) -> Graph:
    """Build a :class:`Graph` from GPF graph XML (a path or the XML text).

    Reads what radar-snap writes as well as graphs saved from SNAP Desktop:
    node ids, operators, sources in element order and the parameters, typed
    by the operator registry. Parameters at SNAP's default are dropped, as for
    any graph built here.
    """
    text = Path(xml).read_text(encoding="utf-8") if _is_path(xml) else str(xml)
    root = ET.fromstring(text)
    graph = Graph(registry=registry if registry is not None else load_registry())

    parsed: dict[str, tuple[str, list[str], dict[str, Any]]] = {}
    for element in root.iter("node"):
        node_id = element.get("id")
        op = (element.findtext("operator") or "").strip()
        if not node_id or not op:
            continue
        sources = _xml_sources(element.find("sources"))
        spec = graph.spec(op)
        params: dict[str, Any] = {}
        parameters = element.find("parameters")
        for child in parameters if parameters is not None else []:
            value = _xml_value(child)
            if value is None:
                continue
            param = spec.resolve_param(child.tag)
            if param is not None and isinstance(value, str):
                value = parse_default(
                    value, param.type, context=f"{node_id}.{child.tag}"
                )
                if isinstance(value, tuple):
                    value = list(value)
            params[child.tag] = value
        parsed[node_id] = (op, sources, params)

    for node_id in _xml_order(parsed):
        op, sources, params = parsed[node_id]
        graph._add(op, sources, params, node_id=node_id)
    return graph


def _is_path(xml: str | Path) -> bool:
    return isinstance(xml, Path) or not str(xml).lstrip().startswith("<")


def _xml_sources(element: ET.Element | None) -> list[str]:
    """Source node ids in element order: ``refid`` attributes or plain text.

    A ``${sourceProducts}`` placeholder (SNAP Desktop's template graphs) is no
    node and is skipped.
    """
    if element is None:
        return []
    sources = []
    for child in element:
        ref = child.get("refid") or (child.text or "").strip()
        if ref and not ref.startswith("${"):
            sources.append(ref)
    return sources


def _xml_value(element: ET.Element) -> Any:
    """Text of a leaf element, nested elements as a list (repeated tags) or dict."""
    children = list(element)
    if not children:
        text = (element.text or "").strip()
        return text or None
    tags = {child.tag for child in children}
    if len(children) > 1 and len(tags) == 1:
        return [_xml_value(child) for child in children]
    return {child.tag: _xml_value(child) for child in children}


def _xml_order(
    parsed: Mapping[str, tuple[str, list[str], dict[str, Any]]],
) -> list[str]:
    """Node ids with every node after its sources (graph XML may be unordered)."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in seen or node_id not in parsed:
            return
        seen.add(node_id)
        for source in parsed[node_id][1]:
            visit(source)
        order.append(node_id)

    for node_id in parsed:
        visit(node_id)
    return order


# --------------------------------------------------------------------------- #
# DOT
# --------------------------------------------------------------------------- #


def category(graph: Graph, op: str) -> tuple[str, str]:
    """(category, header colour) of an operator, from its Java package."""
    if op in READERS or op == "Write":
        return IO_CATEGORY
    spec = graph.registry.get(op)
    if spec is None:
        return OTHER_CATEGORY
    for name, colour, prefixes in CATEGORIES:
        if spec.cls.startswith(prefixes):
            return name, colour
    return OTHER_CATEGORY


def to_dot(graph: Graph, *, title: str | None = None) -> str:
    """Graphviz DOT source of ``graph``, top to bottom (see the module doc)."""
    lines = [
        "digraph snap {",
        "  rankdir=TB; nodesep=0.35; ranksep=0.45; pad=0.3;",
        f'  fontname="{FONT}"; bgcolor="white";',
        f'  node [fontname="{FONT}", shape=plain];',
        f'  edge [fontname="{FONT}", fontsize=10, color="{BORDER}", '
        f'fontcolor="{MUTED}", arrowsize=0.7];',
    ]
    if title:
        lines.append(
            f'  labelloc="t"; fontsize=18; label=<<B>{_esc(title)}</B><BR/> >;'
        )

    nodes = {node.id: node for node in graph.nodes}
    used: dict[str, str] = {}
    for node in graph.nodes:
        if node.op == "Write":
            lines.extend(_write_lines(node))
            continue
        name, colour = category(graph, node.op)
        used[name] = colour
        lines.append(
            f"  {_id(node.id)} [label=<{_card(node, colour)}>, "
            f'tooltip="{_tooltip(graph, node)}"];'
        )
        if node.op in READERS:
            lines.extend(_input_lines(node))

    for node in graph.nodes:
        if node.op == "Write":
            continue
        for index, source in enumerate(node.sources, start=1):
            attrs = []
            if nodes[source].op == "SnaphuExport" and node.op == "SnaphuImport":
                attrs += ["style=dashed", 'label=" snaphu (external) "']
            elif is_filter(node.op) and not is_filter(nodes[source].op):
                attrs += ["style=dashed", 'label=" GeoTIFF "']
            elif is_external(node.op) or is_external(nodes[source].op):
                attrs += ["style=dashed", 'label=" BEAM-DIMAP "']
            elif len(node.sources) > 1:
                attrs.append(f'label=" {index} "')
            suffix = f" [{', '.join(attrs)}]" if attrs else ""
            lines.append(f"  {_id(source)} -> {_id(node.id)}{suffix};")

    if any(node.op == "Write" for node in graph.nodes):
        used[IO_CATEGORY[0]] = IO_CATEGORY[1]
    lines.extend(_legend_lines(used))
    lines.append("}")
    return "\n".join(lines) + "\n"


def _card(node: Node, colour: str) -> str:
    """HTML-like table label: coloured header, node id, parameter rows."""
    rows = [
        f'<TR><TD BGCOLOR="{colour}" ALIGN="LEFT" CELLPADDING="6">'
        f"<B>{_esc(node.op)}</B></TD></TR>"
    ]
    if node.id != node.op:
        rows.append(
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="10" COLOR="{MUTED}">'
            f"<I>{_esc(node.id)}</I></FONT></TD></TR>"
        )
    params = [
        (key, value)
        for key, value in node.params.items()
        if not (node.op in READERS and key in FILE_PARAMS)
    ]
    for key, value in params[:MAX_ROWS]:
        rows.append(
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="10">'
            f'<FONT COLOR="{MUTED}">{_esc(key)}</FONT> {_esc(short_value(value))}'
            f"</FONT></TD></TR>"
        )
    if len(params) > MAX_ROWS:
        rows.append(
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="10" COLOR="{MUTED}">'
            f"+{len(params) - MAX_ROWS} more</FONT></TD></TR>"
        )
    return (
        f'<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" '
        f'COLOR="{BORDER}" BGCOLOR="white">{"".join(rows)}</TABLE>'
    )


def _input_lines(node: Node) -> list[str]:
    """A note per input file of a reader, with an edge into it."""
    files: list[Any] = []
    for key in FILE_PARAMS:
        value = node.params.get(key)
        files += value if isinstance(value, list) else [value] if value else []
    lines = []
    for index, file in enumerate(files):
        file_id = f"{node.id}__in{index}"
        lines.append(
            f'  {_id(file_id)} [shape=note, style=filled, fillcolor="{INPUT_FILL}", '
            f'color="{BORDER}", fontsize=11, label=<{_file_label(file)}>, '
            f'tooltip="{_dot_text(str(file))}"];'
        )
        lines.append(f"  {_id(file_id)} -> {_id(node.id)};")
    return lines


def _write_lines(node: Node) -> list[str]:
    """A Write as the file it writes, with an edge from what it writes.

    Writes the runner adds for ``save:`` are labelled "save".
    """
    file = node.params.get("file", node.id)
    fmt = node.params.get("formatName")
    label = _file_label(file, str(fmt) if fmt else None)
    lines = [
        f'  {_id(node.id)} [shape=note, style=filled, fillcolor="{OUTPUT_FILL}", '
        f'color="{BORDER}", fontsize=11, label=<{label}>, '
        f'tooltip="{_dot_text(str(file))}"];'
    ]
    edge = ' [label=" save "]' if node.id.endswith(SAVE_SUFFIX) else ""
    lines += [f"  {_id(source)} -> {_id(node.id)}{edge};" for source in node.sources]
    return lines


def _legend_lines(used: Mapping[str, str]) -> list[str]:
    """One row of colour swatches for the categories in the graph, at the bottom."""
    if not used:
        return []
    order = [name for name, _, _ in CATEGORIES] + [IO_CATEGORY[0], OTHER_CATEGORY[0]]
    cells = "".join(
        f'<TD BGCOLOR="{used[name]}" CELLPADDING="4">'
        f'<FONT POINT-SIZE="10">{_esc(name)}</FONT></TD>'
        for name in sorted(used, key=order.index)
    )
    table = (
        f'<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="4" COLOR="{BORDER}">'
        f"<TR>{cells}</TR></TABLE>"
    )
    return [f"  legend [label=<{table}>];", "  { rank=sink; legend; }"]


def _file_label(file: Any, fmt: str | None = None) -> str:
    """The file name in full, wrapped at "_" (product names are long), and the
    format below it.
    """
    lines = [""]
    for part in re.split(r"(?<=_)", file_name(str(file))):
        if lines[-1] and len(lines[-1]) + len(part) > FILE_LINE:
            lines.append("")
        lines[-1] += part
    label = "<BR/>".join(f"<B>{_esc(line)}</B>" for line in lines)
    if fmt:
        label += f'<BR/><FONT POINT-SIZE="9" COLOR="{MUTED}">{_esc(fmt)}</FONT>'
    return label


def short_value(value: Any) -> str:
    """A parameter value as it fits on a card.

    WKT becomes its geometry type and point count, a path its file name, a list
    its shortened items; anything longer than ``MAX_VALUE`` is cut.
    """
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, Mapping):
        text = f"{{{len(value)} entries}}"
    elif isinstance(value, (list, tuple)):
        if any(isinstance(item, Mapping) for item in value):
            text = f"[{len(value)} entries]"
        else:
            text = ", ".join(short_value(item) for item in value)
    else:
        text = str(value)
        if match := _WKT.match(text):
            points = len(_COORD_PAIR.findall(text))
            kind = (match.group(1) or "") + match.group(2)
            text = f"{kind.upper()} ({points} pts)"
        elif ("/" in text or "\\" in text) and "://" not in text:
            text = file_name(text)
    return text if len(text) <= MAX_VALUE else text[: MAX_VALUE - 1] + "…"


def file_name(path: str) -> str:
    """Name of a file path; the product folder for a manifest inside it
    (``S1A_..._COG.SAFE`` instead of ``manifest.safe``).
    """
    parts = Path(path.replace("\\", "/"))
    if parts.name.lower() in PRODUCT_FILES and parts.parent.name:
        return parts.parent.name
    return parts.name or path


def _tooltip(graph: Graph, node: Node) -> str:
    spec = graph.registry.get(node.op)
    lines = [node.op + (f": {spec.description}" if spec and spec.description else "")]
    lines += [f"{key} = {value}" for key, value in node.params.items()]
    return "\\n".join(_dot_text(line) for line in lines)


def _id(node_id: str) -> str:
    return '"' + _dot_text(node_id) + '"'


def _dot_text(text: str) -> str:
    """Text for a quoted DOT string."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _esc(text: str) -> str:
    """Text for an HTML-like Graphviz label."""
    return html.escape(text, quote=True)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render(graph: Graph, output: Path | str, *, title: str | None = None) -> Path:
    """Write the drawing of ``graph`` to ``output``; the suffix picks the format.

    ``.svg``, ``.png`` or ``.pdf`` run Graphviz' ``dot``; ``.dot`` writes the
    DOT source and needs no Graphviz.
    """
    output = Path(output)
    source = to_dot(graph, title=title)
    suffix = output.suffix.lower()
    if suffix == ".dot":
        output.write_text(source, encoding="utf-8")
        return output
    fmt = FORMATS.get(suffix)
    if fmt is None:
        raise ValueError(f"{output}: unknown format, use one of {[*FORMATS, '.dot']}")
    dot = shutil.which("dot")
    if dot is None:
        raise RuntimeError(
            "Graphviz 'dot' not found on PATH: install graphviz, or write a .dot "
            "file and render it elsewhere"
        )
    command = [dot, f"-T{fmt}", "-o", str(output)]
    if fmt == "png":
        command.append(f"-Gdpi={PNG_DPI}")
    result = subprocess.run(command, input=source, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"dot failed: {result.stderr.strip()}")
    return output


def visualize(
    source: str | Path | OpsConfig | Graph,
    output: Path | str | None = None,
    *,
    vars: Mapping[str, Any] | None = None,
    title: str | None = None,
) -> Path:
    """Draw a pipeline config, a graph XML file, an ``OpsConfig`` or a ``Graph``.

    A config is drawn as ``run`` executes it, ``save:`` outputs included;
    ``vars`` fills its ``${vars.*}`` like ``run_graph(..., vars=...)``. Without
    ``output`` the drawing goes next to a file source as ``<stem>.svg``. The
    title defaults to the file name.
    """
    path: Path | None = None
    if isinstance(source, Graph):
        graph = source
    elif isinstance(source, OpsConfig):
        graph = source.to_graph()
    else:
        path = Path(source)
        if path.suffix.lower() == ".xml":
            graph = graph_from_xml(path)
        else:
            graph = OpsConfig.load(path, vars=vars).to_graph()
    if output is None:
        if path is None:
            raise ValueError("an OpsConfig or Graph needs an output path")
        output = path.with_suffix(".svg")
    if title is None and path is not None:
        title = path.name
    return render(graph, output, title=title)
