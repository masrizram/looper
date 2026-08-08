# ADR-010: Git integration for reviewability

Status: accepted
Date: 2026-08-08

## Context

ADR-006 lists reviewability as a control, but the only artifact a build left
behind was a workspace directory that the next build overwrote. There was no
way to see what changed between cycle 1 and cycle 3, and no way to review LLM
output using the tooling humans already use for code review.

## Decision

Optional git integration under `execution.git` (default **off**):

```yaml
execution:
  git:
    enabled: true
    branch_prefix: "looper/"
    commit_per_cycle: true
    author_name: "looper"
    author_email: "looper@localhost"
```

* Each build checks out `<prefix><slug-of-goal>` and commits once per cycle
  with the score and its breakdown in the message, plus a final commit.
* The goal is attacker-influenced text, so `slugify_goal` is an **allowlist**:
  lowercase alphanumerics joined by `-`, length-capped, empty → `build`. A goal
  such as `--upload-pack=evil` or `refs/heads/main..HEAD` therefore cannot
  reach a git refspec as-is.
* Every git call is a fixed argv (never `shell=True`) with an explicit
  `-c user.name/-c user.email`, so it does not depend on host git config.
* **All git failures are non-fatal.** Version control here is an observability
  feature; a missing binary, a failed checkout, or "nothing to commit" degrades
  to "no commits recorded" and never fails a build.
* The branch and commit list are exposed in state, so `/status` reports where
  the artifact went.

## Consequences

`git log --oneline` on the workspace becomes the build's audit trail, and
`git diff HEAD~1` shows exactly what a fix cycle changed — the reviewability
gap ADR-006 identified but did not close.
