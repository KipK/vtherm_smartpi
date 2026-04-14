"""Eligibility helpers for FF3 disturbance-only activation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FF3DisturbanceContext:
    """Summarize whether FF3 may run in a disturbance-recovery context."""

    eligible: bool
    disturbance_active: bool
    reason: str
    disturbance_kind: str
    residual_persistent: bool
    dynamic_coherent: bool
    model_reliable: bool
    bias_warning: bool
    external_gain_detected: bool
    external_loss_detected: bool
    perturbation_dtdt_sign: str
    measured_slope_sign: str


def _sign_label(value: float | None) -> str:
    """Return a stable sign label for diagnostics."""
    if value is None:
        return "none"
    if value > 0.0:
        return "positive"
    if value < 0.0:
        return "negative"
    return "none"


def _safe_float(value) -> float | None:
    """Return a float when possible, otherwise None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _classify_disturbance_from_twin(
    *,
    bias_warning: bool,
    external_gain_detected: bool,
    external_loss_detected: bool,
    perturbation_dtdt: float | None,
) -> str:
    """Classify the likely disturbance sign from the twin residual."""
    if external_gain_detected and not external_loss_detected:
        return "gain"
    if external_loss_detected and not external_gain_detected:
        return "loss"
    if not bias_warning:
        return "none"

    perturbation_sign = _sign_label(perturbation_dtdt)
    if perturbation_sign == "positive":
        return "gain"
    if perturbation_sign == "negative":
        return "loss"
    return "none"


def _is_dynamic_coherence_valid(
    *,
    disturbance_kind: str,
    perturbation_dtdt: float | None,
    measured_slope_h: float | None,
) -> bool:
    """Validate that the thermal dynamic matches the disturbance sign."""
    expected_sign = {
        "gain": "positive",
        "loss": "negative",
    }.get(disturbance_kind)

    if expected_sign is None:
        return False

    available_signs = {
        _sign_label(perturbation_dtdt),
        _sign_label(measured_slope_h),
    }
    return expected_sign in available_signs


def get_ff3_twin_unavailability_reason(twin_diag: dict) -> str:
    """Return the FF3 twin gating reason derived from the twin diagnostics."""
    twin_status = str(twin_diag.get("status", "unavailable"))
    if twin_status != "ok":
        return "twin_unavailable"
    if twin_diag.get("warming_up") is True:
        return "twin_warming_up"
    if twin_diag.get("model_reliable") is not True:
        return "twin_not_reliable"
    return "none"


def build_ff3_disturbance_context(
    *,
    twin_diag: dict,
    measured_slope_h: float | None,
    trajectory_active: bool,
    trajectory_source: str,
) -> FF3DisturbanceContext:
    """Build the FF3 disturbance-only eligibility context."""
    twin_status = str(twin_diag.get("status", "unavailable"))
    twin_reason = get_ff3_twin_unavailability_reason(twin_diag)
    model_reliable = twin_status == "ok" and twin_diag.get("model_reliable") is True
    warming_up = twin_diag.get("warming_up") is True
    bias_warning = twin_diag.get("bias_warning") is True
    external_gain_detected = twin_diag.get("external_gain_detected") is True
    external_loss_detected = twin_diag.get("external_loss_detected") is True
    perturbation_dtdt = _safe_float(twin_diag.get("perturbation_dTdt"))
    measured_slope_h = _safe_float(measured_slope_h)

    residual_persistent = (
        bias_warning
        or external_gain_detected
        or external_loss_detected
    )
    disturbance_kind = _classify_disturbance_from_twin(
        bias_warning=bias_warning,
        external_gain_detected=external_gain_detected,
        external_loss_detected=external_loss_detected,
        perturbation_dtdt=perturbation_dtdt,
    )
    dynamic_coherent = _is_dynamic_coherence_valid(
        disturbance_kind=disturbance_kind,
        perturbation_dtdt=perturbation_dtdt,
        measured_slope_h=measured_slope_h,
    )

    if trajectory_active and trajectory_source == "setpoint":
        reason = "trajectory_setpoint_active"
    elif twin_reason != "none":
        reason = twin_reason
    elif not residual_persistent:
        reason = "residual_not_persistent"
    elif disturbance_kind == "none":
        reason = "disturbance_unclassified"
    elif not dynamic_coherent:
        reason = "dynamic_incoherent"
    else:
        reason = "none"

    disturbance_active = reason == "none"
    return FF3DisturbanceContext(
        eligible=disturbance_active,
        disturbance_active=disturbance_active,
        reason=reason,
        disturbance_kind=disturbance_kind,
        residual_persistent=residual_persistent,
        dynamic_coherent=dynamic_coherent,
        model_reliable=model_reliable,
        bias_warning=bias_warning,
        external_gain_detected=external_gain_detected,
        external_loss_detected=external_loss_detected,
        perturbation_dtdt_sign=_sign_label(perturbation_dtdt),
        measured_slope_sign=_sign_label(measured_slope_h),
    )
