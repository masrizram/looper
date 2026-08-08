# Architecture Decision Records

Short documents recording the *why* behind structural choices, so a future
reader (or a future us) does not undo a decision without knowing its reasons.

| ADR | Title | Status |
|---|---|---|
| [001](001-package-layout-and-immutable-config.md) | Package layout, immutable config, no import-time side effects | Accepted |
| [002](002-fail-closed-scoring.md) | Fail-closed scoring with hard release gates | Accepted |
| [003](003-timeouts-retry-classification-cost.md) | Timeouts, retry classification, and cost accounting | Accepted |
| [004](004-verified-evidence.md) | Verified evidence: syntax gate, loopback rule, cycle invalidation | Accepted |

## Format

Context (what forced the decision) → Decision (what we chose) → Consequences
(good and bad) → Alternatives rejected. Keep them short and concrete; cite real
code and real measurements rather than opinions.
