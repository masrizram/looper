"""Precision/recall for a binary gate.

Lives in the package rather than the test tree because the numbers are a
product property, not a test detail: ``looper`` claims its heuristic gates
are calibrated in both directions, and this is the arithmetic behind that
claim (ADR-014, ADR-017).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class GateMetrics:
    """Confusion matrix and its derived rates for one gate.

    ``positive`` means "the gate flagged it". A false positive is therefore a
    *legitimate* input the gate refused -- which costs exactly as much as a
    miss when the refusal is what stops every shopping-cart build from ever
    passing.
    """

    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int

    @property
    def total(self) -> int:
        return (
            self.true_positives + self.false_positives + self.true_negatives + self.false_negatives
        )

    @property
    def precision(self) -> float:
        """Of everything flagged, how much deserved it. 1.0 when nothing was."""
        flagged = self.true_positives + self.false_positives
        if flagged == 0:
            return 1.0
        return round(self.true_positives / flagged, 4)

    @property
    def recall(self) -> float:
        """Of everything that should have been flagged, how much was."""
        actual = self.true_positives + self.false_negatives
        if actual == 0:
            return 1.0
        return round(self.true_positives / actual, 4)

    @property
    def false_positive_rate(self) -> float:
        """Share of legitimate inputs the gate wrongly refused."""
        negatives = self.true_negatives + self.false_positives
        if negatives == 0:
            return 0.0
        return round(self.false_positives / negatives, 4)

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        if precision + recall == 0:
            return 0.0
        return round(2 * precision * recall / (precision + recall), 4)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "true_negatives": self.true_negatives,
            "false_negatives": self.false_negatives,
            "precision": self.precision,
            "recall": self.recall,
            "false_positive_rate": self.false_positive_rate,
            "f1": self.f1,
        }


def evaluate_gate(outcomes: Iterable[tuple[bool, bool]]) -> GateMetrics:
    """Tally ``(expected_flag, actual_flag)`` pairs into a confusion matrix."""
    tp = fp = tn = fn = 0
    for expected, actual in outcomes:
        if expected and actual:
            tp += 1
        elif expected and not actual:
            fn += 1
        elif not expected and actual:
            fp += 1
        else:
            tn += 1
    return GateMetrics(
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
    )
