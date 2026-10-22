"""Tests for the radar-snap command line, with execution mocked out."""

from __future__ import annotations

import pytest

from radar_snap_lib import config
from radar_snap_lib.snap_ops import cli

SEARCH_YAML = (
    "aoi: POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))\n"
    "platform: SENTINEL-1\n"
    "flight_direction: ASCENDING\n"
)

PIPELINE_YAML = (
    "pipeline:\n"
    "  Read:\n"
    "    file: in.zip\n"
    "  Write:\n"
    "    file: out.tif\n"
    "    formatName: GeoTIFF\n"
)


class TestValidateRouting:
    def test_search_config_validates(self, tmp_path, capsys):
        path = tmp_path / "s.yaml"
        path.write_text(SEARCH_YAML)
        assert cli.main(["validate", str(path)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_pipeline_config_validates(self, tmp_path, capsys):
        path = tmp_path / "p.yaml"
        path.write_text(PIPELINE_YAML)
        assert cli.main(["validate", str(path)]) == 0
        assert "OK" in capsys.readouterr().out

    def test_bad_search_config_fails(self, tmp_path, capsys):
        path = tmp_path / "s.yaml"
        path.write_text("platform: SENTINEL-1\nbogus: 1\n")
        assert cli.main(["validate", str(path)]) == 1
        assert "bogus" in capsys.readouterr().err

    def test_bad_snaphu_split_fails_without_a_jvm(self, tmp_path, capsys):
        path = tmp_path / "p.yaml"
        path.write_text(
            "pipeline:\n"
            "  Read:\n"
            "    file: in.zip\n"
            "  SnaphuExport:\n"
            "    targetFolder: /tmp/snaphu_work\n"
            "  SnaphuExport2:\n"
            "    op: SnaphuExport\n"
            "    sources: Read\n"
            "    targetFolder: /tmp/snaphu_work2\n"
        )
        assert cli.main(["validate", str(path)]) == 1
        assert "exactly one 'SnaphuExport'" in capsys.readouterr().err


class TestSearchCommand:
    def test_calls_search_and_reports_the_count(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "s.yaml"
        path.write_text(SEARCH_YAML)
        seen = {}

        def fake_search(config, **kwargs):
            seen["config"] = config
            return [{"fileName": "a.zip"}, {"fileName": "b.zip"}]

        monkeypatch.setattr("radar_snap_lib.search.search", fake_search)
        assert cli.main(["search", str(path)]) == 0
        assert seen["config"].source == str(path)
        assert "2" in capsys.readouterr().out

    def test_config_error_returns_one(self, tmp_path, capsys):
        path = tmp_path / "s.yaml"
        path.write_text("platform: SENTINEL-1\nbogus: 1\n")
        assert cli.main(["search", str(path)]) == 1
        assert "bogus" in capsys.readouterr().err


class TestDownloadCommand:
    def test_calls_download_and_lists_paths(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "s.yaml"
        path.write_text(SEARCH_YAML + f"dest: {tmp_path / 'scenes'}\n")
        monkeypatch.setattr(
            "radar_snap_lib.search.download",
            lambda config: [tmp_path / "scenes" / "a.zip"],
        )
        assert cli.main(["download", str(path)]) == 0
        assert "a.zip" in capsys.readouterr().out


class TestConfigLoadErrors:
    @pytest.mark.parametrize("command", ["search", "download", "validate"])
    def test_missing_config_returns_two(self, command, tmp_path, capsys):
        path = tmp_path / "nope.yaml"
        assert cli.main([command, str(path)]) == 2
        err = capsys.readouterr().err
        assert "not found" in err
        assert "Traceback" not in err

    @pytest.mark.parametrize("command", ["search", "download", "validate"])
    def test_malformed_yaml_returns_two(self, command, tmp_path, capsys):
        path = tmp_path / "bad.yaml"
        path.write_text("aoi: [unclosed\n")
        assert cli.main([command, str(path)]) == 2
        err = capsys.readouterr().err
        assert "Traceback" not in err


class TestDumpXmlSplitConfig:
    def test_dump_xml_on_split_config_writes_graph_a_only(self, tmp_path, capsys):
        target_folder = tmp_path / "snaphu_work"
        target_folder.mkdir()
        path = tmp_path / "config.yaml"
        path.write_text(
            "pipeline:\n"
            "  Read:\n"
            "    file: src.dim\n"
            "  SnaphuExport:\n"
            f"    targetFolder: {target_folder}\n"
            "  SnaphuImport:\n"
            "    sources: SnaphuExport\n"
            "  Write:\n"
            "    file: out.dim\n"
            "    formatName: BEAM-DIMAP\n"
        )

        assert cli.main(["dump-xml", str(path)]) == 0

        out = capsys.readouterr()
        assert "SnaphuExport" in out.out
        assert "SnaphuImport" not in out.out
        assert "graph B" in out.err
        assert "run --dump-xml" in out.err


class TestRunSnaphuBinResolution:
    def _run_and_capture(self, tmp_path, monkeypatch, args: list[str]) -> dict:
        path = tmp_path / "config.yaml"
        path.write_text(PIPELINE_YAML)
        seen: dict = {}

        def fake_run_graph(config, **kwargs):
            seen.update(kwargs)
            return "<graph/>"

        monkeypatch.setattr("radar_snap_lib.snap_ops.runner.run_graph", fake_run_graph)
        assert cli.main(["run", str(path), *args]) == 0
        return seen

    def test_defaults_to_plain_snaphu_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SNAPHU_BIN", raising=False)
        # Isolate from this project's own .env, which may itself set
        # SNAPHU_BIN -- same pattern as tests/test_config.py.
        monkeypatch.setattr(config, "_project_root", lambda: None)
        seen = self._run_and_capture(tmp_path, monkeypatch, [])
        assert seen["snaphu_bin"] == "snaphu"

    def test_falls_back_to_the_snaphu_bin_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SNAPHU_BIN", "/env/snaphu")
        seen = self._run_and_capture(tmp_path, monkeypatch, [])
        assert seen["snaphu_bin"] == "/env/snaphu"

    def test_cli_flag_overrides_the_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SNAPHU_BIN", "/env/snaphu")
        seen = self._run_and_capture(
            tmp_path, monkeypatch, ["--snaphu-bin", "/flag/snaphu"]
        )
        assert seen["snaphu_bin"] == "/flag/snaphu"


class TestValidateWarnsAboutSnaphu:
    """validate must stay usable without snaphu installed, so an unusable
    binary is a warning on stderr and never changes the exit code."""

    def _split_config(self, tmp_path) -> str:
        path = tmp_path / "split.yaml"
        path.write_text(
            "pipeline:\n"
            "  Read:\n"
            "    file: src.dim\n"
            "  SnaphuExport:\n"
            f"    targetFolder: {tmp_path}\n"
            "  SnaphuImport:\n"
            "    sources: SnaphuExport\n"
            "  Write:\n"
            "    file: out.dim\n"
            "    formatName: BEAM-DIMAP\n"
        )
        return str(path)

    def test_non_executable_binary_warns_but_exits_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        binary = tmp_path / "snaphu"
        binary.write_bytes(b"\x7fELF")
        binary.chmod(0o644)

        code = cli.main(
            ["validate", self._split_config(tmp_path), "--snaphu-bin", str(binary)]
        )

        assert code == 0
        out = capsys.readouterr()
        assert "OK" in out.out
        assert "warning:" in out.err
        assert f"chmod +x {binary}" in out.err

    def test_executable_binary_produces_no_warning(self, tmp_path, monkeypatch, capsys):
        binary = tmp_path / "snaphu"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)

        code = cli.main(
            ["validate", self._split_config(tmp_path), "--snaphu-bin", str(binary)]
        )

        assert code == 0
        assert "warning:" not in capsys.readouterr().err

    def test_a_config_without_snaphu_is_never_checked(
        self, tmp_path, monkeypatch, capsys
    ):
        """No SnaphuExport pair means no reason to care about the binary."""
        monkeypatch.setenv("SNAPHU_BIN", str(tmp_path / "does-not-exist"))
        path = tmp_path / "plain.yaml"
        path.write_text(PIPELINE_YAML)

        assert cli.main(["validate", str(path)]) == 0
        assert "warning:" not in capsys.readouterr().err


class TestDumpXmlIncludesSaves:
    """dump-xml must show the graph `run` would actually execute."""

    def _config(self, tmp_path, extra: str) -> str:
        path = tmp_path / "saved.yaml"
        path.write_text(
            f"output_dir: {tmp_path}\n"
            "pipeline:\n"
            "  Read:\n"
            "    file: in.zip\n"
            "  Apply-Orbit-File:\n"
            "    save: true\n"
            f"{extra}"
            "  Write:\n"
            "    file: out.tif\n"
            "    formatName: GeoTIFF\n"
        )
        return str(path)

    def test_save_node_appears_in_the_dumped_graph(self, tmp_path, capsys):
        assert cli.main(["dump-xml", self._config(tmp_path, "")]) == 0

        out = capsys.readouterr().out
        assert "Apply-Orbit-File__save" in out
        assert str(tmp_path / "Apply-Orbit-File.dim") in out

    def test_split_config_only_dumps_graph_a_saves(self, tmp_path, capsys):
        extra = (
            "  SnaphuExport:\n"
            f"    targetFolder: {tmp_path}\n"
            "  SnaphuImport:\n"
            "    sources: SnaphuExport\n"
            "  Terrain-Correction:\n"
            "    save: tc\n"
        )
        assert cli.main(["dump-xml", self._config(tmp_path, extra)]) == 0

        out = capsys.readouterr().out
        assert "Apply-Orbit-File__save" in out
        assert "Terrain-Correction__save" not in out


class TestNoFlags:
    @pytest.mark.parametrize("command", ["search", "download"])
    def test_flags_are_rejected(self, command, tmp_path):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([command, str(tmp_path / "s.yaml"), "--dest", "/data"])

    @pytest.mark.parametrize("command", ["search", "download"])
    def test_config_argument_is_required(self, command):
        parser = cli.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([command])
