"""Regression guards for the v3 audit findings.

Every test here pins a *behaviour* that previously passed a full green suite
while the guarantee it advertised did not hold. The recurring failure mode in
this codebase has been tests that prove a function was called rather than that
its effect occurred, so these assert observable outcomes: bytes on disk, the
numbers that reach scoring, the argv that would actually run.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

from looper.adequacy import evaluate_suite
from looper.config import build_config
from looper.llm import AgentReply
from looper.phases import CODE_FILE, PhaseManager, WorkspaceEscapeError
from looper.sandbox import docker_argv, scan_for_dangerous_calls, to_container_path
from looper.scoring import reports_no_issues
from looper.state import StateManager


class ScriptedClient:
    """Returns a fixed reply for every agent call."""

    def __init__(self, text: str, ok: bool = True) -> None:
        self.text = text
        self.ok = ok

    async def call(self, agent, prompt, extra_system: str = "") -> AgentReply:
        return AgentReply(text=self.text, ok=self.ok, attempts=1)


@pytest.fixture()
def manager(tmp_path):
    def _make(reply: str, **execution):
        config = build_config(
            {
                "workspace": str(tmp_path / "ws"),
                "state_file": str(tmp_path / "state.json"),
                "execution": {"lint_generated": "off", **execution},
            },
            env={},
        )
        return PhaseManager(config, StateManager(config.state_file), ScriptedClient(reply))

    return _make


# --- C-2: run_fix must not persist fenced markdown -----------------------


def test_run_fix_writes_fence_stripped_source(manager):
    """C-2: build_ok=True must describe a file that actually parses.

    run_build stripped fences before writing; run_fix wrote reply.text raw,
    so every fix cycle left ```python markers in CODE_FILE.
    """
    phases = manager("```python\nvalue = 1\n```")
    result = asyncio.run(phases.run_fix("goal", ["an issue"]))

    on_disk = (Path(phases.workspace) / CODE_FILE).read_text(encoding="utf-8")
    assert result.build_ok is True
    assert "```" not in on_disk
    ast.parse(on_disk)  # raises if the artifact is not valid Python


def test_run_fix_refuses_to_clobber_a_good_artifact(manager):
    """C-2: a fix that does not parse must lose, not overwrite the artifact."""
    good = manager("value = 1\n")
    asyncio.run(good.run_fix("goal", ["issue"]))
    artifact = Path(good.workspace) / CODE_FILE
    assert artifact.read_text(encoding="utf-8").strip() == "value = 1"

    broken = manager("def broken(:\n")
    result = asyncio.run(broken.run_fix("goal", ["issue"]))

    assert result.build_ok is False
    assert artifact.read_text(encoding="utf-8").strip() == "value = 1"


# --- C-3: user-suite failures must reach the score -----------------------


def test_user_test_failures_are_counted_without_a_note(manager):
    """C-3: folding user results in only when a note was set discarded real
    failures -- a suite failing 5 of 5 still scored the full test weight."""
    phases = manager("def test_x():\n    assert True\n")

    async def generated():
        return 3, 0, ""

    async def user():
        return 0, 5, ""

    phases._execute_generated_tests = generated  # type: ignore[assignment]
    phases._run_user_tests = user  # type: ignore[assignment]

    result = asyncio.run(phases.run_test("goal"))

    assert (result.tests_passed, result.tests_total) == (3, 8)
    assert result.tests_passed / result.tests_total < 1.0


def test_user_test_passes_are_counted_too(manager):
    """C-3: the fix must not silently drop the passing side either."""
    phases = manager("def test_x():\n    assert True\n")

    async def generated():
        return 2, 0, ""

    async def user():
        return 4, 0, ""

    phases._execute_generated_tests = generated  # type: ignore[assignment]
    phases._run_user_tests = user  # type: ignore[assignment]

    result = asyncio.run(phases.run_test("goal"))
    assert (result.tests_passed, result.tests_total) == (6, 6)


# --- H-1: container argv must not carry host paths -----------------------


def test_docker_argv_rewrites_workspace_paths_to_work():
    """H-1: the host tests path does not exist inside the image, so the
    container backend could never once succeed."""
    cwd = str(Path("/srv/ws").resolve())
    argv = [sys.executable, "-I", "-B", "-m", "pytest", str(Path(cwd) / "tests"), "-q"]

    wrapped = docker_argv(
        argv,
        cwd=cwd,
        image="python:3.11-slim",
        network="none",
        cpu_seconds=60,
        rss_bytes=1_000_000_000,
    )

    assert not any(cwd in part for part in wrapped[wrapped.index("python") :])
    assert "/work/tests" in wrapped


