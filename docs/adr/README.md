# Architecture Decision Records

Short documents recording the *why* behind structural choices, so a future
reader (or a future us) does not undo a decision without knowing its reasons.

| ADR | Title | Status |
|---|---|---|
| [001](001-package-layout-and-immutable-config.md) | Package layout, immutable config, no import-time side effects | Accepted |
| [002](002-fail-closed-scoring.md) | Fail-closed scoring with hard release gates | Accepted |
| [003](003-timeouts-retry-classification-cost.md) | Timeouts, retry classification, and cost accounting | Accepted |
| [004](004-verified-evidence.md) | Verified evidence: syntax gate, loopback rule, cycle invalidation | Accepted |
| [005](005-cost-ceiling.md) | Hard USD cost ceiling per build | Accepted |
| [006](006-anti-overfitting.md) | Anti-overfitting: human-owned tests and the adequacy gate | Accepted |
| [007](007-scope-guard.md) | Scope guard against context rot | Accepted |
| [008](008-sandboxed-execution.md) | Sandboxed execution of generated code | Accepted |
| [009](009-multi-file-artifacts.md) | Multi-file artifacts and the path allowlist | Accepted |
| [010](010-git-integration.md) | Per-cycle git commit trail | Accepted |
| [011](011-split-phases-by-responsibility.md) | Split phases.py by responsibility | Accepted |
| [012](012-calibrate-heuristics-both-directions.md) | Calibrate heuristics in both directions | Accepted |

## Format

Context (what forced the decision) → Decision (what we chose) → Consequences
(good and bad) → Alternatives rejected. Keep them short and concrete; cite real
code and real measurements rather than opinions.
