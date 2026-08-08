# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [2.1.0] - 2026-08-08

### Added

* **Cross-platform sandbox** (ADR-008). `execution.sandbox_backend`
  (`auto|rlimit|docker|none`) with a hardened Docker backend: throwaway
  read-only container, no network, cpu/memory/pids caps, `no-new-privileges`,
  only the workspace mounted. Docker detection probes the *daemon*, not the
  client binary.
* **`looper --doctor`** — reports the isolation this host can actually deliver
  (docker / rlimits / git, effective backend) and exits `5` when the configured
  policy cannot be met, so CI can gate on it.
* **Multi-file artifacts** (ADR-009). `execution.artifact_mode: package` lets
  the builder emit a file tree via `### FILE: path` markers, with an allowlist
  on paths, a per-build file cap, and syntax + lint verification of every
  Python file. Reviewer and security agents now audit *all* modules.
* **Git integration** (ADR-010). Optional `execution.git`: one branch per goal,
  one commit per cycle with the score breakdown, exposed in `/status`. Goals
  are slugified through an allowlist before reaching a git ref.
* `ScoreBreakdown.summary_line()` for commit-ready score summaries.
* PyPI release workflow with Trusted Publishing, tag/version consistency check,
  and a clean-venv install smoke test.

### Changed

* **BREAKING (safety).** When no sandbox backend is available and
  `execution.sandbox_fail_closed` is true (the default), generated tests are
  **refused** instead of being run unconfined. Previously every Windows host
  silently ran LLM-authored test code with no resource limits while the
  documentation promised isolation. Set `sandbox_fail_closed: false` to accept
  the old behaviour explicitly.
* Package metadata rewritten to describe what Looper is (a release gate for
  AI-written code) with URLs, keywords and classifiers.

### Fixed

* `### FILE: \`path\`` (backticked paths) is now parsed instead of being
  skipped silently.
* `GitRepo` creates the workspace on demand instead of failing with an opaque
  "directory name is invalid" on the first build.

### Verified

black · isort · flake8 · mypy --strict · bandit -ll · **100% line and branch
coverage** (464 tests) · `python -m build` + `twine check` PASSED · wheel
installed into a clean venv and `looper --version` executed.

## [2.0.0] - 2026-08-08

Initial hardened release: package split, fail-closed scoring, cost budget,
sandboxed tests, anti-overfitting gate, scope guard. See ADR-001..007.
