"""Three-phase run_graph orchestration for SnaphuExport/SnaphuImport.

JVM-free: `execute_xml` and `radar_snap_lib.snap_ops.snaphu.run_snaphu` are
monkeypatched, so these tests exercise the orchestration logic (call order,
file discovery, node injection) without SNAP or a real snaphu binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from radar_snap_lib.snap_ops import runner
from radar_snap_lib.snap_ops.runner import SnaphuRunResult, run_graph


def _config(target_folder: Path) -> dict:
    return {
        "vars": {"target_folder": str(target_folder)},
        "pipeline": {
            "Read": {"file": "src.dim"},
            "GoldsteinPhaseFiltering": {},
            "SnaphuExport": {"targetFolder": "${vars.target_folder}"},
            "SnaphuImport": {"sources": "SnaphuExport"},
            "Terrain-Correction": {},
            "Write": {"file": "out.dim", "formatName": "BEAM-DIMAP"},
        },
    }


@pytest.fixture
def target_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "snaphu_work"
    folder.mkdir()
    return folder


def _stage_export_output(target_folder: Path) -> None:
    """Simulate what a real graph-A execution would leave on disk."""
    (target_folder / "Phase_ifg.dim").write_text("x", encoding="utf-8")
    (target_folder / "snaphu.conf").write_text(
        "# snaphu -f snaphu.conf Phase_ifg.snaphu.img 64\n", encoding="utf-8"
    )


class TestThreePhaseOrchestration:
    def test_runs_graph_a_then_snaphu_then_graph_b_in_order(
        self, target_folder: Path, monkeypatch: pytest.MonkeyPatch, registry
    ):
        calls: list[str] = []

        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            calls.append("execute_xml")
            if "SnaphuExport" in xml:
                _stage_export_output(target_folder)

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            calls.append("run_snaphu")
            assert conf_path == target_folder / "snaphu.conf"
            (target_folder / "UnwPhase_ifg.snaphu.hdr").write_text(
                "x", encoding="utf-8"
            )

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        result = run_graph(_config(target_folder), registry=registry, quiet=True)

        assert calls == ["execute_xml", "run_snaphu", "execute_xml"]
        assert isinstance(result, SnaphuRunResult)
        assert "SnaphuExport" in result.xml_a
        assert "SnaphuImport" in result.xml_b

    def test_graph_b_reads_the_discovered_files(
        self, target_folder: Path, monkeypatch: pytest.MonkeyPatch, registry
    ):
        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            if "SnaphuExport" in xml:
                _stage_export_output(target_folder)

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            (target_folder / "UnwPhase_ifg.snaphu.hdr").write_text(
                "x", encoding="utf-8"
            )

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        result = run_graph(_config(target_folder), registry=registry, quiet=True)

        assert isinstance(result, SnaphuRunResult)
        assert str(target_folder / "Phase_ifg.dim") in result.xml_b
        assert str(target_folder / "UnwPhase_ifg.snaphu.hdr") in result.xml_b

    def test_snaphu_failure_aborts_before_graph_b(
        self, target_folder: Path, monkeypatch: pytest.MonkeyPatch, registry
    ):
        calls: list[str] = []

        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            calls.append("execute_xml")
            _stage_export_output(target_folder)

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            calls.append("run_snaphu")
            raise RuntimeError("snaphu exited with code 1")

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        with pytest.raises(RuntimeError, match="snaphu exited with code 1"):
            run_graph(_config(target_folder), registry=registry, quiet=True)

        assert calls == ["execute_xml", "run_snaphu"]

    def test_non_split_config_is_unaffected(
        self, monkeypatch: pytest.MonkeyPatch, registry
    ):
        calls: list[str] = []
        monkeypatch.setattr(
            runner, "execute_xml", lambda xml, *, quiet=False: calls.append(xml)
        )

        config = {
            "pipeline": {
                "Read": {"file": "src.dim"},
                "Write": {"file": "out.dim", "formatName": "BEAM-DIMAP"},
            }
        }
        result = run_graph(config, registry=registry, quiet=True)

        assert calls == [result]
        assert isinstance(result, str)
