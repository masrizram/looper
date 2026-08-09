# 3 Example Prompts for Maximum Results

Three *goal* scenarios that exercise Looper's strongest features. Each scenario
ships with a `config.yaml` snippet (copy it into `config.yaml` before running)
and the exact commands. Run in PowerShell (Windows).

One-time prerequisites:

```powershell
.venv\Scripts\activate
$env:OPENROUTER_API_KEY = "sk-or-..."
looper --check-config     # make sure the config is valid
looper --check-models     # make sure every model slug is still on OpenRouter
looper --doctor           # check host isolation (exit 5 = host cannot sandbox)
```

---

## Prompt 1 — Multi-file production app + human-owned tests (max anti-overfit)

Exercises: **model tiering** (builder Sonnet, tester/reviewer Opus/GPT from a
different family), **`user_tests_dir`** (Looper rejects a build that fails your
tests the AI never sees), **`artifact_mode: package`** (multi-file output via
the `### FILE:` marker), **per-cycle git trail** for review (`git diff`), and a
**Docker sandbox** that is read-only with no network.

### `config.yaml` setup

```yaml
workspace: "./workspace"
execution:
  max_cycles: 5
  target_score: 99
  min_acceptable: 95
  sandbox_tests: true
  sandbox_backend: "docker"     # use "rlimit" if Docker is missing (POSIX)
  sandbox_image: "python:3.11-slim"
  sandbox_network: "none"
  sandbox_cpu_seconds: 60
  sandbox_wall_seconds: 300
  sandbox_rss_bytes: 1000000000
  sandbox_fail_closed: true
  artifact_mode: "package"      # let the builder emit a multi-file tree
  max_files_per_build: 25
  user_tests_dir: "./user_tests"   # Looper also runs THESE tests before "done"
  min_test_assertions_per_100_lines: 6
  git:
    enabled: true
    branch_prefix: "looper/"
    commit_per_cycle: true
    author_name: "looper"
    author_email: "looper@localhost"
  lint_generated: "flake8"
agents:
  researcher:  { model: "anthropic/claude-opus-5" }
  architect:   { model: "anthropic/claude-opus-5" }
  ux_api_designer: { model: "openai/gpt-5.6-sol" }
  builder:     { model: "anthropic/claude-sonnet-5" }
  tester:      { model: "anthropic/claude-opus-5" }     # different family from builder
  reviewer:    { model: "openai/gpt-5.6-terra" }        # different family from builder
  security_auditor: { model: "openai/gpt-5.6-sol" }
  performance_optimizer: { model: "anthropic/claude-sonnet-5" }
  documentation_writer: { model: "google/gemini-3.1-pro-preview" }
  fixer:       { model: "anthropic/claude-sonnet-5" }
```

### Human-owned tests (`./user_tests/test_api_contract.py`)

Create a `user_tests/` folder next to `config.yaml`, then add:

```python
# This test is NEVER seen or edited by the AI. Looper runs it as a second gate
# after the AI-authored tests. The build fails if this file is red.
import importlib.util, os, sys

SPEC = os.path.join(os.path.dirname(__file__), "..", "workspace")


def _load(name):
    # Find the built module (single_file or package) and import it dynamically.
    for root, _, files in os.walk(os.path.abspath(SPEC)):
        for f in files:
            if f == f"{name}.py":
                path = os.path.join(root, f)
                spec = importlib.util.spec_from_file_location(name, path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
    raise FileNotFoundError(f"{name} not found in workspace")


def test_crud_roundtrip():
    app = _load("generated_code")          # or "__init__" for a package
    assert hasattr(app, "create"), "create() endpoint is required"
    assert hasattr(app, "read"), "read() endpoint is required"
    rec = app.create({"title": "x"})
    assert rec["id"], "create() must return an id"
    assert app.read(rec["id"])["title"] == "x"


def test_no_hardcoded_secret():
    from pathlib import Path
    src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in Path(os.path.abspath(SPEC)).rglob("*.py")
    )
    assert "sk-" not in src, "do not hardcode OpenAI/API secrets"
    assert "password =" not in src.replace(" ", ""), "do not hardcode passwords"
```

### Goal prompt

```powershell
looper --goal "Build a Flask REST API for task management (full CRUD: create, read, update, delete) with JWT auth, per-IP rate limiting, and strict input validation. Emit it as a multi-file package using the ### FILE: marker (separate models, routes, auth, and the app factory). No hardcoded secrets; read them from environment variables. The code must pass the sandbox with no shell-out and must not open any external network."
```

