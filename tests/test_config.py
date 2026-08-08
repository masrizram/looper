"""Configuration loading, validation, and immutability."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from looper.config import (
    ALL_INTERFACES,
    DEFAULT_AGENTS,
    AgentSpec,
    ConfigError,
    ExecutionConfig,
    HTTPConfig,
    LooperConfig,
    OpenRouterConfig,
    RetryPolicy,
    ScoringWeights,
    build_config,
    load_config,
)

# --- File loading -----------------------------------------------------------


def test_load_config_reads_config_yaml(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("workspace: ./w\nhttp_port: 8765\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(env={})
    assert cfg.http.port == 8765
    assert cfg.workspace == Path("./w")


def test_load_config_falls_back_to_looper_config_yaml(tmp_path, monkeypatch):
    (tmp_path / "looper_config.yaml").write_text("http_port: 8766\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_config(env={}).http.port == 8766


def test_load_config_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="No config file found"):
        load_config(env={})


def test_load_config_explicit_path_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config(tmp_path / "nope.yaml", env={})


def test_load_config_rejects_malformed_yaml(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("key: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(bad, env={})


def test_load_config_explicit_path(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(yaml.safe_dump({"workspace": "./ws"}), encoding="utf-8")
    assert load_config(path, env={}).workspace == Path("./ws")


def test_empty_yaml_yields_defaults(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(env={})
    assert cfg.execution.max_cycles == 5


# --- Import purity (ADR-001) ------------------------------------------------


def test_importing_package_has_no_side_effects(tmp_path, monkeypatch):
    """Regression: the old module called configure() at import time, so
    `import daemon` crashed anywhere without a config.yaml in the CWD."""
    monkeypatch.chdir(tmp_path)
    import importlib

    import daemon
    import looper

    importlib.reload(looper)
    importlib.reload(daemon)
    assert looper.__version__
    assert callable(daemon.main)


# --- build_config validation ------------------------------------------------


def test_build_config_none_yields_defaults():
    cfg = build_config(None, env={})
    assert isinstance(cfg, LooperConfig)
    assert cfg.http.port == 9999


def test_build_config_rejects_non_mapping():
    with pytest.raises(ConfigError, match="must be a mapping"):
        build_config(["not", "a", "map"], env={})


def test_build_config_rejects_non_mapping_section():
    with pytest.raises(ConfigError, match="section 'http'"):
        build_config({"http": ["nope"]}, env={})


def test_build_config_rejects_non_mapping_severity():
    with pytest.raises(ConfigError, match="scoring.severity"):
        build_config({"scoring": {"severity": [1]}}, env={})


def test_build_config_rejects_non_mapping_agent():
    with pytest.raises(ConfigError, match="agents.builder"):
        build_config({"agents": {"builder": "claude"}}, env={})


def test_build_config_rejects_unknown_agent_key():
    with pytest.raises(ConfigError, match="unknown agent key"):
        build_config({"agents": {"wizard": {"model": "m"}}}, env={})


def test_agent_override_is_applied():
    cfg = build_config({"agents": {"builder": {"model": "custom/model"}}}, env={})
    assert cfg.agents["builder"].model == "custom/model"
    # Untouched fields keep their defaults.
    assert cfg.agents["builder"].role == DEFAULT_AGENTS["builder"].role


def test_env_supplies_secrets_not_the_file():
    cfg = build_config({}, env={"OPENROUTER_API_KEY": "sk-or-test"})
    assert cfg.openrouter.api_key == "sk-or-test"


def test_legacy_top_level_http_port_still_honoured():
    assert build_config({"http_port": 7777}, env={}).http.port == 7777


def test_http_section_wins_over_legacy_port():
    cfg = build_config({"http_port": 7777, "http": {"port": 8888}}, env={})
    assert cfg.http.port == 8888


# --- HTTPConfig -------------------------------------------------------------


@pytest.mark.parametrize("port", [0, 65536, "8080", 3.5, True])
def test_http_rejects_bad_port(port):
    with pytest.raises(ConfigError, match="http.port"):
        HTTPConfig(port=port)


def test_http_rejects_empty_bind():
    with pytest.raises(ConfigError, match="http.bind"):
        HTTPConfig(bind="")


def test_bind_all_interfaces_without_token_is_refused():
    """C-4: /build triggers arbitrary LLM-driven code execution."""
    with pytest.raises(ConfigError, match="Refusing to bind"):
        HTTPConfig(bind=ALL_INTERFACES, auth_token="")


def test_bind_all_interfaces_with_token_allowed(caplog):
    cfg = HTTPConfig(bind=ALL_INTERFACES, auth_token="s3cret")
    assert cfg.is_public is True
    assert "reachable from the network" in caplog.text


def test_lan_bind_without_token_is_refused():
    # Regression: a LAN address is just as reachable as 0.0.0.0. The old code
    # only logged "Unusual http.bind" and started the RCE-capable API anyway.
    with pytest.raises(ConfigError, match="Refusing to bind non-loopback"):
        HTTPConfig(bind="192.168.1.5")


def test_ipv6_wildcard_bind_without_token_is_refused():
    with pytest.raises(ConfigError, match="Refusing to bind non-loopback"):
        HTTPConfig(bind="::")


def test_lan_bind_with_token_warns_and_is_public(caplog):
    caplog.set_level(logging.WARNING, logger="looper.config")
    cfg = HTTPConfig(bind="192.168.1.5", auth_token="t0ken")
    assert cfg.is_public is True
    assert "reachable from the network" in caplog.text


def test_loopback_is_not_public():
    assert HTTPConfig(bind="127.0.0.1").is_public is False
    assert HTTPConfig(bind="::1").is_public is False


def test_build_config_propagates_bind_refusal():
    with pytest.raises(ConfigError, match="Refusing to bind"):
        build_config({"http": {"bind": ALL_INTERFACES}}, env={})


def test_build_config_bind_all_with_env_token():
    cfg = build_config({"http": {"bind": ALL_INTERFACES}}, env={"LOOPER_HTTP_TOKEN": "tok"})
    assert cfg.http.auth_token == "tok"


# --- ExecutionConfig --------------------------------------------------------


@pytest.mark.parametrize("cycles", [0, -1, "5", 1.5])
def test_execution_rejects_bad_max_cycles(cycles):
    with pytest.raises(ConfigError, match="max_cycles"):
        ExecutionConfig(max_cycles=cycles)


def test_execution_rejects_min_above_target():
    with pytest.raises(ConfigError, match="min_acceptable must be <="):
        ExecutionConfig(min_acceptable=99, target_score=50)


def test_execution_rejects_bad_timeout():
    with pytest.raises(ConfigError, match="test_timeout_seconds"):
        ExecutionConfig(test_timeout_seconds=0)


def test_execution_rejects_bad_history_cap():
    with pytest.raises(ConfigError, match="max_history_entries"):
        ExecutionConfig(max_history_entries=0)


def test_execution_rejects_out_of_range_score():
    with pytest.raises(ConfigError, match="target_score"):
        ExecutionConfig(target_score=101)


# --- ScoringWeights ---------------------------------------------------------


def test_scoring_weights_must_sum_to_100():
    with pytest.raises(ConfigError, match="must sum to 100"):
        ScoringWeights(build=10, tests=10, security=10, review=10)


def test_scoring_rejects_bad_severity_weight():
    with pytest.raises(ConfigError, match="severity.critical"):
        ScoringWeights(critical=-1)


def test_scoring_rejects_bad_cap():
    with pytest.raises(ConfigError, match="unverified_build_cap"):
        ScoringWeights(unverified_build_cap=101)


def test_penalty_for_unknown_severity_uses_fallback():
    weights = ScoringWeights(unknown=7.0)
    assert weights.penalty_for("BOGUS") == 7.0
    assert weights.penalty_for("critical") == 30.0


def test_scoring_weights_configurable_from_yaml():
    cfg = build_config(
        {
            "scoring": {
                "build": 25,
                "tests": 25,
                "security": 25,
                "review": 25,
                "severity": {"critical": 40},
            }
        },
        env={},
    )
    assert cfg.scoring.build == 25.0
    assert cfg.scoring.critical == 40.0


# --- RetryPolicy ------------------------------------------------------------


def test_retry_rejects_zero_attempts():
    with pytest.raises(ConfigError, match="max_attempts"):
        RetryPolicy(max_attempts=0)


def test_retry_rejects_bad_backoff_base():
    with pytest.raises(ConfigError, match="backoff_base"):
        RetryPolicy(backoff_base=0.5)


def test_retry_rejects_bad_backoff_max():
    with pytest.raises(ConfigError, match="backoff_max"):
        RetryPolicy(backoff_max=-1)


def test_retry_backoff_is_exponential_and_capped():
    policy = RetryPolicy(backoff_base=2.0, backoff_max=10.0)
    assert policy.delay_for(1) == 2.0
    assert policy.delay_for(2) == 4.0
    assert policy.delay_for(10) == 10.0  # capped


def test_retry_configurable_from_yaml():
    cfg = build_config({"retry": {"max_attempts": 7, "backoff_base": 3.0}}, env={})
    assert cfg.retry.max_attempts == 7
    assert cfg.retry.delay_for(1) == 3.0


# --- AgentSpec --------------------------------------------------------------


def test_agent_spec_rejects_empty_model():
    with pytest.raises(ConfigError, match="model must be"):
        AgentSpec(model="", role="R")


def test_agent_spec_rejects_empty_role():
    with pytest.raises(ConfigError, match="role must be"):
        AgentSpec(model="m", role="")


def test_agent_spec_rejects_bad_temperature():
    with pytest.raises(ConfigError, match="temperature"):
        AgentSpec(model="m", role="R", temperature=5.0)


def test_agent_spec_rejects_bad_max_tokens():
    with pytest.raises(ConfigError, match="max_tokens"):
        AgentSpec(model="m", role="R", max_tokens=0)


# --- OpenRouterConfig -------------------------------------------------------


def test_openrouter_rejects_non_http_base_url():
    with pytest.raises(ConfigError, match="base_url"):
        OpenRouterConfig(base_url="ftp://example.com")


def test_openrouter_default_headers_omits_empty():
    assert OpenRouterConfig(site_url="", site_name="").default_headers() == {}


def test_openrouter_default_headers_populated():
    headers = OpenRouterConfig(site_url="https://x.dev", site_name="Looper").default_headers()
    assert headers == {"HTTP-Referer": "https://x.dev", "X-Title": "Looper"}


# --- Phase list validation --------------------------------------------------


def test_unknown_phase_is_rejected():
    with pytest.raises(ConfigError, match="unknown phase"):
        build_config({"phases": ["research", "teleport"]}, env={})


def test_duplicate_phase_is_rejected():
    with pytest.raises(ConfigError, match="duplicate phases"):
        build_config({"phases": ["research", "research"]}, env={})


def test_phase_list_must_be_a_list():
    with pytest.raises(ConfigError, match="must be a list"):
        build_config({"phases": "research"}, env={})


def test_custom_phase_lists_are_accepted():
    cfg = build_config(
        {"phases": ["build"], "retry_phases": ["test"], "final_phases": ["documentation"]},
        env={},
    )
    assert cfg.first_cycle_phases == ("build",)
    assert cfg.retry_cycle_phases == ("test",)
    assert cfg.final_phases == ("documentation",)


# --- Immutability -----------------------------------------------------------


def test_config_is_frozen(config):
    with pytest.raises(Exception):
        config.workspace = Path("/tmp/other")


def test_with_returns_a_modified_copy(config):
    updated = config.with_(watch_interval=9.0)
    assert updated.watch_interval == 9.0
    assert config.watch_interval != 9.0


def test_missing_agent_definition_is_rejected():
    with pytest.raises(ConfigError, match="missing agent definitions"):
        LooperConfig(agents={"builder": DEFAULT_AGENTS["builder"]})


def test_bad_watch_interval_rejected():
    with pytest.raises(ConfigError, match="watch_interval"):
        LooperConfig(watch_interval=0)


def test_require_number_rejects_bool():
    """bool is a subclass of int; it must not slip through numeric checks."""
    with pytest.raises(ConfigError, match="must be a number"):
        ScoringWeights(build=True, tests=30, security=30, review=20)


def test_require_number_rejects_non_numeric_string():
    with pytest.raises(ConfigError, match="must be a number"):
        ScoringWeights(build="20", tests=30, security=30, review=20)
