# Operating the daemon

Triggering builds, the HTTP control plane, and the resilience behaviour behind
them.

- [Triggering builds](#triggering-builds)
- [HTTP API](#http-api)
- [Resilience & cost control](#resilience--cost-control)

---

## Triggering builds

**File watcher** — write a goal into `looper_commands.txt`:

```bash
echo "build a URL shortener" >> looper_commands.txt
```

**HTTP API** — available in `--daemon` mode.

---

## HTTP API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/build` | token | Start a build: `{"goal": "..."}` |
| GET | `/status` | token | Full state: cycle, score, breakdown, history, git branch |
| GET | `/health` | none | Liveness; leaks nothing |
| GET | `/metrics` | token | Counters, uptime, token spend |

```bash
export LOOPER_HTTP_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
curl -X POST localhost:9999/build \
     -H "Authorization: Bearer $LOOPER_HTTP_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"goal":"build a URL shortener"}'
```

Protections: constant-time token comparison, per-IP rate limiting, request body
and goal-length caps, and a startup **refusal** to bind any non-loopback
address (`0.0.0.0`, `::`, or a LAN address) without a token
([ADR-004](adr/004-verified-evidence.md)).

---

## Resilience & cost control

Every LLM call has a hard timeout (`openrouter.request_timeout_seconds`), so a
stalled connection cannot wedge the daemon.

Errors are **classified** before retrying: a `401` fails after one attempt
instead of burning the whole budget, while `429` and `5xx` back off
exponentially. Retrying an auth failure is just paying to be rejected twice.

Token usage is accumulated per call and reported at `/status` and `/metrics`.
Files written from agent output are capped by `execution.max_file_bytes`.

See [ADR-003](adr/003-timeouts-retry-classification-cost.md).
