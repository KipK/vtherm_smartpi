"""Tests for the bounded learning window outcome counters."""
from custom_components.vtherm_smartpi.smartpi.learning_outcomes import (
    LEARNING_OUTCOMES_PERIOD_S,
    LEARNING_OUTCOMES_TOP_N,
    LearningOutcomes,
    normalise_reason,
)


def test_reason_normalisation_drops_values():
    assert normalise_reason("skip: power instability (CV=0.42, too_few_jumps)") == (
        "skip: power instability"
    )
    assert normalise_reason("skip: collecting a meas (3/6)") == "skip: collecting a meas"
    assert normalise_reason("skip: B flywheel (-0.20°C), sliding start") == (
        "skip: B flywheel , sliding start"
    )
    assert normalise_reason("pool: learned a (weighted_median)") == "pool: learned a"


def test_summary_counts_per_parameter_and_expires():
    out = LearningOutcomes()
    out.record(0.0, "a", "skip: power instability (CV=0.5, too_few_jumps)")
    out.record(10.0, "a", "skip: power instability (CV=0.9, insufficient_samples)")
    out.record(20.0, "b", "skip: deadtime window")
    summary = out.summary(30.0)
    assert summary["a"] == [["skip: power instability", 2]]
    assert summary["b"] == [["skip: deadtime window", 1]]

    later = out.summary(15.0 + LEARNING_OUTCOMES_PERIOD_S)
    assert later["a"] == []
    assert later["b"] == [["skip: deadtime window", 1]]


def test_summary_is_bounded_to_the_most_frequent_reasons():
    out = LearningOutcomes()
    for i in range(LEARNING_OUTCOMES_TOP_N + 3):
        for _ in range(i + 1):
            out.record(0.0, "a", f"reason_{chr(97 + i)}")
    summary = out.summary(1.0)["a"]
    assert len(summary) == LEARNING_OUTCOMES_TOP_N
    assert summary[0][1] >= summary[-1][1]


def test_clear_forgets_outcomes():
    out = LearningOutcomes()
    out.record(0.0, "b", "learned b")
    out.clear()
    assert out.summary(1.0) == {"a": [], "b": []}
