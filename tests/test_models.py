"""Proof tests for model-slug verification.

A wrong slug is well-formed YAML, so ``--check-config`` cannot catch it. It
fails mid-build instead, after earlier phases have already been billed. These
tests pin the two behaviours that matter: a bad slug fails the check, and an
unreachable catalogue does *not* (an offline laptop is not a broken config).

No test here touches the network; the catalogue fetch is always injected.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from looper.cli import EXIT_CONFIG_ERROR, EXIT_OK, main, run_check_models
from looper.config import DEFAULT_AGENTS, DEFAULT_MODEL_PRICES_USD_PER_1K, build_config
from looper.models import (
    CatalogueUnavailableError,
    ModelCheck,
    check_models,
    fetch_catalogue,
)


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _patch_urlopen(monkeypatch, result):
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)

    monkeypatch.setattr("looper.models.urllib.request.urlopen", fake_urlopen)
    return seen


# -- fetch_catalogue -----------------------------------------------------


def test_fetch_hits_the_models_endpoint(monkeypatch):
    seen = _patch_urlopen(monkeypatch, {"data": [{"id": "a/b"}, {"id": "c/d"}]})
    ids = fetch_catalogue("https://openrouter.ai/api/v1")
    assert ids == frozenset({"a/b", "c/d"})
    assert seen["url"] == "https://openrouter.ai/api/v1/models"


def test_fetch_tolerates_a_trailing_slash(monkeypatch):
    seen = _patch_urlopen(monkeypatch, {"data": [{"id": "a/b"}]})
    fetch_catalogue("https://openrouter.ai/api/v1/")
    assert seen["url"] == "https://openrouter.ai/api/v1/models"


def test_fetch_skips_malformed_entries(monkeypatch):
    _patch_urlopen(monkeypatch, {"data": [{"id": "a/b"}, {"no_id": 1}, "junk"]})
    assert fetch_catalogue("https://x.test/v1") == frozenset({"a/b"})


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("no route to host"),
        TimeoutError("timed out"),
        OSError("connection reset"),
    ],
)
def test_network_failures_become_catalogue_unavailable(monkeypatch, failure):
    _patch_urlopen(monkeypatch, failure)
    with pytest.raises(CatalogueUnavailableError) as err:
        fetch_catalogue("https://x.test/v1")
    assert "could not reach" in str(err.value)


def test_invalid_json_is_reported_as_unavailable(monkeypatch):
    class Broken(_FakeResponse):
        def read(self) -> bytes:
            return b"not json"

    monkeypatch.setattr(
        "looper.models.urllib.request.urlopen", lambda request, timeout=None: Broken({})
    )
    with pytest.raises(CatalogueUnavailableError) as err:
        fetch_catalogue("https://x.test/v1")
    assert "invalid JSON" in str(err.value)


def test_missing_data_list_is_unavailable(monkeypatch):
    _patch_urlopen(monkeypatch, {"error": "nope"})
    with pytest.raises(CatalogueUnavailableError) as err:
        fetch_catalogue("https://x.test/v1")
    assert "no model list" in str(err.value)


def test_empty_catalogue_is_unavailable_not_all_slugs_bad(monkeypatch):
    """Guard: an empty list must not read as 'every model you use is gone'."""
    _patch_urlopen(monkeypatch, {"data": []})
    with pytest.raises(CatalogueUnavailableError) as err:
        fetch_catalogue("https://x.test/v1")
    assert "empty model list" in str(err.value)


# -- check_models --------------------------------------------------------


def test_check_flags_only_unknown_slugs():
    results = check_models(
        {"builder": "good/model", "fixer": "bad/model"}, frozenset({"good/model"})
    )
    assert results == [
        ModelCheck(agent="builder", model="good/model", known=True),
        ModelCheck(agent="fixer", model="bad/model", known=False),
    ]


def test_check_output_is_sorted_for_determinism():
    results = check_models({"z": "m", "a": "m"}, frozenset({"m"}))
    assert [r.agent for r in results] == ["a", "z"]


# -- CLI wiring ----------------------------------------------------------


def test_cli_passes_when_every_slug_is_known(config, monkeypatch, caplog):
    known = frozenset(spec.model for spec in config.agents.values())
    monkeypatch.setattr("looper.cli.fetch_catalogue", lambda base_url: known)
    with caplog.at_level("INFO"):
        assert run_check_models(config) == EXIT_OK
    assert "All 10 model slugs verified" in caplog.text


def test_cli_fails_with_config_exit_code_on_a_bad_slug(config, monkeypatch, caplog):
    monkeypatch.setattr("looper.cli.fetch_catalogue", lambda base_url: frozenset({"only/this"}))
    with caplog.at_level("ERROR"):
        assert run_check_models(config) == EXIT_CONFIG_ERROR
    assert "not served by OpenRouter" in caplog.text


def test_offline_does_not_fail_the_check(config, monkeypatch, caplog):
    """A flaky network must not be reported as a broken configuration."""

    def boom(base_url):
        raise CatalogueUnavailableError("could not reach https://x.test/v1: offline")

    monkeypatch.setattr("looper.cli.fetch_catalogue", boom)
    with caplog.at_level("WARNING"):
        assert run_check_models(config) == EXIT_OK
    assert "Could not verify models" in caplog.text


def test_check_models_is_reachable_from_main(tmp_path, raw_config, monkeypatch):
    import yaml

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
    monkeypatch.setattr(
        "looper.cli.fetch_catalogue",
        lambda base_url: frozenset(spec.model for spec in DEFAULT_AGENTS.values()),
    )
    assert main(["--config", str(cfg_path), "--check-models"]) == EXIT_OK


# -- roster and pricing consistency --------------------------------------


def test_roster_is_still_ten_agents():
    assert len(DEFAULT_AGENTS) == 10


def test_every_default_model_has_a_price():
    """An unpriced model silently falls back to the $0.002/1K guess, which
    would under-report Opus spend ~7x and make max_cost_usd a budget in name
    only (ADR-005)."""
    unpriced = sorted(
        {spec.model for spec in DEFAULT_AGENTS.values()} - set(DEFAULT_MODEL_PRICES_USD_PER_1K)
    )
    assert unpriced == []


def test_reviewer_and_tester_differ_in_family_from_the_builder():
    """Independence of verification: the code's author must not also be its
    only reviewer and test designer (ADR-006)."""
    family = lambda slug: slug.split("/")[0]  # noqa: E731
    builder = family(DEFAULT_AGENTS["builder"].model)
    assert family(DEFAULT_AGENTS["reviewer"].model) != builder
    assert family(DEFAULT_AGENTS["security_auditor"].model) != builder


def test_user_prices_override_defaults_without_dropping_them(raw_config):
    cfg = build_config(
        {
            **raw_config,
            "execution": {"model_prices_usd_per_1k": {"anthropic/claude-opus-5": 0.99}},
        },
        env={},
    )
    prices = cfg.execution.model_prices_usd_per_1k
    assert prices["anthropic/claude-opus-5"] == 0.99
    # A user overriding one price must not lose the rest of the table.
    assert prices["anthropic/claude-sonnet-5"] == 0.006
