"""Proof tests for `--doctor`, config validation, and the orchestrator's git hook.

`--doctor` exists because the sandbox gap was *invisible*: a Windows host
silently ran LLM-authored tests unconfined while the docs promised limits.
The command has to surface that before a build, and exit non-zero so CI can
gate on it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from looper.cli import EXIT_OK, EXIT_SANDBOX_UNAVAILABLE, main, run_doctor
from looper.config import ConfigError, build_config
from looper.orchestrator import LooperDaemon
from looper.sandbox import posix_rlimits_available
from looper.scoring import ScoreBreakdown
from looper.vcs import BuildRepo
from tests.conftest import make_client


def _client_for(cfg):
    return make_client(cfg)


# -- config validation ---------------------------------------------------


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({"sandbox_backend": "chroot"}, "sandbox_backend must be one of"),
        ({"sandbox_image": ""}, "sandbox_image must not be empty"),
        ({"artifact_mode": "monorepo"}, "artifact_mode must be single_file|package"),
        ({"max_files_per_build": 0}, "max_files_per_build"),
        ({"git": {"branch_prefix": ""}}, "git_branch_prefix must not be empty"),
    ],
)
def test_invalid_execution_values_are_refused(raw_config, override, fragment):
    with pytest.raises(ConfigError) as err:
        build_config({**raw_config, "execution": override}, env={})
    assert fragment in str(err.value)


def test_git_section_must_be_a_mapping(raw_config):
    with pytest.raises(ConfigError) as err:
        build_config({**raw_config, "execution": {"git": ["nope"]}}, env={})
    assert "execution.git must be a mapping" in str(err.value)


def test_git_section_is_parsed(raw_config):
    cfg = build_config(
        {
            **raw_config,
            "execution": {
                "git": {
                    "enabled": True,
                    "branch_prefix": "ai/",
                    "commit_per_cycle": False,
                    "author_name": "bot",
                    "author_email": "bot@x",
                }
            },
        },
        env={},
    )
    assert cfg.execution.git_enabled is True
    assert cfg.execution.git_branch_prefix == "ai/"
    assert cfg.execution.git_commit_per_cycle is False
    assert cfg.execution.git_author_name == "bot"
    assert cfg.execution.git_author_email == "bot@x"


def test_sandbox_section_defaults_are_fail_closed(config):
    assert config.execution.sandbox_backend == "auto"
    assert config.execution.sandbox_fail_closed is True
    assert config.execution.artifact_mode == "single_file"
    assert config.execution.git_enabled is False


# -- doctor --------------------------------------------------------------


def test_doctor_reports_unavailable_sandbox_with_exit_5(config, monkeypatch, caplog):
    monkeypatch.setattr("looper.cli.docker_available", lambda: False)
    monkeypatch.setattr("looper.cli.posix_rlimits_available", lambda: False)
    monkeypatch.setattr(
        "looper.cli.resolve_backend",
        lambda *a, **k: (_ for _ in ()).throw(
            __import__("looper.sandbox", fromlist=["x"]).SandboxUnavailableError("nothing here")
        ),
    )
    with caplog.at_level("INFO"):
        assert run_doctor(config) == EXIT_SANDBOX_UNAVAILABLE
    assert "SANDBOX UNAVAILABLE" in caplog.text


def test_doctor_ok_when_docker_present(config, monkeypatch, caplog):
    monkeypatch.setattr("looper.cli.docker_available", lambda: True)
    monkeypatch.setattr("looper.cli.resolve_backend", lambda *a, **k: "docker")
    with caplog.at_level("INFO"):
        assert run_doctor(config) == EXIT_OK
    assert "effective sandbox" in caplog.text


def test_doctor_reports_podman_when_only_podman_available(config, monkeypatch, caplog):
    monkeypatch.setattr("looper.cli.docker_available", lambda: False)
    monkeypatch.setattr("looper.cli.podman_available", lambda: True)
    monkeypatch.setattr("looper.cli.resolve_backend", lambda *a, **k: "podman")
    with caplog.at_level("INFO"):
        assert run_doctor(config) == EXIT_OK
    assert "podman machine      : yes" in caplog.text
    assert "effective sandbox   : podman" in caplog.text


def test_doctor_warns_when_effective_backend_is_none(config, monkeypatch, caplog):
    monkeypatch.setattr("looper.cli.docker_available", lambda: False)
    monkeypatch.setattr("looper.cli.resolve_backend", lambda *a, **k: "none")
    with caplog.at_level("WARNING"):
        assert run_doctor(config) == EXIT_OK
    assert "No isolation in effect" in caplog.text


def test_doctor_warns_when_sandboxing_is_switched_off(raw_config, caplog):
    cfg = build_config({**raw_config, "execution": {"sandbox_tests": False}}, env={})
    with caplog.at_level("WARNING"):
        assert run_doctor(cfg) == EXIT_OK
    assert "run unconfined" in caplog.text


def test_doctor_warns_when_git_enabled_but_missing(raw_config, monkeypatch, caplog):
    cfg = build_config({**raw_config, "execution": {"git": {"enabled": True}}}, env={})
    monkeypatch.setattr("looper.cli.resolve_backend", lambda *a, **k: "none")
    monkeypatch.setattr("looper.vcs.GitRepo.available", lambda self: False)
    with caplog.at_level("WARNING"):
        assert run_doctor(cfg) == EXIT_OK
    assert "no git binary was found" in caplog.text


def test_doctor_is_reachable_from_main(tmp_path, monkeypatch, raw_config):
    import yaml

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
    monkeypatch.setattr("looper.cli.resolve_backend", lambda *a, **k: "none")
    assert main(["--config", str(cfg_path), "--doctor"]) == EXIT_OK


def test_doctor_uses_real_capability_probes(config, monkeypatch):
    """No monkeypatching of the probes: whatever this host is, doctor answers."""
    monkeypatch.setattr(
        "looper.cli.resolve_backend",
        lambda *a, **k: "rlimit" if posix_rlimits_available() else "none",
    )
    assert run_doctor(config) == EXIT_OK


# -- scoring summary -----------------------------------------------------


def test_summary_line_is_commit_ready():
    breakdown = ScoreBreakdown(
        build=20.0,
        tests=30.0,
        security=25.0,
        review=18.0,
        raw_total=93.0,
        total=93.0,
        caps_applied=(),
    )
    assert breakdown.summary_line() == "build=20 tests=30 security=25 review=18"


def test_summary_line_records_caps():
    breakdown = ScoreBreakdown(
        build=0.0,
        tests=0.0,
        security=30.0,
        review=20.0,
        raw_total=50.0,
        total=50.0,
        caps_applied=("unverified_build", "critical_finding"),
    )
    assert "caps=unverified_build,critical_finding" in breakdown.summary_line()


# -- orchestrator git hook ----------------------------------------------


class _FakeSession:
    """Stands in for BuildRepo, recording the calls the loop makes."""

    def __init__(self, branch: str = "looper/goal", sha: str = "abc1234"):
        self.branch = branch
        self._sha = sha
        self.started: list[str] = []
        self.cycles: list[tuple[int, float]] = []
        self.enabled = bool(branch)

    def start(self, goal: str) -> str:
        self.started.append(goal)
        return self.branch

    def record_cycle(self, cycle: int, score: float, summary: str = "") -> str:
        self.cycles.append((cycle, score))
        return self._sha

    def as_dict(self):
        return {"enabled": self.enabled, "branch": self.branch, "commits": [self._sha]}


def test_git_disabled_by_default_creates_no_session(config):
    daemon = LooperDaemon(config, client=SimpleNamespace())
    assert daemon.vcs is None


def test_git_enabled_builds_a_session(raw_config):
    cfg = build_config({**raw_config, "execution": {"git": {"enabled": True}}}, env={})
    daemon = LooperDaemon(cfg, client=SimpleNamespace())
    assert isinstance(daemon.vcs, BuildRepo)
    assert daemon.vcs.branch_prefix == "looper/"


def test_build_commits_each_cycle_and_the_final_artifact(config, client):
    session = _FakeSession()
    daemon = LooperDaemon(config, client=client, vcs=session)
    asyncio.run(daemon.build("build a thing"))

    assert session.started == ["build a thing"]
    # One commit per cycle, plus the final-artifact commit.
    assert len(session.cycles) >= 2
    assert daemon.state.state["git"]["branch"] == "looper/goal"


def test_build_skips_per_cycle_commits_when_disabled(raw_config, config):
    cfg = build_config(
        {**raw_config, "execution": {"git": {"enabled": True, "commit_per_cycle": False}}},
        env={},
    )
    session = _FakeSession()
    daemon = LooperDaemon(cfg, client=_client_for(cfg), vcs=session)
    asyncio.run(daemon.build("goal"))
    # Only the final artifact commit remains.
    assert len(session.cycles) == 1


def test_build_tolerates_a_disabled_branch(config, client):
    session = _FakeSession(branch="", sha="")
    daemon = LooperDaemon(config, client=client, vcs=session)
    # Must not raise even though git never opened a branch.
    asyncio.run(daemon.build("goal"))
    assert session.started == ["goal"]