def test_to_container_path_maps_root_and_leaves_outsiders_alone():
    cwd = str(Path("/srv/ws").resolve())
    assert to_container_path(cwd, cwd) == "/work"
    assert to_container_path(str(Path(cwd) / "a" / "b"), cwd) == "/work/a/b"
    assert to_container_path("-q", cwd) == "-q"
    outside = str(Path("/other/place").resolve())
    assert to_container_path(outside, cwd) == outside


# --- H-2: scanner must not fire on ordinary code -------------------------


def test_json_loads_is_not_flagged():
    """H-2: matching the bare attribute reported json.loads as dangerous,
    rejecting perfectly normal suites -- a phantom finding."""
    source = "import json\n\ndef test_x():\n    assert json.loads('{}') == {}\n"
    assert scan_for_dangerous_calls(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.popen('ls')\n",
        "import shutil\nshutil.move('a', 'b')\n",
        "from pathlib import Path\nPath('x').write_text('y')\n",
        "import os\ngetattr(os, 'system')('ls')\n",
        "import subprocess\nsubprocess.run(['ls'])\n",
    ],
)
def test_genuinely_dangerous_calls_are_refused(source):
    """H-2: each of these previously slipped past the scanner entirely."""
    assert scan_for_dangerous_calls(source) != []


def test_comment_stripping_keeps_string_literals_intact():
    """M-3: splitting on the first '#' truncated at a '#' inside a string,
    hiding whatever followed on that line."""
    source = 'import os\nurl = "http://x/#frag"; os.system("rm -rf /")\n'
    assert scan_for_dangerous_calls(source) != []


def test_prose_mentioning_a_dangerous_call_is_not_flagged():
    source = "def test_x():\n    # never call os.system here\n    assert True\n"
    assert scan_for_dangerous_calls(source) == []


# --- H-4: symlinked components must be refused ---------------------------


