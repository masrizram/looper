# Artifacts & git review trail

Where a build's output goes, and how to review it with ordinary tooling.

- [Multi-file artifacts](#multi-file-artifacts)
- [Git integration](#git-integration)

---

## Multi-file artifacts

By default a build produces one `src/generated_code.py`. That can only ever be
a toy, so set `execution.artifact_mode: package` and the builder may emit a
file tree using an explicit marker:

    ### FILE: src/app/models.py
    ```python
    class User: ...
    ```

Rules — all fail-closed, because the marker text derives from an
attacker-influenceable goal:

* Paths go through an **allowlist**: relative only, no `..`, no drive letters,
  no NUL, and a known extension (`.py .md .txt .toml .cfg .ini .json .yaml .yml`).
* The file count is capped by `max_files_per_build`, so a looping agent cannot
  fill an unattended daemon's disk.
* **Every** `.py` file must parse and pass the lint gate. `build_ok` continues
  to mean *the code parses*.
* Reviewer and security agents receive **all** modules concatenated. Auditing
  only the first file would let vulnerabilities in the rest through.
* No markers in the reply → falls back to single-file. Enabling package mode
  can never break a previously working build.

See [ADR-009](adr/009-multi-file-artifacts.md).

---

## Git integration

```yaml
execution:
  git:
    enabled: true
    branch_prefix: "looper/"
    commit_per_cycle: true
```

Each build checks out `looper/<slug-of-goal>` and commits once per cycle with
the score breakdown in the message. `git log --oneline` becomes the build's
audit trail, and `git diff HEAD~1` shows exactly what a fix cycle changed.

Two properties worth knowing:

* The goal is attacker-influenced text, so it is slugified through an
  allowlist before it can reach a git ref (`--upload-pack=evil` →
  `upload-pack-evil`). Every git call uses a fixed argv, never `shell=True`.
* **Every git failure is non-fatal.** Version control here is observability,
  not correctness — a missing binary or a failed checkout degrades to "no
  commits recorded" and never fails a build.

See [ADR-010](adr/010-git-integration.md).
