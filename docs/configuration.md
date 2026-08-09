# Configuration & CLI

- [Config file](#config-file)
- [Environment variables](#environment-variables)
- [CLI reference](#cli-reference)
- [Exit codes](#exit-codes)
- [Notifications](#notifications)
- [Resuming an interrupted build](#resuming-an-interrupted-build)

---

## Config file

Everything lives in `config.yaml`; all fields are optional and anything omitted
uses the code default. Safeguard keys are documented in
[safeguards.md](safeguards.md#configuration-reference).

**Secrets never go in this file** — it stores only the *names* of environment
variables.

Validate before deploying:

```bash
looper --check-config     # schema, ranges, cross-field rules
looper --check-models     # every model slug is really served by OpenRouter
looper --doctor           # what isolation this host can actually deliver
```

Run all three in CI. They fail for different reasons: a malformed config, a
slug the provider retired, and a host that cannot sandbox.

---

## Environment variables

| Env var | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter credential (required for real runs) |
| `LOOPER_HTTP_TOKEN` | Bearer token for `/build`, `/status`, `/metrics` |

---

## CLI reference

| Flag | Effect |
|---|---|
| `--version` | Print version and exit |
| `--init [PATH]` | Write a minimal starter config (default `config.yaml`) and exit. Never overwrites an existing file |
| `--goal "..."` | Run one build, exit non-zero if below `min_acceptable` |
| `--dry-run` | Answer every agent from a local stub: no API key, no network, no spend. Lint, adequacy, sandbox and scoring still run for real, so the verdict is honest |
| `--report [PATH]` | Write a machine-readable run report (default `run_report.json`): verdict, score breakdown, spend per model, per-phase history. Appends a Markdown summary to `$GITHUB_STEP_SUMMARY` when that variable is set |
| `--daemon` | Run HTTP server + file watcher until signalled |
| `--check-config` | Validate config and exit |
| `--check-models` | Verify every configured model slug against the live OpenRouter catalogue, and exit |
| `--doctor` | Report the sandbox/git capability this host actually has, and exit |
| `--reset` | Clear persisted state |
| `--resume` | Skip `research`/`architecture` when the saved state shows they completed for this exact `--goal` and their artifacts survive (ADR-015) |
| `--config PATH` | Use a specific config file |
| `--json-logs` | Structured JSON logs for log shippers |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `2` | Config error (including an unknown model slug) |
| `3` | Build scored below `min_acceptable` |
| `4` | Cost budget exceeded |
| `5` | Sandbox unavailable and `sandbox_fail_closed` is on |
| `6` | Provider returned 402 (account out of credits) |
| `130` | Interrupted |

Codes are stable and meant to be branched on in CI.

---

## Notifications

Off by default. When `notifications.webhook_url` is set, Looper POSTs a JSON
body once per build, on terminal outcomes only.

```yaml
notifications:
  webhook_url: "https://hooks.slack.com/services/..."
  on_status: ["passed", "below_minimum", "cost_exhausted", "out_of_credits", "failed"]
  timeout_seconds: 10
  headers: {}          # e.g. an auth header for a private endpoint
```

| Key | Default | Notes |
|---|---|---|
| `webhook_url` | `""` (disabled) | must be `http://` or `https://` |
| `on_status` | all five terminal statuses | unknown values are rejected at load |
| `timeout_seconds` | `10.0` | range `0.1`–`300` |
| `headers` | `{}` | merged over `Content-Type: application/json` |

Payload:

```json
{
  "text": "[looper] below_minimum: score 78.00 after 3 cycle(s), $2.4100 spent - <goal>",
  "status": "below_minimum",
  "goal": "...",
  "score": 78.0,
  "cycle": 3,
  "cost_usd": 2.41,
  "detail": null
}
```

The `text` field is what Slack, Discord and Mattermost render. Delivery
failures are logged and swallowed: **a webhook can never turn a passing build
red**, and a notification is never retried (a duplicate alert is worse than a
missed one for a terminal event).

Non-terminal states (`running`) are never sent.

---

## Resuming an interrupted build

```bash
looper --resume --goal "<exactly the same goal>"
```

Skips `research` and `architecture` — the two frontier-tier calls that produce
no score — when the saved state proves they finished for this goal and their
artifacts are still on disk and non-empty.

Everything scored (`build`, `test`, `review`, `security_audit`) **always**
re-runs. A resume that could restore a score would let a stale gate result pass
as a fresh one; see [ADR-015](adr/015-resume-only-unscored-phases.md).

The skip is cancelled — silently and safely — by any of: a different goal, a
previous run that already completed, a wiped workspace, or an empty artifact.
Without `--resume` the checkpoint is ignored entirely.
