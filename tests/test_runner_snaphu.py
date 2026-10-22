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


@pytest.fixture
def snaphu_bin(tmp_path: Path) -> str:
    """A real executable, so the runner's preflight check passes.

    The binary is never actually run -- run_snaphu is monkeypatched -- it
    just has to survive check_binary.
    """
    path = tmp_path / "snaphu_stub"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _stage_export_output(target_folder: Path) -> None:
    """Simulate what a real graph-A execution would leave on disk."""
    (target_folder / "Phase_ifg.snaphu.hdr").write_text("x", encoding="utf-8")
    (target_folder / "snaphu.conf").write_text(
        "# snaphu -f snaphu.conf Phase_ifg.snaphu.img 64\n", encoding="utf-8"
    )


class TestThreePhaseOrchestration:
    def test_runs_graph_a_then_snaphu_then_graph_b_in_order(
        self,
        target_folder: Path,
        snaphu_bin: str,
        monkeypatch: pytest.MonkeyPatch,
        registry,
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
            (target_folder / "UnwPhase_ifg.snaphu.img").write_bytes(b"data")

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        result = run_graph(
            _config(target_folder),
            registry=registry,
            snaphu_bin=snaphu_bin,
            quiet=True,
        )

        assert calls == ["execute_xml", "run_snaphu", "execute_xml"]
        assert isinstance(result, SnaphuRunResult)
        assert "SnaphuExport" in result.xml_a
        assert "SnaphuImport" in result.xml_b

    def test_graph_b_reads_the_discovered_files(
        self,
        target_folder: Path,
        snaphu_bin: str,
        monkeypatch: pytest.MonkeyPatch,
        registry,
    ):
        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            if "SnaphuExport" in xml:
                _stage_export_output(target_folder)

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            (target_folder / "UnwPhase_ifg.snaphu.hdr").write_text(
                "x", encoding="utf-8"
            )
            (target_folder / "UnwPhase_ifg.snaphu.img").write_bytes(b"data")

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        result = run_graph(
            _config(target_folder),
            registry=registry,
            snaphu_bin=snaphu_bin,
            quiet=True,
        )

        assert isinstance(result, SnaphuRunResult)
        assert str(target_folder / "Phase_ifg.snaphu.hdr") in result.xml_b
        assert str(target_folder / "UnwPhase_ifg.snaphu.hdr") in result.xml_b

    def test_snaphu_failure_aborts_before_graph_b(
        self,
        target_folder: Path,
        snaphu_bin: str,
        monkeypatch: pytest.MonkeyPatch,
        registry,
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
            run_graph(
                _config(target_folder),
                registry=registry,
                snaphu_bin=snaphu_bin,
                quiet=True,
            )

        assert calls == ["execute_xml", "run_snaphu"]

    def test_unusable_snaphu_aborts_before_any_graph_runs(
        self,
        target_folder: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        registry,
    ):
        """The regression this change exists for.

        A snaphu without its exec bit used to surface only at the point of
        invocation -- i.e. after graph A's full coregistration had already
        run. The preflight must abort before any SNAP work happens.
        """
        binary = tmp_path / "snaphu"
        binary.write_bytes(b"\x7fELF")
        binary.chmod(0o644)

        calls: list[str] = []
        monkeypatch.setattr(
            runner, "execute_xml", lambda xml, *, quiet=False: calls.append(xml)
        )

        with pytest.raises(RuntimeError, match="not executable"):
            run_graph(
                _config(target_folder),
                registry=registry,
                snaphu_bin=str(binary),
                quiet=True,
            )

        assert calls == []

    def test_missing_corrfile_aborts_before_snaphu_runs(
        self,
        target_folder: Path,
        snaphu_bin: str,
        monkeypatch: pytest.MonkeyPatch,
        registry,
    ):
        """A CORRFILE that SnaphuExport named but never wrote must be caught
        before snaphu is launched -- otherwise it dies per-tile, opaquely."""
        calls: list[str] = []

        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            calls.append("execute_xml")
            _stage_export_output(target_folder)
            (target_folder / "snaphu.conf").write_text(
                "# snaphu -f snaphu.conf Phase_ifg.snaphu.img 64\n"
                "CORRFILE\t\tcoh_ifg.snaphu.img\n",
                encoding="utf-8",
            )

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            calls.append("run_snaphu")

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)

        with pytest.raises(RuntimeError, match="coh_ifg.snaphu.img"):
            run_graph(
                _config(target_folder),
                registry=registry,
                snaphu_bin=snaphu_bin,
                quiet=True,
            )

        assert "run_snaphu" not in calls

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

    def test_vars_reach_the_executed_graph(
        self, monkeypatch: pytest.MonkeyPatch, registry
    ):
        monkeypatch.setattr(runner, "execute_xml", lambda xml, *, quiet=False: None)

        config = {
            "pipeline": {
                "Read": {"file": "${vars.scene}"},
                "Write": {"file": "${vars.out}", "formatName": "BEAM-DIMAP"},
            }
        }
        xml = run_graph(
            config,
            registry=registry,
            quiet=True,
            vars={"scene": "/data/a.SAFE", "out": "/data/a_out.dim"},
        )

        assert isinstance(xml, str)
        assert "/data/a.SAFE" in xml
        assert "/data/a_out.dim" in xml


class TestSaveAndGraphDump:
    """Intermediates written to disk, and the executed graph XML beside them."""

    def _config(self, target_folder: Path, output_dir: Path, **saves) -> dict:
        pipeline = {
            "Read": {"file": "src.dim"},
            "GoldsteinPhaseFiltering": {},
            "SnaphuExport": {"targetFolder": str(target_folder)},
            "SnaphuImport": {"sources": "SnaphuExport"},
            "Terrain-Correction": {},
            "Write": {"file": "out.dim", "formatName": "BEAM-DIMAP"},
        }
        for node, value in saves.items():
            pipeline[node]["save"] = value
        return {"output_dir": str(output_dir), "pipeline": pipeline}

    def _run(self, config, target_folder, snaphu_bin, monkeypatch, registry):
        seen: list[str] = []

        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            seen.append(xml)
            if "SnaphuExport" in xml:
                _stage_export_output(target_folder)

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            (target_folder / "UnwPhase_ifg.snaphu.hdr").write_text("x")
            (target_folder / "UnwPhase_ifg.snaphu.img").write_bytes(b"data")

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)
        run_graph(config, registry=registry, snaphu_bin=snaphu_bin, quiet=True)
        return seen

    def test_a_save_on_a_graph_a_node_lands_in_graph_a(
        self, target_folder, snaphu_bin, tmp_path, monkeypatch, registry
    ):
        out = tmp_path / "out"
        out.mkdir()
        config = self._config(target_folder, out, GoldsteinPhaseFiltering=True)

        xml_a, xml_b = self._run(
            config, target_folder, snaphu_bin, monkeypatch, registry
        )

        assert "GoldsteinPhaseFiltering__save" in xml_a
        assert "GoldsteinPhaseFiltering__save" not in xml_b
        assert str(out / "GoldsteinPhaseFiltering.dim") in xml_a

    def test_a_save_on_a_graph_b_node_lands_in_graph_b(
        self, target_folder, snaphu_bin, tmp_path, monkeypatch, registry
    ):
        out = tmp_path / "out"
        out.mkdir()
        config = self._config(target_folder, out, **{"Terrain-Correction": "tc"})

        xml_a, xml_b = self._run(
            config, target_folder, snaphu_bin, monkeypatch, registry
        )

        assert "Terrain-Correction__save" in xml_b
        assert "Terrain-Correction__save" not in xml_a
        assert str(out / "tc.dim") in xml_b

    def test_graph_xml_exists_before_the_graph_is_executed(
        self, target_folder, snaphu_bin, tmp_path, monkeypatch, registry
    ):
        """The whole point: a run that dies must still leave the graph behind."""
        out = tmp_path / "out"
        out.mkdir()
        existed: list[bool] = []

        def fake_execute_xml(xml: str, *, quiet: bool = False) -> None:
            if "SnaphuExport" in xml:
                existed.append((out / "graph.a.xml").is_file())
                _stage_export_output(target_folder)
            else:
                existed.append((out / "graph.b.xml").is_file())

        def fake_run_snaphu(conf_path, *, snaphu_bin="snaphu", quiet=False):
            (target_folder / "UnwPhase_ifg.snaphu.hdr").write_text("x")
            (target_folder / "UnwPhase_ifg.snaphu.img").write_bytes(b"data")

        monkeypatch.setattr(runner, "execute_xml", fake_execute_xml)
        monkeypatch.setattr(runner.snaphu, "run_snaphu", fake_run_snaphu)
        run_graph(
            self._config(target_folder, out),
            registry=registry,
            snaphu_bin=snaphu_bin,
            quiet=True,
        )

        assert existed == [True, True]
        assert (out / "graph.a.xml").is_file()
        assert (out / "graph.b.xml").is_file()

    def test_without_output_dir_no_xml_is_written(
        self, target_folder, snaphu_bin, tmp_path, monkeypatch, registry
    ):
        out = tmp_path / "out"
        out.mkdir()
        config = self._config(target_folder, out)
        del config["output_dir"]

        self._run(config, target_folder, snaphu_bin, monkeypatch, registry)

        assert list(out.iterdir()) == []

    def test_non_split_config_writes_a_single_graph_xml(
        self, tmp_path, monkeypatch, registry
    ):
        out = tmp_path / "out"
        out.mkdir()
        monkeypatch.setattr(runner, "execute_xml", lambda xml, *, quiet=False: None)

        run_graph(
            {
                "output_dir": str(out),
                "pipeline": {
                    "Read": {"file": "src.dim"},
                    "Apply-Orbit-File": {"save": True},
                    "Write": {"file": "out.dim", "formatName": "BEAM-DIMAP"},
                },
            },
            registry=registry,
            quiet=True,
        )

        xml = (out / "graph.xml").read_text(encoding="utf-8")
        assert "Apply-Orbit-File__save" in xml
        assert str(out / "Apply-Orbit-File.dim") in xml
