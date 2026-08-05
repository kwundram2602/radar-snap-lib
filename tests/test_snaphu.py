"""Tests for the SnaphuExport/SnaphuImport split detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, ParsedNode
from radar_snap_lib.snap_ops.snaphu import parse_command, split_at_snaphu


def _node(id_: str, op: str, sources: list[str] | None = None) -> ParsedNode:
    return ParsedNode(id=id_, op=op, sources=sources or [], params={})


LINEAR_WITH_SNAPHU = [
    _node("read", "Read"),
    _node("goldstein", "GoldsteinPhaseFiltering", ["read"]),
    _node("snaphu_export", "SnaphuExport", ["goldstein"]),
    _node("snaphu_import", "SnaphuImport", ["snaphu_export"]),
    _node("terrain_correction", "Terrain-Correction", ["snaphu_import"]),
    _node("write", "Write", ["terrain_correction"]),
]


class TestNoSplit:
    def test_returns_none_without_snaphu_export(self):
        nodes = [_node("read", "Read"), _node("write", "Write", ["read"])]
        assert split_at_snaphu(nodes) is None


class TestHappyPath:
    def test_splits_into_graph_a_and_graph_b(self):
        split = split_at_snaphu(LINEAR_WITH_SNAPHU)
        assert split is not None
        assert [n.id for n in split.graph_a] == ["read", "goldstein", "snaphu_export"]
        assert split.export_node.id == "snaphu_export"
        assert split.import_node.id == "snaphu_import"
        assert [n.id for n in split.graph_b] == [
            "snaphu_import",
            "terrain_correction",
            "write",
        ]


class TestValidationErrors:
    def test_multiple_snaphu_export_nodes(self):
        nodes = [
            *LINEAR_WITH_SNAPHU,
            _node("snaphu_export_2", "SnaphuExport", ["read"]),
        ]
        with pytest.raises(GraphConfigError, match="exactly one 'SnaphuExport'"):
            split_at_snaphu(nodes)

    def test_missing_snaphu_import(self):
        nodes = [
            _node("read", "Read"),
            _node("snaphu_export", "SnaphuExport", ["read"]),
        ]
        with pytest.raises(GraphConfigError, match="exactly one 'SnaphuImport'"):
            split_at_snaphu(nodes)

    def test_multiple_snaphu_import_nodes(self):
        nodes = [
            *LINEAR_WITH_SNAPHU,
            _node("snaphu_import_2", "SnaphuImport", ["snaphu_export"]),
        ]
        with pytest.raises(GraphConfigError, match="exactly one 'SnaphuImport'"):
            split_at_snaphu(nodes)

    def test_import_not_sourced_from_export(self):
        nodes = [
            _node("read", "Read"),
            _node("snaphu_export", "SnaphuExport", ["read"]),
            _node("snaphu_import", "SnaphuImport", ["read"]),
        ]
        with pytest.raises(GraphConfigError, match="must list 'snaphu_export'"):
            split_at_snaphu(nodes)

    def test_import_has_extra_sources(self):
        nodes = [
            _node("read", "Read"),
            _node("snaphu_export", "SnaphuExport", ["read"]),
            _node("snaphu_import", "SnaphuImport", ["snaphu_export", "read"]),
        ]
        with pytest.raises(GraphConfigError, match="only 'snaphu_export'"):
            split_at_snaphu(nodes)

    def test_node_referenced_from_both_halves(self):
        nodes = [
            _node("read", "Read"),
            _node("snaphu_export", "SnaphuExport", ["read"]),
            _node("snaphu_import", "SnaphuImport", ["snaphu_export"]),
            # 'stray' has no sources, so it is never an ancestor of
            # snaphu_export; it is only referenced by 'write' (a descendant
            # of snaphu_import), so it lands in neither graph_a_ids nor
            # graph_b_ids -- this hits the "uncovered" branch, not "overlap".
            _node("stray", "Read"),
            _node("write", "Write", ["snaphu_import", "stray"]),
        ]
        with pytest.raises(GraphConfigError, match="stray"):
            split_at_snaphu(nodes)

    def test_node_reachable_from_both_export_ancestry_and_import_descendants(self):
        # 'x' is wired into a genuine cycle: snaphu_export depends on it
        # directly (making it an ancestor of snaphu_export, i.e. a
        # graph_a_ids member), and it in turn depends on snaphu_import
        # (making it a descendant of snaphu_import, i.e. a graph_b_ids
        # member too). This is the only way to land a node in both sets
        # simultaneously, so it genuinely exercises the "overlap" branch
        # rather than the "uncovered" branch above.
        nodes = [
            _node("read", "Read"),
            _node("x", "Read", ["snaphu_import"]),
            _node("snaphu_export", "SnaphuExport", ["read", "x"]),
            _node("snaphu_import", "SnaphuImport", ["snaphu_export"]),
        ]
        with pytest.raises(GraphConfigError, match="reachable from both sides"):
            split_at_snaphu(nodes)

    def test_node_reachable_from_neither_half(self):
        nodes = [
            *LINEAR_WITH_SNAPHU,
            _node("orphan", "Read"),
        ]
        with pytest.raises(GraphConfigError, match="orphan"):
            split_at_snaphu(nodes)


CONF_TEXT = """\
# Configuration file for SNAPHU
#
# Recommended command line call to snaphu (assuming this file is in the
# working directory):
# snaphu -f snaphu.conf Phase_ifg_srd_02Apr2026_13Jan2024.snaphu.img 500

STATCOSTMODE  DEFO
INITMETHOD    MCF
"""


class TestParseCommand:
    def test_extracts_the_recommended_command(self, tmp_path: Path):
        conf = tmp_path / "snaphu.conf"
        conf.write_text(CONF_TEXT, encoding="utf-8")

        assert parse_command(conf) == [
            "-f",
            "snaphu.conf",
            "Phase_ifg_srd_02Apr2026_13Jan2024.snaphu.img",
            "500",
        ]

    def test_raises_when_no_command_line_found(self, tmp_path: Path):
        conf = tmp_path / "snaphu.conf"
        conf.write_text("STATCOSTMODE  DEFO\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="no 'snaphu -f snaphu.conf'"):
            parse_command(conf)
