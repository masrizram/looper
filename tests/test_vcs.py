"""Proof tests for git integration (ADR-010).

Two properties matter here and both are security-relevant:

* a goal is attacker-influenced text (``POST /build`` takes any string), so it
  must never reach a git refspec unsanitised;
* version control is observability, not correctness, so every git failure has
  to degrade to "no commits" instead of breaking the build.
"""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from looper.vcs import BuildRepo, GitRepo, slugify_goal

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git binary not available")


def _scripted(*results, raises: Exception | None = None):
    """Fake runner returning queued exit codes, recording every argv."""
    queue = list(results)
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if raises is not None:
            raise raises
        code, out = queue.pop(0) if queue else (0, "")
        return SimpleNamespace(returncode=code, stdout=out, stderr=out, args=argv)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# -- slug safety ---------------------------------------------------------


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("build a URL shortener", "build-a-url-shortener"),
        ("  Mixed CASE  ", "mixed-case"),
        ("../../etc/passwd", "etc-passwd"),
        ("--upload-pack=evil", "upload-pack-evil"),
        ("refs/heads/main..HEAD", "refs-heads-main-head"),
        ("!!!", "build"),
        ("", "build"),
    ],
)
def test_slugify_is_an_allowlist(goal: str, expected: str):
    assert slugify_goal(goal) == expected


def test_slug_is_length_capped():
    slug = slugify_goal("word " * 100)
    assert len(slug) <= 48
    assert not slug.endswith("-")


def test_slug_never_contains_ref_metacharacters():
    slug = slugify_goal("a~b^c:d?e*f[g]h\\i j")
    assert all(ch.isalnum() or ch == "-" for ch in slug)


# -- GitRepo, faked ------------------------------------------------------


def test_available_false_when_binary_missing(tmp_path):
    repo = GitRepo(tmp_path, runner=_scripted(raises=FileNotFoundError("no git")))
    assert repo.available() is False


def test_available_false_on_nonzero(tmp_path):
    assert GitRepo(tmp_path, runner=_scripted((1, ""))).available() is False


def test_ensure_repo_returns_false_when_git_missing(tmp_path):
    repo = GitRepo(tmp_path, runner=_scripted((1, "")))
    assert repo.ensure_repo() is False


def test_ensure_repo_reports_init_failure(tmp_path, caplog):
    # --version ok, then init fails.
    repo = GitRepo(tmp_path, runner=_scripted((0, ""), (1, "boom")))
    with caplog.at_level("WARNING"):
        assert repo.ensure_repo() is False
    assert "git init failed" in caplog.text


def test_ensure_repo_short_circuits_on_existing_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    run = _scripted((0, ""))
    assert GitRepo(tmp_path, runner=run).ensure_repo() is True
    # Only the --version probe ran; no second `git init`.
    assert len(run.calls) == 1


def test_checkout_failure_is_reported(tmp_path, caplog):
    # rev-parse fails (branch unknown) then checkout -b fails.
    repo = GitRepo(tmp_path, runner=_scripted((1, ""), (1, "denied")))
    with caplog.at_level("WARNING"):
        assert repo.checkout_branch("looper/x") is False
    assert "git checkout" in caplog.text


def test_checkout_existing_branch_uses_plain_checkout(tmp_path):
    run = _scripted((0, ""), (0, ""))
    assert GitRepo(tmp_path, runner=run).checkout_branch("looper/x") is True
    assert "-b" not in run.calls[1]


def test_commit_all_returns_empty_when_add_fails(tmp_path):
    assert GitRepo(tmp_path, runner=_scripted((1, ""))).commit_all("m") == ""


def test_commit_all_returns_empty_when_nothing_to_commit(tmp_path):
    # add ok, commit non-zero ("nothing to commit").
    repo = GitRepo(tmp_path, runner=_scripted((0, ""), (1, "nothing to commit")))
    assert repo.commit_all("m") == ""


def test_commit_all_returns_empty_when_rev_parse_fails(tmp_path):
    repo = GitRepo(tmp_path, runner=_scripted((0, ""), (0, ""), (1, "")))
    assert repo.commit_all("m") == ""


def test_diff_stat_empty_on_failure(tmp_path):
    assert GitRepo(tmp_path, runner=_scripted((1, ""))).diff_stat() == ""


def test_diff_stat_returns_output(tmp_path):
    repo = GitRepo(tmp_path, runner=_scripted((0, " a.py | 2 +-\n")))
    assert "a.py" in repo.diff_stat()


def test_author_identity_is_passed_to_git(tmp_path):
    run = _scripted((0, ""))
    GitRepo(tmp_path, author_name="bot", author_email="b@x", runner=run).available()
    assert "user.name=bot" in run.calls[0]
    assert "user.email=b@x" in run.calls[0]


def test_workspace_is_created_on_demand(tmp_path):
    missing = tmp_path / "not-yet"
    GitRepo(missing, runner=_scripted((0, ""))).available()
    assert missing.is_dir()


# -- BuildRepo, faked ----------------------------------------------------


def test_build_repo_disabled_when_repo_unavailable(tmp_path):
    session = BuildRepo(GitRepo(tmp_path, runner=_scripted((1, ""))))
    assert session.start("goal") == ""
    assert session.enabled is False
    assert session.record_cycle(1, 90.0) == ""


def test_build_repo_start_failure_on_checkout(tmp_path):
    # --version ok, init ok, rev-parse fail, checkout -b fail
    run = _scripted((0, ""), (0, ""), (1, ""), (1, ""))
    session = BuildRepo(GitRepo(tmp_path, runner=run))
    assert session.start("goal") == ""
    assert session.enabled is False


def test_as_dict_shape(tmp_path):
    session = BuildRepo(GitRepo(tmp_path, runner=_scripted((1, ""))))
    session.start("goal")
    assert session.as_dict() == {"enabled": False, "branch": "", "commits": []}


# -- real git ------------------------------------------------------------


@requires_git
def test_real_git_roundtrip_produces_a_reviewable_history(tmp_path):
    """The whole point of ADR-010: `git log` shows one commit per cycle."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = BuildRepo(GitRepo(workspace), branch_prefix="looper/")

    branch = session.start("build a URL shortener")
    assert branch == "looper/build-a-url-shortener"

    (workspace / "generated.py").write_text("x = 1\n", encoding="utf-8")
    first = session.record_cycle(1, 72.5, "build=20 tests=0")
    assert first, "cycle 1 must produce a commit"

    (workspace / "generated.py").write_text("x = 2\n", encoding="utf-8")
    second = session.record_cycle(2, 96.0, "build=20 tests=30")
    assert second and second != first

    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "cycle 1 (score 72.50)" in log
    assert "cycle 2 (score 96.00)" in log
    assert session.as_dict()["commits"] == [first, second]

    # And the diff between cycles is inspectable -- the reviewability payoff.
    assert "generated.py" in session.repo.diff_stat("HEAD~1")


@requires_git
def test_real_git_no_changes_produces_no_commit(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = BuildRepo(GitRepo(workspace))
    session.start("goal")
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert session.record_cycle(1, 50.0)
    # Nothing changed since -> no second commit, and no error.
    assert session.record_cycle(2, 50.0) == ""


@requires_git
def test_real_git_reuses_existing_branch(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    repo = GitRepo(workspace)
    session = BuildRepo(repo)
    session.start("same goal")
    (workspace / "a.py").write_text("1\n", encoding="utf-8")
    session.record_cycle(1, 10.0)
    # A second build with the same goal must check the branch out, not fail.
    again = BuildRepo(repo)
    assert again.start("same goal") == "looper/same-goal"
    assert again.enabled is True
