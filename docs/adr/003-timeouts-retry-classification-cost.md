# ADR-003: Timeouts, retry classification, and cost accounting

**Status:** Accepted · **Date:** 2026-08-08

## Context

A post-v2.0 review of our own code found three gaps, each confirmed by a probe
script before any change was made.

**1. The LLM call had no timeout.** v2.0 gave the pytest subprocess a hard
timeout (audit finding H-5) but left the network call unbounded. A probe
against a server that accepts the connection and then goes silent hung past
the observation window with no internal limit. In production one stalled
connection wedges a phase, and therefore the whole 24/7 daemon, forever. The
fix for H-5 was applied to the wrong half of the problem.

**2. Every error was retried, including ones that can never succeed.** A probe
raising `401 Invalid API key` consumed all three attempts and six seconds of
backoff. Across ten phases that is thirty pointless requests per build, each
eating rate-limit headroom and delaying the operator's feedback about a
misconfigured key.

**3. Nothing tracked token spend.** `response.usage` was never read. An
unattended daemon could exhaust a credit balance with no signal at all.

## Decision

**Wrap every call in `asyncio.wait_for`** with
`openrouter.request_timeout_seconds` (default 300s, validated to 1–3600).
Timeouts *are* retried — a stall may be transient — and surface as
`AgentReply.timed_out` so callers can distinguish them from refusals.

**Classify errors before retrying.** `NON_RETRYABLE_STATUSES` =
`{400, 401, 403, 404, 405, 422}` fail after one attempt. `429` remains
retryable, because backing off is exactly the correct response to rate
limiting. An unknown or unparseable error is assumed transient: a bare socket
error is far more likely to be a network blip than a permanent refusal.
Status is read from `status_code` / `status` / `http_status` /
`response.status_code`, falling back to a regex over the message text because
the SDK does not always attach a structured code.

**Record `response.usage`** per call and cumulatively on the client. Totals
are exposed at `/status` (`token_usage`, `llm_calls`) and persisted into state
at the end of each build. Providers that omit `usage` degrade to zero rather
than crashing.

**Cap file writes** at `execution.max_file_bytes` (default 2 MB). Over-long
agent output is *truncated with a visible marker* rather than rejected: a
partial artifact still feeds the next phase, and the marker tells both the
reviewer agent and a human what happened. Truncation decodes with
`errors="ignore"` so slicing never splits a UTF-8 codepoint.

## Consequences

*Positive.* No single call can hang the daemon. A bad API key now fails in
one attempt instead of thirty. Operators can see token spend without an
OpenRouter dashboard. A looping agent cannot fill the disk.

*Negative.* Timeout tuning is now an operational concern — a 300s default may
be too aggressive for very large `max_tokens` on a slow model. The status
regex is heuristic and could in principle misread a status out of prose; the
failure mode is a retry that would have happened under the old code anyway.

## Alternatives rejected

- **Rely on the SDK's own `timeout=`** — it varies by SDK version and does not
  cover the whole await; `wait_for` is explicit and testable.
- **Reject oversized files outright** — throws away work the next phase could
  still use, and makes a slightly chatty agent fatal.
- **A hard token budget that aborts a build** — deferred: it needs per-model
  pricing to be meaningful. Measurement first, enforcement later.
