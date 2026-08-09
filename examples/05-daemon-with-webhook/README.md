# 05 - Daemon with webhook notifications

Unattended mode. Without notifications the only way to learn that a 03:00
build ran out of credits is to poll `/status`.

```bash
export OPENROUTER_API_KEY=sk-or-...
export LOOPER_HTTP_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
looper --config examples/05-daemon-with-webhook/config.yaml --daemon
```

## Two ways to trigger a build

```bash
# HTTP
curl -X POST http://127.0.0.1:9999/build \
     -H "Authorization: Bearer $LOOPER_HTTP_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"goal": "build a token bucket rate limiter"}'

# File watcher
echo "build a token bucket rate limiter" >> looper_commands.txt
```

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /build` | queue a build (auth required) |
| `GET /status` | current phase, cycle, score, last 20 history entries |
| `GET /health` | liveness |
| `GET /metrics` | token usage, call count, cost, cost per model |

## The notification payload

```json
{
  "text": "[looper] out_of_credits: score 0.00 after 1 cycle(s), $0.4210 spent - build a token bucket rate limiter | ...",
  "status": "out_of_credits",
  "goal": "build a token bucket rate limiter",
  "score": 0.0,
  "cycle": 1,
  "cost_usd": 0.421,
  "detail": "OpenRouter 402 Payment Required: account out of credits."
}
```

Fired only on terminal states (`passed`, `below_minimum`, `cost_exhausted`,
`out_of_credits`, `failed`). Narrow the list with `on_status` if a passing
build is not news.

## Security notes

* The bind stays on `127.0.0.1`. Binding anything else **without** an auth
  token is rejected at startup, not merely warned about.
* `POST /build` runs code an LLM wrote. Never expose it to an untrusted
  network, and never disable the sandbox to make a build "work".
* The webhook URL in this file is a placeholder. It is committed to the repo --
  substitute it from your secret store in production.
