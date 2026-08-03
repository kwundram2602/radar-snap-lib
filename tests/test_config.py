"""Tests for external-environment resolution and credential lookup."""

from __future__ import annotations

import sys

from radar_snap_lib import config


class TestSnappyPathOrder:
    def test_site_packages_is_appended_not_prepended(self, monkeypatch, tmp_path):
        site_packages = tmp_path / "lib" / "python3.13" / "site-packages"
        site_packages.mkdir(parents=True)
        monkeypatch.setattr(config, "snappy_site_packages", lambda: site_packages)
        monkeypatch.setattr(sys, "path", ["/project/first", "/project/second"])

        config.ensure_esa_snappy()

        assert sys.path[-1] == str(site_packages)
        assert sys.path[0] == "/project/first"

    def test_is_idempotent(self, monkeypatch, tmp_path):
        site_packages = tmp_path / "lib" / "python3.13" / "site-packages"
        site_packages.mkdir(parents=True)
        monkeypatch.setattr(config, "snappy_site_packages", lambda: site_packages)
        monkeypatch.setattr(sys, "path", ["/project/first"])

        config.ensure_esa_snappy()
        config.ensure_esa_snappy()

        assert sys.path.count(str(site_packages)) == 1


class TestEnvValue:
    def test_environment_wins(self, monkeypatch):
        monkeypatch.setenv("EARTHDATA_TOKEN", "from-env")
        assert config.env_value("EARTHDATA_TOKEN") == "from-env"

    def test_falls_back_to_dotenv(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        (tmp_path / ".env").write_text(
            "# a comment\nexport EARTHDATA_TOKEN='from-dotenv'\n"
        )
        monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
        assert config.env_value("EARTHDATA_TOKEN") == "from-dotenv"

    def test_missing_key_is_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
        assert config.env_value("EARTHDATA_TOKEN") is None


class TestEarthdataCredentials:
    def test_token_is_preferred(self, monkeypatch):
        monkeypatch.setenv("EARTHDATA_TOKEN", "tok")
        monkeypatch.setenv("EARTHDATA_USERNAME", "user")
        monkeypatch.setenv("EARTHDATA_PASSWORD", "pass")
        creds = config.earthdata_credentials()
        assert creds is not None
        assert creds.token == "tok"

    def test_username_and_password(self, monkeypatch):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        monkeypatch.setenv("EARTHDATA_USERNAME", "user")
        monkeypatch.setenv("EARTHDATA_PASSWORD", "pass")
        creds = config.earthdata_credentials()
        assert creds is not None
        assert (creds.username, creds.password) == ("user", "pass")
        assert creds.token is None

    def test_nothing_configured_is_none(self, monkeypatch, tmp_path):
        for name in (
            "EARTHDATA_TOKEN",
            "EARTHDATA_USERNAME",
            "EARTHDATA_PASSWORD",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
        assert config.earthdata_credentials() is None

    def test_username_without_password_is_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
        monkeypatch.setenv("EARTHDATA_USERNAME", "user")
        monkeypatch.delenv("EARTHDATA_PASSWORD", raising=False)
        monkeypatch.setattr(config, "_project_root", lambda: tmp_path)
        assert config.earthdata_credentials() is None