def test_symlinked_component_is_refused(tmp_path, manager):
    """H-4: a '..' check alone does not stop a symlinked directory
    redirecting an LLM-named write outside the workspace."""
    phases = manager("x = 1\n")
    workspace = Path(phases.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    try:
        (workspace / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    with pytest.raises(WorkspaceEscapeError):
        phases.resolve_in_workspace("escape/pwned.py")


def test_symlinked_component_is_refused_without_privilege(manager, monkeypatch):
    """H-4: same guard, exercised on hosts where creating a real symlink
    needs privilege (Windows CI). Without this the branch went untested."""
    phases = manager("x = 1\n")
    workspace = Path(phases.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace.resolve() / "escape" / "pwned.py"

    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self):
        if self == target.parent:
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(WorkspaceEscapeError, match="Symlinked"):
        phases.resolve_in_workspace("escape/pwned.py")


def test_parent_traversal_is_still_refused(manager):
    phases = manager("x = 1\n")
    with pytest.raises(WorkspaceEscapeError):
        phases.resolve_in_workspace("../escaped.py")


# --- M-5: "clean" prose must not become a phantom finding ----------------


@pytest.mark.parametrize(
    "text",
    [
        "No security issues were identified in this code.",
        "No vulnerabilities found.",
        "Findings: none",
        "None identified.",
        "Nothing of concern.",
    ],
)
def test_clean_audit_prose_is_recognised(text):
    """M-5: three literal English phrases meant a clean report in any other
    wording became a phantom MEDIUM finding."""
    assert reports_no_issues(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "HIGH: the token is compared with ==",
        "The code has issues that need attention.",
        "",
    ],
)
def test_non_clean_prose_is_not_treated_as_clean(text):
    assert reports_no_issues(text) is False


# --- H-3: rlimits must be installed with the right shape ----------------


def test_posix_rlimit_fn_sets_tuples_and_does_not_overwrite_cpu(monkeypatch):
    """H-3: setrlimit requires a (soft, hard) tuple, and a trailing write of
    wall_seconds to RLIMIT_CPU silently replaced the real CPU cap. The whole
    function carried 'pragma: no cover', so none of it was ever executed.
    """
    import types

    from looper import sandbox

    calls: list[tuple[int, object]] = []

    fake = types.SimpleNamespace(
        RLIMIT_CPU=0,
        RLIMIT_AS=1,
        RLIMIT_NPROC=2,
        RLIMIT_FSIZE=3,
        setrlimit=lambda limit, value: calls.append((limit, value)),
    )
    monkeypatch.setitem(sys.modules, "resource", fake)

    sandbox._posix_rlimit_fn(cpu_seconds=60, wall_seconds=300, rss_bytes=1_000)()

    assert all(isinstance(value, tuple) and len(value) == 2 for _, value in calls)
    cpu_values = [value for limit, value in calls if limit == fake.RLIMIT_CPU]
    assert cpu_values == [(60, 60)]  # not overwritten with wall_seconds
    assert (fake.RLIMIT_AS, (1_000, 1_000)) in calls
    assert (fake.RLIMIT_NPROC, (64, 64)) in calls


def test_posix_rlimit_fn_survives_a_rejected_limit(monkeypatch):
    """H-3: the _set helper existed but was never called, so a ValueError
    from a hardened kernel killed the child instead of being logged."""
    import types

    from looper import sandbox

    def refuse(limit, value):
        raise ValueError("not permitted")

    fake = types.SimpleNamespace(
        RLIMIT_CPU=0, RLIMIT_AS=1, RLIMIT_NPROC=2, RLIMIT_FSIZE=3, setrlimit=refuse
    )
    monkeypatch.setitem(sys.modules, "resource", fake)

    sandbox._posix_rlimit_fn(cpu_seconds=1, wall_seconds=1, rss_bytes=1)()


def test_posix_rlimit_fn_skips_limits_the_platform_lacks(monkeypatch):
    """H-3: RLIMIT_NPROC/FSIZE are not universal; absence must not raise."""
    import types

    from looper import sandbox

    calls: list[tuple[int, object]] = []
    fake = types.SimpleNamespace(
        RLIMIT_CPU=0,
        RLIMIT_AS=1,
        setrlimit=lambda limit, value: calls.append((limit, value)),
    )
    monkeypatch.setitem(sys.modules, "resource", fake)

    sandbox._posix_rlimit_fn(cpu_seconds=5, wall_seconds=5, rss_bytes=5)()
    assert len(calls) == 2


# --- M-4: a broken backend must not kill the build ----------------------


def test_pytest_runner_fails_closed_on_an_unexpected_error(manager, monkeypatch):
    """M-4: a malformed sandbox argv raising ValueError propagated out of
    run_test and aborted the entire build instead of failing that phase."""
    phases = manager("def test_x():\n    assert True\n")

    def explode(*args, **kwargs):
        raise ValueError("malformed sandbox argv")

    monkeypatch.setattr("looper.phases.execution.run_sandboxed", explode)

    passed, failed, note = asyncio.run(phases._run_pytest(Path(phases.workspace)))
    assert (passed, failed) == (0, 1)
    assert "error" in note.lower()


# --- L-4: /status snapshot caching --------------------------------------


def test_snapshot_is_cached_until_state_changes(tmp_path):
    """L-4: /status paid a full O(history) serialise+parse on every poll."""
    state = StateManager(tmp_path / "state.json")
    first = state.snapshot()
    assert state._snapshot_cache is not None

    state.update(status="running")
    assert state._snapshot_cache is None  # invalidated by the mutator

    second = state.snapshot()
    assert second["status"] == "running"
    assert first is not second


def test_snapshot_callers_cannot_poison_the_cache(tmp_path):
    """A cached snapshot must still hand out independent copies."""
    state = StateManager(tmp_path / "state.json")
    first = state.snapshot()
    first["status"] = "tampered"
    assert state.snapshot()["status"] != "tampered"


def test_every_mutator_invalidates_the_snapshot(tmp_path):
    state = StateManager(tmp_path / "state.json")

    for mutate in (
        lambda: state.append_history({"cycle": 1}),
        lambda: state.record_files(["a.py"]),
        lambda: state.record_error("boom"),
    ):
        state.snapshot()
        assert state._snapshot_cache is not None
        mutate()
        assert state._snapshot_cache is None


# --- Coverage of the new guard branches ---------------------------------


def test_count_asserts_handles_empty_and_unparsable_source():
    """Both early exits of the standalone (tree=None) path."""
    from looper.adequacy import _count_assert_statements

    assert _count_assert_statements("   \n") == 0
    assert _count_assert_statements("def broken(:\n") == 0
    assert _count_assert_statements("def test_x():\n    assert True\n") == 1


def test_config_rejects_non_tuple_trusted_proxies():
    """M-2: the new field is validated like every other config entry."""
    from looper.config import ConfigError, HTTPConfig

    with pytest.raises(ConfigError, match="trusted_proxies"):
        HTTPConfig(trusted_proxies=["10.0.0.1"])  # type: ignore[arg-type]


def test_config_accepts_trusted_proxies_from_yaml():
    config = build_config({"http": {"trusted_proxies": ["10.0.0.1", "10.0.0.2"]}}, env={})
    assert config.http.trusted_proxies == ("10.0.0.1", "10.0.0.2")


def test_user_tests_outside_workspace_are_skipped_in_container_mode(tmp_path):
    """H-1 follow-on: only the workspace is mounted at /work, so an outside
    suite would 'pass' without ever having run."""
    outside = tmp_path / "human_tests"
    outside.mkdir()
    (outside / "test_real.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    config = build_config(
        {
            "workspace": str(tmp_path / "ws"),
            "state_file": str(tmp_path / "state.json"),
            "execution": {
                "user_tests_dir": str(outside),
                "sandbox_tests": True,
                "sandbox_backend": "docker",
            },
        },
        env={},
    )
    phases = PhaseManager(config, StateManager(config.state_file), ScriptedClient("x = 1\n"))

    assert asyncio.run(phases._run_user_tests()) == (0, 0, "")


def test_user_tests_inside_workspace_still_run_in_container_mode(tmp_path, monkeypatch):
    """The guard must not skip a suite that IS mounted."""
    workspace = tmp_path / "ws"
    inside = workspace / "human_tests"
    inside.mkdir(parents=True)

    config = build_config(
        {
            "workspace": str(workspace),
            "state_file": str(tmp_path / "state.json"),
            "execution": {
                "user_tests_dir": str(inside),
                "sandbox_tests": True,
                "sandbox_backend": "docker",
            },
        },
        env={},
    )
    phases = PhaseManager(config, StateManager(config.state_file), ScriptedClient("x = 1\n"))

    async def fake_pytest(path):
        return 7, 0, ""

    monkeypatch.setattr(phases, "_run_pytest", fake_pytest)
    assert asyncio.run(phases._run_user_tests()) == (7, 0, "")


def test_run_fix_rejects_code_that_fails_the_lint_gate(tmp_path, monkeypatch):
    """C-2: a fix that parses but fails lint must not be accepted."""
    config = build_config(
        {
            "workspace": str(tmp_path / "ws"),
            "state_file": str(tmp_path / "state.json"),
            "execution": {"lint_generated": "py_compile"},
        },
        env={},
    )
    phases = PhaseManager(config, StateManager(config.state_file), ScriptedClient("value = 1\n"))
    monkeypatch.setattr(phases, "_lint_generated", lambda path: (False, "lint: E999"))

    result = asyncio.run(phases.run_fix("goal", ["issue"]))
    assert result.build_ok is False
    assert "lint" in result.summary.lower()


def test_rate_limiter_refuses_over_the_limit_within_the_window():
    """Covers the limit branch alongside the new eviction code."""
    from looper.server import RateLimiter

    limiter = RateLimiter(limit_per_minute=2)
    assert limiter.allow("a", now=1000.0) is True
    assert limiter.allow("a", now=1000.1) is True
    assert limiter.allow("a", now=1000.2) is False


def test_to_container_path_ignores_a_malformed_value():
    """The relative/non-path branch of the rewriter."""
    cwd = str(Path("/srv/ws").resolve())
    assert to_container_path("", cwd) == ""


def test_scanner_deduplicates_repeated_reasons():
    """Two identical dangerous calls must yield one reason, not two."""
    source = "import os\nos.popen('a')\nos.popen('b')\n"
    reasons = scan_for_dangerous_calls(source)
    assert len(reasons) == len(set(reasons))
    assert sum("os.popen" in r for r in reasons) >= 1


def test_rate_limiter_expires_old_hits_but_keeps_a_live_client():
    """The per-bucket prune must run for a client that is still inside the
    window: its oldest hit ages out while the newest keeps it alive."""
    from looper.server import RateLimiter

    limiter = RateLimiter(limit_per_minute=2, window_seconds=60.0)
    assert limiter.allow("a", now=1000.0) is True
    assert limiter.allow("a", now=1059.0) is True
    # At t=1061 the cutoff is 1001: the 1000.0 hit is pruned, 1059.0 remains,
    # so the client is still tracked and has room for one more request.
    assert limiter.allow("a", now=1061.0) is True
    assert limiter.tracked_clients == 1


def test_assertion_against_a_variable_is_not_a_hardcoded_verdict():
    """L-3: 'assert result.tests_passed == expected' is a real assertion,
    not a suite written to pass."""
    source = (
        "def test_counts():\n"
        "    expected = compute()\n"
        "    result = run()\n"
        "    assert result.tests_passed == expected\n"
    )
    verdict = evaluate_suite(source, min_assertions_per_100_lines=0)
    assert "hardcode" not in verdict.reason.lower()


def test_assertion_against_a_literal_score_is_still_flagged():
    source = "def test_score():\n    assert score == 95\n"
    verdict = evaluate_suite(source, min_assertions_per_100_lines=0)
    assert verdict.ok is False
