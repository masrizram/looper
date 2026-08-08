# Configuration & CLI

- [Config file](#config-file)
- [Environment variables](#environment-variables)
- [CLI reference](#cli-reference)
- [Exit codes](#exit-codes)

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
| `--goal "..."` | Run one build, exit non-zero if below `min_acceptable` |
| `--daemon` | Run HTTP server + file watcher until signalled |
| `--check-config` | Validate config and exit |
| `--check-models` | Verify every configured model slug against the live OpenRouter catalogue, and exit |
| `--doctor` | Report the sandbox/git capability this host actually has, and exit |
| `--reset` | Clear persisted state |
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
| `130` | Interrupted |

Codes are stable and meant to be branched on in CI.
