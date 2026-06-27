"""Tests for the coupling effective-parameter fold (pure function)."""

from custom_components.vtherm_smartpi.smartpi.room_coupling import (
    compute_effective_params,
)


def test_identity_when_no_open_edge():
    """No coupling load -> identity (b_eff == b, Text_eff == text)."""
    assert compute_effective_params(0.01, 5.0, 0.0, 0.0) == (0.01, 5.0)


def test_single_open_edge_exact_fold():
    """One open edge collapses to (b + k, blended Text)."""
    b, text, k, t_j = 0.01, 5.0, 0.004, 22.0
    b_eff, text_eff = compute_effective_params(b, text, k, k * t_j)
    assert abs(b_eff - (b + k)) < 1e-12
    assert abs(text_eff - ((b * text + k * t_j) / (b + k))) < 1e-12
    # Text_eff is pulled from the outdoor temp toward the warmer neighbour.
    assert text < text_eff < t_j


def test_text_none_passthrough():
    """A missing outdoor temp keeps Text None but still raises b_eff."""
    b_eff, text_eff = compute_effective_params(0.01, None, 0.004, 0.088)
    assert abs(b_eff - 0.014) < 1e-12
    assert text_eff is None


def test_negative_sum_k_clamped():
    """Defensive: a negative load never lowers b below the base."""
    b_eff, text_eff = compute_effective_params(0.01, 5.0, -1.0, 0.0)
    assert b_eff == 0.01
    assert text_eff == 5.0
