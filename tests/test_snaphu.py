"""Tests for the SnaphuExport/SnaphuImport split detection."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from radar_snap_lib.snap_ops.OpsConfig import GraphConfigError, ParsedNode
from radar_snap_lib.snap_ops.snaphu import (
    find_conf,
    find_exported_product,
    find_unwrapped_product,
    parse_command,
    run_snaphu,
    split_at_snaphu,
)


def _node(
    id_: str,
    op: str,
    sources: list[str] | None = None,
    params: dict | None = None,
) -> ParsedNode:
    return ParsedNode(id=id_, op=op, sources=sources or [], params=params or {})


LINEAR_WITH_SNAPHU = [
    _node("read", "Read"),
    _node("goldstein", "GoldsteinPhaseFiltering", ["read"]),
    _node(
        "snaphu_export",
        "SnaphuExport",
        ["goldstein"],
        params={"targetFolder": "/tmp/snaphu_work"},
    ),
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
            _node(
                "snaphu_export",
                "SnaphuExport",
                ["read"],
                params={"targetFolder": "/tmp/snaphu_work"},
            ),
            _node("snaphu_import", "SnaphuImport", ["read"]),
        ]
        with pytest.raises(GraphConfigError, match="must list 'snaphu_export'"):
            split_at_snaphu(nodes)

    def test_import_has_extra_sources(self):
        nodes = [
            _node("read", "Read"),
            _node(
                "snaphu_export",
                "SnaphuExport",
                ["read"],
                params={"targetFolder": "/tmp/snaphu_work"},
            ),
            _node("snaphu_import", "SnaphuImport", ["snaphu_export", "read"]),
        ]
        with pytest.raises(GraphConfigError, match="only 'snaphu_export'"):
            split_at_snaphu(nodes)

    def test_node_referenced_from_both_halves(self):
        nodes = [
            _node("read", "Read"),
            _node(
                "snaphu_export",
                "SnaphuExport",
                ["read"],
                params={"targetFolder": "/tmp/snaphu_work"},
            ),
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
            _node(
                "snaphu_export",
                "SnaphuExport",
                ["read", "x"],
                params={"targetFolder": "/tmp/snaphu_work"},
            ),
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

    def test_missing_target_folder_on_export_node(self):
        nodes = [
            _node("read", "Read"),
            _node("snaphu_export", "SnaphuExport", ["read"]),
            _node("snaphu_import", "SnaphuImport", ["snaphu_export"]),
        ]
        with pytest.raises(GraphConfigError, match="requires a 'targetFolder'"):
            split_at_snaphu(nodes)

    def test_empty_target_folder_on_export_node(self):
        nodes = [
            _node("read", "Read"),
            _node(
                "snaphu_export",
                "SnaphuExport",
                ["read"],
                params={"targetFolder": ""},
            ),
            _node("snaphu_import", "SnaphuImport", ["snaphu_export"]),
        ]
        with pytest.raises(GraphConfigError, match="requires a 'targetFolder'"):
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


class TestFindConf:
    def test_finds_the_single_conf_file(self, tmp_path: Path):
        (tmp_path / "snaphu.conf").write_text("x", encoding="utf-8")
        assert find_conf(tmp_path) == tmp_path / "snaphu.conf"

    def test_finds_the_conf_file_nested_in_a_product_subdirectory(self, tmp_path: Path):
        # Real SnaphuExport output: <targetFolder>/<productName>/snaphu.conf,
        # not directly in <targetFolder> -- confirmed against a real SNAP run.
        product_dir = tmp_path / "S1A_IW_SLC__1SDV_..._Orb_Stack_Ifg_Deb_ML_Flt"
        product_dir.mkdir()
        (product_dir / "snaphu.conf").write_text("x", encoding="utf-8")
        assert find_conf(tmp_path) == product_dir / "snaphu.conf"

    def test_raises_when_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="No snaphu.conf"):
            find_conf(tmp_path)


class TestFindExportedProduct:
    def test_finds_the_single_exported_header(self, tmp_path: Path):
        # Real SnaphuExport output is ENVI format (.hdr/.img pair), not
        # BEAM-DIMAP (.dim) -- confirmed against a real SNAP run.
        (tmp_path / "Phase_ifg_IW3_VV_02Apr2026_08Apr2026.snaphu.hdr").write_text(
            "x", encoding="utf-8"
        )
        assert (
            find_exported_product(tmp_path)
            == tmp_path / "Phase_ifg_IW3_VV_02Apr2026_08Apr2026.snaphu.hdr"
        )

    def test_finds_the_exported_header_nested_in_a_product_subdirectory(
        self, tmp_path: Path
    ):
        product_dir = tmp_path / "S1A_IW_SLC__1SDV_..._Orb_Stack_Ifg_Deb_ML_Flt"
        product_dir.mkdir()
        (product_dir / "Phase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
        assert find_exported_product(tmp_path) == product_dir / "Phase_ifg.snaphu.hdr"

    def test_raises_when_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="No exported"):
            find_exported_product(tmp_path)

    def test_raises_when_ambiguous(self, tmp_path: Path):
        (tmp_path / "Phase_a.snaphu.hdr").write_text("x", encoding="utf-8")
        (tmp_path / "Phase_b.snaphu.hdr").write_text("x", encoding="utf-8")
        with pytest.raises(RuntimeError, match="Multiple exported"):
            find_exported_product(tmp_path)

    def test_does_not_match_the_unwrapped_output_stub(self, tmp_path: Path):
        # SNAP pre-writes the UnwPhase_*.snaphu.hdr stub (see
        # TestFindUnwrappedProduct) before snaphu ever runs, so both files
        # coexist in the same directory when find_exported_product is
        # called -- confirmed against a real SNAP run.
        (tmp_path / "Phase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
        (tmp_path / "UnwPhase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
        assert find_exported_product(tmp_path) == tmp_path / "Phase_ifg.snaphu.hdr"


class TestFindUnwrappedProduct:
    def test_finds_the_single_unwrapped_header(self, tmp_path: Path):
        (tmp_path / "UnwPhase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
        (tmp_path / "UnwPhase_ifg.snaphu.img").write_bytes(b"data")
        assert find_unwrapped_product(tmp_path) == tmp_path / "UnwPhase_ifg.snaphu.hdr"

    def test_finds_the_unwrapped_header_nested_in_a_product_subdirectory(
        self, tmp_path: Path
    ):
        product_dir = tmp_path / "S1A_IW_SLC__1SDV_..._Orb_Stack_Ifg_Deb_ML_Flt"
        product_dir.mkdir()
        (product_dir / "UnwPhase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
        (product_dir / "UnwPhase_ifg.snaphu.img").write_bytes(b"data")
        assert (
            find_unwrapped_product(tmp_path) == product_dir / "UnwPhase_ifg.snaphu.hdr"
        )

    def test_raises_when_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="No unwrapped"):
            find_unwrapped_product(tmp_path)

    def test_raises_when_only_the_pre_written_stub_header_exists(
        self, tmp_path: Path
    ):
        # SnaphuExport pre-writes the .hdr stub during graph A, before
        # snaphu ever runs -- confirmed against a real SNAP run. If the
        # sibling .img (snaphu's actual output) never showed up, snaphu
        # silently failed to produce anything despite exiting zero, and
        # graph B must not proceed to read a headerless product.
        (tmp_path / "UnwPhase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
        with pytest.raises(RuntimeError, match="snaphu did not write"):
            find_unwrapped_product(tmp_path)

    def test_raises_when_the_img_exists_but_is_empty(self, tmp_path: Path):
        (tmp_path / "UnwPhase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
        (tmp_path / "UnwPhase_ifg.snaphu.img").write_bytes(b"")
        with pytest.raises(RuntimeError, match="snaphu did not write"):
            find_unwrapped_product(tmp_path)


class TestRunSnaphu:
    def test_invokes_the_parsed_command_in_the_conf_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        conf = tmp_path / "snaphu.conf"
        conf.write_text(CONF_TEXT, encoding="utf-8")

        calls: list[dict] = []

        def fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, "kwargs": kwargs})
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)

        run_snaphu(conf, snaphu_bin="snaphu")

        assert len(calls) == 1
        assert calls[0]["cmd"] == [
            "snaphu",
            "-f",
            "snaphu.conf",
            "Phase_ifg_srd_02Apr2026_13Jan2024.snaphu.img",
            "500",
        ]
        assert calls[0]["kwargs"]["cwd"] == tmp_path

    def test_missing_binary_raises_a_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        conf = tmp_path / "snaphu.conf"
        conf.write_text(CONF_TEXT, encoding="utf-8")

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="snaphu binary not found"):
            run_snaphu(conf, snaphu_bin="snaphu")

    def test_nonzero_exit_raises_with_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        conf = tmp_path / "snaphu.conf"
        conf.write_text(CONF_TEXT, encoding="utf-8")

        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, output=b"", stderr=b"bad tile geometry"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="bad tile geometry"):
            run_snaphu(conf, snaphu_bin="snaphu")


class TestInterferogramExampleSplit:
    def test_the_example_config_splits_at_snaphu(self, registry):
        from radar_snap_lib.snap_ops.OpsConfig import OpsConfig

        EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
        path = EXAMPLES / "s1_slc_interferogram.yaml"
        nodes = OpsConfig.load(path, registry=registry).parse()

        split = split_at_snaphu(nodes)

        assert split is not None
        assert split.export_node.op == "SnaphuExport"
        assert split.import_node.op == "SnaphuImport"
        assert [n.id for n in split.graph_b][-1] == "Write"
