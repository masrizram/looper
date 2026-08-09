"""Calibration gate: measure the heuristics in BOTH directions (ADR-017).

Every defect found by audits v4 and v5 lived under 100% line and branch
coverage. Coverage proves execution, never correctness. These tests measure
*verdicts* against a corpus of labelled inputs written from outside the
implementation, and fail when either direction regresses -- a fix that stops
missing dangerous code by refusing every legitimate suite is not a fix.
"""

from __future__ import annotations

import pytest

from looper.adequacy import evaluate_suite
from looper.calibration import evaluate_gate
from looper.sandbox import scan_for_dangerous_calls
from tests.corpus import ADEQUACY_SAMPLES, SANDBOX_SAMPLES, Sample

#: The generated build writes ``src/generated_code.py``, so this is what
#: ``_subject_modules()`` hands the gate in a real run.
SUBJECT_MODULES = frozenset({"generated_code"})

#: Thresholds. Recall is held higher than precision on the sandbox gate
#: because a missed dangerous call executes on the host, while a false
#: positive only costs a build. Both floors are real: at the time of writing
#: both gates score 1.0/1.0, so any regression in either direction fails.
MIN_RECALL = 0.95
MIN_PRECISION = 0.90
MAX_FALSE_POSITIVE_RATE = 0.10


def _adequacy_flags(sample: Sample) -> bool:
    """True when the adequacy gate REFUSES the sample."""
    report = evaluate_suite(
        sample.source,
        min_assertions_per_100_lines=6,
        subject_modules=SUBJECT_MODULES,
    )
    return not report.ok


def _sandbox_flags(sample: Sample) -> bool:
    """True when the sandbox tripwire REFUSES the sample."""
    return bool(scan_for_dangerous_calls(sample.source))


@pytest.mark.parametrize("sample", ADEQUACY_SAMPLES, ids=lambda s: s.name)
def test_adequacy_verdict_matches_label(sample: Sample) -> None:
    actual = _adequacy_flags(sample)
    assert actual == sample.should_flag, (
        f"adequacy gate {'refused' if actual else 'accepted'} {sample.name!r}; "
        f"expected {'refuse' if sample.should_flag else 'accept'} "
        f"({sample.rationale})"
    )


@pytest.mark.parametrize("sample", SANDBOX_SAMPLES, ids=lambda s: s.name)
def test_sandbox_verdict_matches_label(sample: Sample) -> None:
    actual = _sandbox_flags(sample)
    assert actual == sample.should_flag, (
        f"sandbox scanner {'refused' if actual else 'allowed'} {sample.name!r}; "
        f"expected {'refuse' if sample.should_flag else 'allow'} "
        f"({sample.rationale})"
    )


def test_adequacy_gate_meets_calibration_thresholds() -> None:
    metrics = evaluate_gate(
        (sample.should_flag, _adequacy_flags(sample)) for sample in ADEQUACY_SAMPLES
    )
    assert metrics.total == len(ADEQUACY_SAMPLES)
    assert metrics.recall >= MIN_RECALL, metrics.as_dict()
    assert metrics.precision >= MIN_PRECISION, metrics.as_dict()
    assert metrics.false_positive_rate <= MAX_FALSE_POSITIVE_RATE, metrics.as_dict()


def test_sandbox_gate_meets_calibration_thresholds() -> None:
    metrics = evaluate_gate(
        (sample.should_flag, _sandbox_flags(sample)) for sample in SANDBOX_SAMPLES
    )
    assert metrics.total == len(SANDBOX_SAMPLES)
    assert metrics.recall >= MIN_RECALL, metrics.as_dict()
    assert metrics.precision >= MIN_PRECISION, metrics.as_dict()
    assert metrics.false_positive_rate <= MAX_FALSE_POSITIVE_RATE, metrics.as_dict()


def test_corpus_covers_both_directions() -> None:
    """A corpus of only-positives would pass every threshold vacuously."""
    for name, samples in (("adequacy", ADEQUACY_SAMPLES), ("sandbox", SANDBOX_SAMPLES)):
        positives = sum(1 for s in samples if s.should_flag)
        negatives = len(samples) - positives
        assert positives >= 4, f"{name} corpus needs real positives"
        assert negatives >= 4, f"{name} corpus needs real negatives"


def test_sample_names_are_unique() -> None:
    names = [s.name for s in (*ADEQUACY_SAMPLES, *SANDBOX_SAMPLES)]
    assert len(names) == len(set(names))
