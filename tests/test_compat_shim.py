"""The pre-2.0 ``daemon.py`` shim must keep working."""

from __future__ import annotations

import daemon
import looper


def test_shim_reexports_public_api():
    for name in daemon.__all__:
        assert hasattr(daemon, name), f"daemon.{name} missing"


def test_shim_main_is_the_cli_main():
    from looper.cli import main

    assert daemon.main is main


def test_shim_shares_identity_with_package():
    assert daemon.LooperDaemon is looper.LooperDaemon
    assert daemon.ScoringEngine is looper.ScoringEngine
    assert daemon.StateManager is looper.StateManager


def test_shim_version_matches():
    assert daemon.__version__ == looper.__version__


def test_legacy_helpers_still_callable():
    assert daemon.parse_test_summary("2 passed") == (2, 0)
    assert daemon.parse_security_findings("- HIGH: x") == ["HIGH: x"]


def test_package_exports_are_importable():
    for name in looper.__all__:
        assert hasattr(looper, name), f"looper.{name} missing"
