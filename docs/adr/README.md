# Architecture Decision Records

Short documents recording the *why* behind structural choices, so a future
reader (or a future us) does not undo a decision without knowing its reasons.

| ADR | Title | Status |
|---|---|---|
| [001](001-package-layout-and-immutable-config.md) | Package layout, immutable config, no import-time side effects | Accepted |
| [002](002-fail-closed-scoring.md) | Fail-closed scoring with hard release gates | Accepted |
| [003](003-timeouts-retry-classification-cost.md) | Timeouts, retry classification, and cost accounting | Accepted |
| [004](004-verified-evidence.md) | Verified evidence: syntax gate, loopback rule, cycle invalidation | Accepted |
| [005](005-cost-budget.md) | Hard USD cost ceiling per build | Accepted |
| [006](006-sandbox-and-anti-overfit.md) | Anti-overfitting: human-owned tests and the adequacy gate | Accepted |
| [007](007-scope-guard.md) | Scope guard against context rot | Accepted |
| [008](008-cross-platform-sandbox-fail-closed.md) | Sandboxed execution of generated code | Accepted |
| [009](009-multi-file-artifacts.md) | Multi-file artifacts and the path allowlist | Accepted |
| [010](010-git-integration.md) | Per-cycle git commit trail | Accepted |
| [011](011-split-phases-by-responsibility.md) | Split phases.py by responsibility | Accepted |
| [012](012-calibrate-heuristics-both-directions.md) | Calibrate heuristics in both directions | Accepted |
| [013](013-reserve-budget-before-the-call.md) | Reserve the budget before the call, not after it | Accepted |
| [014](014-measure-gates-in-both-directions.md) | Gates must be measured in both directions | Accepted |
| [015](015-resume-only-unscored-phases.md) | Resume may skip only unscored phases | Accepted |
| [016](016-wsl-as-a-third-sandbox-backend.md) | WSL as a third sandbox backend | Accepted |
| [017](017-labelled-corpus-for-heuristic-gates.md) | A labelled corpus, not coverage, calibrates the heuristic gates | Accepted |
| [018](018-parallelise-independent-phases.md) | Parallelise only phases that read nothing their co-runner writes | Accepted |

## Format

Context (what forced the decision) → Decision (what we chose) → Consequences
(good and bad) → Alternatives rejected. Keep them short and concrete; cite real
code and real measurements rather than opinions.
