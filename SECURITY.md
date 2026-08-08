# Security Policy

## The core risk

Looper executes code written by a large language model. `POST /build` starts a
pipeline that **writes files to disk and runs `pytest` over generated code**.
Treat the HTTP endpoint as remote code execution by design.

## Deployment rules

1. **Bind to loopback.** The default is `127.0.0.1`. Binding to `0.0.0.0`
   without an auth token is refused at startup, not merely warned about.
2. **Always set a token when exposing the port.**
   ```bash
   export LOOPER_HTTP_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
   ```
   Tokens are compared with `hmac.compare_digest` (constant time).
3. **Run as an unprivileged user**, ideally inside a container with no host
   mounts beyond the workspace and no cloud credentials in the environment.
4. **Never put secrets in `config.yaml`.** The file holds only the *names* of
   environment variables (`api_key_env`), never values.

## Known residual risk

Generated tests run via `subprocess` with `-I` (isolated), `-B` (no bytecode),
`-p no:cacheprovider`, and a hard timeout — but still with the daemon user's
privileges. **A container or `nsjail` boundary is still recommended** and is
tracked as the top item on the hardening roadmap.

## Built-in protections

| Control | Where |
|---|---|
| Constant-time token comparison | `looper/server.py` |
| Per-IP rate limiting | `looper/server.py` (`RateLimiter`) |
| Request body + goal length caps | `HTTPConfig.max_body_bytes` / `max_goal_length` |
| Workspace path-traversal containment | `PhaseManager._resolve_in_workspace` |
| Subprocess timeout + isolation | `PhaseManager.run_test` |
| Refuse public bind without auth | `HTTPConfig.__post_init__` |
| Atomic state writes, bounded history | `looper/state.py` |
| `/health` leaks nothing; `/status` and `/metrics` require auth | `looper/server.py` |

## Reporting a vulnerability

Open a **private** security advisory on GitHub rather than a public issue.
Please include a reproduction and the commit hash. Expect an acknowledgement
within 72 hours.

## Automated scanning

Every push runs `bandit -r looper/ daemon.py -ll` and
`pip-audit -r requirements.txt --strict` in CI. Both must be clean to merge.
