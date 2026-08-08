"""Git integration: make agent output reviewable with ordinary tooling.

Without this, a build's only artifact is a workspace directory that gets
overwritten by the next run -- there is no way to see *what changed* in cycle
3 versus cycle 1, and no way to review LLM output the way code is normally
reviewed. Committing each cycle onto a dedicated branch turns the pipeline's
history into a ``git log``/``git diff`` a human can actually read (ADR-010).

Every call is a fixed argv (never ``shell=True``) and every failure is
non-fatal: version control is an observability feature, so a missing ``git``
binary degrades to "no commits" rather than failing the build.
"""

from __future__ import annotations

import logging
import re
import subprocess  # nosec B404 - fixed argv, never shell=True
from pathlib import Path
from typing import Callable

logger = logging.getLogger("looper.vcs")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

#: Anything outside this set is collapsed to "-" when a goal becomes a branch
#: name, so an LLM-authored goal can never inject git refspec syntax.
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")

#: Git refs cannot contain these, and a leading "-" would parse as a flag.
MAX_SLUG_LENGTH = 48


def slugify_goal(goal: str, *, max_length: int = MAX_SLUG_LENGTH) -> str:
    """Turn a free-text goal into a safe branch-name fragment.

    The goal is attacker-influenced text (``POST /build`` accepts any string),
    so this is an allowlist: lowercase alphanumerics joined by ``-``. An empty
    result falls back to ``build`` rather than producing an invalid ref.
    """
    slug = _SLUG_UNSAFE.sub("-", goal.strip().lower()).strip("-")
    slug = slug[:max_length].strip("-")
    return slug or "build"


class GitRepo:
    """A thin, failure-tolerant wrapper over the git CLI for one workspace."""

    def __init__(
        self,
        workspace: Path,
        *,
        author_name: str = "looper",
        author_email: str = "looper@localhost",
        runner: Runner | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.author_name = author_name
        self.author_email = author_email
        self._run = runner or subprocess.run

    # -- plumbing --------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        # The workspace may not exist yet on the very first build; git would
        # then fail with an opaque "directory name is invalid" instead of
        # doing the obvious thing.
        self.workspace.mkdir(parents=True, exist_ok=True)
        return self._run(  # nosec B603 B607 - fixed argv, no shell
            [
                "git",
                "-c",
                f"user.name={self.author_name}",
                "-c",
                f"user.email={self.author_email}",
                *args,
            ],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def available(self) -> bool:
        """True when a usable ``git`` binary is on PATH."""
        try:
            return self._git("--version").returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("git unavailable: %s", exc)
            return False

    # -- operations ------------------------------------------------------

    def ensure_repo(self) -> bool:
        """Initialise the workspace as a repo if it is not one already."""
        if not self.available():
            return False
        if (self.workspace / ".git").exists():
            return True
        proc = self._git("init", "-q")
        if proc.returncode != 0:
            logger.warning("git init failed: %s", proc.stderr.strip()[:200])
            return False
        return True

    def checkout_branch(self, branch: str) -> bool:
        """Switch to ``branch``, creating it when it does not exist."""
        if self._git("rev-parse", "--verify", "--quiet", branch).returncode == 0:
            proc = self._git("checkout", branch)
        else:
            proc = self._git("checkout", "-b", branch)
        if proc.returncode != 0:
            logger.warning("git checkout %s failed: %s", branch, proc.stderr.strip()[:200])
            return False
        return True

    def commit_all(self, message: str) -> str:
        """Stage everything and commit. Returns the short SHA, or "".

        An empty return is normal and not an error: a cycle that changed no
        files has nothing to commit.
        """
        if self._git("add", "-A").returncode != 0:
            return ""
        proc = self._git("commit", "-m", message)
        if proc.returncode != 0:
            logger.debug("nothing to commit: %s", proc.stdout.strip()[:200])
            return ""
        rev = self._git("rev-parse", "--short", "HEAD")
        return rev.stdout.strip() if rev.returncode == 0 else ""

    def diff_stat(self, ref: str = "HEAD~1") -> str:
        """Return ``git diff --stat`` against ``ref``, or "" when unavailable."""
        proc = self._git("diff", "--stat", ref)
        return proc.stdout.strip() if proc.returncode == 0 else ""


class BuildRepo:
    """Per-build git session: one branch, one commit per cycle."""

    def __init__(self, repo: GitRepo, *, branch_prefix: str = "looper/") -> None:
        self.repo = repo
        self.branch_prefix = branch_prefix
        self.branch = ""
        self.commits: list[str] = []
        self.enabled = False

    def start(self, goal: str) -> str:
        """Open a branch for ``goal``. Returns the branch name, or "" if off."""
        if not self.repo.ensure_repo():
            self.enabled = False
            return ""
        self.branch = f"{self.branch_prefix}{slugify_goal(goal)}"
        self.enabled = self.repo.checkout_branch(self.branch)
        return self.branch if self.enabled else ""

    def record_cycle(self, cycle: int, score: float, summary: str = "") -> str:
        """Commit the current workspace as cycle ``cycle``."""
        if not self.enabled:
            return ""
        message = f"looper: cycle {cycle} (score {score:.2f})"
        if summary:
            message += f"\n\n{summary}"
        sha = self.repo.commit_all(message)
        if sha:
            self.commits.append(sha)
        return sha

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "branch": self.branch,
            "commits": list(self.commits),
        }
