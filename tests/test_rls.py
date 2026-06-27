"""Tests for the joint multi-edge recursive least squares engine."""

import math

from custom_components.vtherm_smartpi.smartpi.rls import MultiEdgeRLS


def _rls(**kw):
    defaults = dict(p0=10.0, lam=0.995, p_max=50.0, huber_c=0.2,
                    theta_min=0.0, theta_max=0.5)
    defaults.update(kw)
    return MultiEdgeRLS(**defaults)


def test_ensure_and_accessors():
    rls = _rls()
    assert rls.value("A") == 0.0
    assert math.isinf(rls.variance("A"))
    rls.ensure_edge("A")
    assert rls.value("A") == 0.0
    assert rls.variance("A") == 10.0
    assert rls.edge_ids() == ["A"]


def test_drop_missing():
    rls = _rls()
    rls.ensure_edge("A")
    rls.ensure_edge("B")
    rls.drop_missing({"A"})
    assert rls.edge_ids() == ["A"]
    assert math.isinf(rls.variance("B"))
