"""Shared fixtures.

Most tests run against the committed ``operators.json``, which needs no JVM.
Tests marked ``snap`` need a real SNAP install and are skipped without one.
"""

from __future__ import annotations

import pytest

from radar_snap_lib.snap_ops.registry import load_registry


def _snap_available() -> bool:
    try:
        from radar_snap_lib.config import snappy_site_packages

        snappy_site_packages()
    except Exception:
        return False
    return True


SNAP_AVAILABLE = _snap_available()


def pytest_runtest_setup(item: pytest.Item) -> None:
    if list(item.iter_markers(name="snap")) and not SNAP_AVAILABLE:
        pytest.skip("no esa_snappy environment configured")


@pytest.fixture(scope="session")
def registry():
    """The committed operator registry."""
    return load_registry()
