"""Tests for the radar-snap command line, with execution mocked out."""

from __future__ import annotations

import pytest

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
        assert str(seen["config"]) == str(path)
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
