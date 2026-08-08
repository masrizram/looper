# ADR-009: Multi-file artifacts

Status: accepted
Date: 2026-08-08

## Context

The builder phase wrote exactly one file, `src/generated_code.py`. The README
claimed the pipeline "turns a one-sentence goal into working software", but a
single module can only ever be a toy — any real artifact has a package layout,
and the reviewer and security agents were auditing something no real project
would look like.

## Decision

Add `execution.artifact_mode: single_file | package` (default `single_file`,
so behaviour is unchanged unless opted into).

In `package` mode the builder may emit a file tree using an explicit marker:

```
### FILE: src/app/models.py
```python
...
```
```

Rules, all fail-closed because the marker text is attacker-influenced
(`POST /build` accepts any goal, and the model output derives from it):

* Paths go through an **allowlist**: relative only, no `..` segments, no drive
  letters, no NUL, and a suffix in `{.py,.md,.txt,.toml,.cfg,.ini,.json,.yaml,.yml}`.
  Rejected paths are dropped with a warning, never written.
* The existing workspace containment sink (`resolve_in_workspace`) still
  applies as defence in depth; a path that reaches it and escapes fails the
  build.
* `execution.max_files_per_build` (default 25) caps the tree so a looping agent
  cannot fill an unattended daemon's disk.
* **Every** `.py` artifact must parse, and each is passed through the existing
  lint gate. `build_ok` continues to mean *the code parses*.
* The concatenation of all Python modules is written to `CODE_FILE`, so the
  reviewer and security agents see every module. Auditing only the first file
  would let vulnerabilities in the rest pass unexamined.
* No markers in the reply → fall back to the single-file path. Enabling
  package mode can never make a previously-working build stop producing code.
