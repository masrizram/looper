# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

* **Default agent roster re-tiered to current models.** Every slug was verified
  against the live OpenRouter catalogue; four of the previously shipped slugs
  (`anthropic/claude-3.5-sonnet`, `google/gemini-pro-1.5`) no longer exist and
  would have failed mid-build. Reasoning phases (research, architecture, test
  design) now use Opus 5, coding uses Sonnet 5, security uses GPT-5.6 Sol,
  documentation uses Gemini 3.1 Pro.
* **Reviewer moved off the builder's model family** (`openai/gpt-5.6-terra`).
  A reviewer from the same family as the author shares its blind spots, which
  weakens the independence ADR-006 depends on.
* **Real prices shipped for the default roster.** Cost estimation previously
  fell back to a flat $0.002/1K guess, under-reporting Opus spend ~7x and
  making `max_cost_usd` (ADR-005) a budget in name only. User-supplied prices
  are merged over the defaults rather than replacing them.

### Added

* **`looper --check-models`** — verifies every configured slug against
  `GET /api/v1/models` and exits `2` on an unknown one. A bad slug is
  well-formed YAML, so `--check-config` cannot catch it; it used to surface
  mid-pipeline after earlier phases had already been billed. An unreachable
  catalogue warns and passes, so an offline machine is not reported as broken.
* **CI job `models`** runs `--check-models` on every push/PR and on the weekly
  cron, so a slug retired by the provider turns the build red on its own
  instead of being discovered by a paid, half-finished build.

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