Why it is maximal: the output is a structured package, not one file; the AI
knows there are human tests (`user_tests_dir`) that will expose overfitting;
every cycle is recorded on the `looper/*` git branch for `git diff`.

---

## Prompt 2 — Heavy workload that proves the sandbox is fail-closed

Exercises: **Docker sandbox with `--network=none` + cpu/wall caps**, the
**static scan** that refuses `os.system`/`subprocess`/`eval`, and the
**security_audit** that rejects CRITICAL findings (cap 50 → build fails, exit
3). The goal deliberately demands I/O work so the resource limits are truly
tested.

### `config.yaml` setup (override `execution` only)

```yaml
execution:
  sandbox_tests: true
  sandbox_backend: "docker"
  sandbox_network: "none"        # builder is NOT allowed internet access
  sandbox_cpu_seconds: 30        # an unbounded loop is killed here
  sandbox_wall_seconds: 120
  sandbox_rss_bytes: 500000000
  sandbox_fail_closed: true
  min_test_assertions_per_100_lines: 8
  lint_generated: "flake8"
```

### Goal prompt

```powershell
looper --goal "Build a local CSV processing pipeline: read ./in, normalize columns, compute aggregates, and write the result to ./out as Parquet. ALL I/O must be relative to the workspace (no absolute paths outside it, no network, no shell-out). Include tests that compare input rows vs output rows. The code must pass the sandbox with a tight CPU cap without calling subprocess or eval."
```

Why it is maximal: if the builder tries `os.system`/`socket`/`eval`, the static
scan **refuses to run** the tests (fail-closed, not silently passing); if it
tries to open the network, the `--network=none` container blocks it; a CRITICAL
finding from the security_auditor immediately knocks the score below 50.

---

## Prompt 3 — 24/7 daemon over the HTTP API + file watcher (production ops)

Exercises: **`--daemon`** (HTTP control plane + file watcher), **POST `/build`**
with a bearer token, **GET `/status`** (cycle/score/git branch), **GET
`/metrics`** (token spend, uptime), and **`looper_commands.txt`** as a
non-HTTP interface. Looper refuses to bind anything but loopback without a
token (ADR-004).

### Set the token + start the daemon

```powershell
$env:LOOPER_HTTP_TOKEN = python -c "import secrets;print(secrets.token_urlsafe(32))"
# note the token above; it goes in the Authorization header below
looper --daemon
```

### Send a goal over HTTP (PowerShell)

```powershell
$token = $env:LOOPER_HTTP_TOKEN
$body = '{"goal":"Build a CLI tool that finds duplicate files by SHA-256 hash in a directory, with --json and --min-size options. Include tests that compare two fixture directories. The code must pass the sandbox with no network."}'
Invoke-RestMethod -Uri http://127.0.0.1:9999/build `
  -Method POST -ContentType 'application/json' `
  -Headers @{ Authorization = "Bearer $token" } -Body $body
```

### Or via the file watcher (no HTTP)

```powershell
Add-Content -Path looper_commands.txt -Value "Build a password generator CLI with a configurable length, ambiguous-character exclusion, and deterministic output when a seed is given."
```

### Monitor

```powershell
# Full state: cycle, score breakdown, history, git branch
Invoke-RestMethod -Uri http://127.0.0.1:9999/status -Headers @{ Authorization = "Bearer $token" }
# Metrics: counters, uptime, token spend
Invoke-RestMethod -Uri http://127.0.0.1:9999/metrics -Headers @{ Authorization = "Bearer $token" }
```

Why it is maximal: not a single run, but a *control plane* that other CI/
orchestrators can trigger; all token usage and cycles are visible at
`/metrics`; the daemon refuses to bind `0.0.0.0` without a token, so it never
leaks to the LAN.

---

## Feature coverage by prompt

| Prompt | Tiering | user_tests | Package | Sandbox | Git trail | Daemon/HTTP |
|---|---|---|---|---|---|---|
| 1 | ✅ | ✅ | ✅ | Docker | ✅ | — |
| 2 | ✅ | (floor) | — | Docker none | — | — |
| 3 | ✅ | (floor) | — | auto | — | ✅ |

All prompts respect the release gate: a build below `min_acceptable` exits with
code `3`, over budget `4`, and a missing sandbox `5`.
