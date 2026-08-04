"""Tests for search and download execution, with ASF mocked out."""

from __future__ import annotations

import asf_search as asf
import pytest

from radar_snap_lib.search import runner as search_module
from radar_snap_lib.search.SearchConfig import SearchConfigError

BASE = {
    "aoi": "POLYGON((10 50, 11 50, 11 51, 10 51, 10 50))",
    "platform": "SENTINEL-1",
}


class FakeProduct:
    """Minimal stand-in for an ASFProduct: enough for both the file-name
    lookup in download() and the geojson writer in write_results()."""

    def __init__(self, name: str) -> None:
        self.properties = {"fileName": name}

    def geojson(self) -> dict:
        return {
            "type": "Feature",
            "geometry": None,
            "properties": dict(self.properties),
        }


class FakeResults(asf.ASFSearchResults):
    """A real ASFSearchResults (so the geojson writer works) that records
    download calls instead of making them."""

    def __init__(self, items=()):
        super().__init__(items)
        self.download_calls = []

    def download(self, path, session=None, processes=1, **kwargs):
        self.download_calls.append(
            {"path": path, "session": session, "processes": processes}
        )


@pytest.fixture
def captured(monkeypatch):
    """Capture the options handed to asf.search and return canned results."""
    calls = {}
    results = FakeResults([FakeProduct("S1A_scene.zip")])

    def fake_search(**kwargs):
        calls.update(kwargs)
        return results

    monkeypatch.setattr(search_module.asf, "search", fake_search)
    return calls, results


class TestSearch:
    def test_options_reach_asf(self, captured):
        calls, _ = captured
        search_module.search({**BASE, "flight_direction": "ASCENDING"})
        assert calls["platform"] == "SENTINEL-1"
        assert calls["flightDirection"] == "ASCENDING"
        assert "POLYGON" in calls["intersectsWith"]

    def test_reserved_keys_do_not_reach_asf(self, captured):
        calls, _ = captured
        search_module.search({**BASE, "dest": "/data", "processes": 4})
        assert "dest" not in calls
        assert "processes" not in calls

    def test_results_are_returned(self, captured):
        _, results = captured
        assert search_module.search(BASE) is results

    def test_invalid_config_never_reaches_the_network(self, monkeypatch):
        def explode(**kwargs):
            raise AssertionError("asf.search must not be called")

        monkeypatch.setattr(search_module.asf, "search", explode)
        with pytest.raises(SearchConfigError):
            search_module.search({**BASE, "bogus": 1})

    def test_output_file_is_written(self, captured, tmp_path):
        target = tmp_path / "out" / "results.geojson"
        search_module.search({**BASE, "output": str(target)})
        assert target.is_file()
        content = target.read_text()
        assert "FeatureCollection" in content
        assert "S1A_scene.zip" in content

    def test_write_output_false_skips_the_file(self, captured, tmp_path):
        target = tmp_path / "results.geojson"
        search_module.search({**BASE, "output": str(target)}, write_output=False)
        assert not target.exists()


class TestDownload:
    def test_downloads_to_dest(self, captured, monkeypatch, tmp_path):
        _, results = captured
        monkeypatch.setattr(
            search_module, "earthdata_session", lambda: "session-object"
        )
        dest = tmp_path / "scenes"
        paths = search_module.download({**BASE, "dest": str(dest), "processes": 3})

        assert dest.is_dir()
        assert paths == [dest / "S1A_scene.zip"]
        assert results.download_calls == [
            {"path": str(dest), "session": "session-object", "processes": 3}
        ]

    def test_missing_dest_is_a_config_error(self, captured):
        with pytest.raises(SearchConfigError, match="dest"):
            search_module.download(BASE)


class TestSession:
    def test_token_credentials(self, monkeypatch):
        from radar_snap_lib.config import EarthdataCredentials

        monkeypatch.setattr(
            search_module,
            "earthdata_credentials",
            lambda: EarthdataCredentials(token="tok"),
        )
        calls = {}

        class FakeSession:
            def auth_with_token(self, token):
                calls["token"] = token
                return self

            def auth_with_creds(self, username, password):
                raise AssertionError("token should win")

        monkeypatch.setattr(search_module.asf, "ASFSession", FakeSession)
        search_module.earthdata_session()
        assert calls == {"token": "tok"}

    def test_username_password_credentials(self, monkeypatch):
        from radar_snap_lib.config import EarthdataCredentials

        monkeypatch.setattr(
            search_module,
            "earthdata_credentials",
            lambda: EarthdataCredentials(username="u", password="p"),
        )
        calls = {}

        class FakeSession:
            def auth_with_creds(self, username, password):
                calls["creds"] = (username, password)
                return self

        monkeypatch.setattr(search_module.asf, "ASFSession", FakeSession)
        search_module.earthdata_session()
        assert calls == {"creds": ("u", "p")}

    def test_no_credentials_raises(self, monkeypatch):
        monkeypatch.setattr(search_module, "earthdata_credentials", lambda: None)
        with pytest.raises(RuntimeError, match="EARTHDATA_TOKEN"):
            search_module.earthdata_session()
