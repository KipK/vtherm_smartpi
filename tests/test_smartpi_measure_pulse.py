"""Steps of the measurement pulses that the pulses of the learning stage and those of a calibration share: the heating
step lasts until the room is back near the setpoint (for 30 minutes at most once the rise is seen), the regulation takes over a stop only once the room is deeper (or
at the cap). The sequences are driven by room temperatures, the dead-time estimator is a stand-in."""

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi.calibration import CALIBRATION_STEP_MAX_S, PulseLimits
from custom_components.vtherm_smartpi.smartpi.measure_pulse import (
    PULSE_HEAT_BACK_C,
    PULSE_HEAT_EXTEND_MAX_S,
    PULSE_HEAT_MAX_S,
    PULSE_TAKEOVER_CAP_C,
    MeasurePulse,
)

TARGET = 20.0
KINDS = ["learning", "calibration"]
SIDES = [(VThermHvacMode_HEAT, 1.0), (VThermHvacMode_COOL, -1.0)]


class _Est:
    """Dead-time estimator as the pulses read it."""

    def __init__(self, cooling: bool) -> None:
        self.cooling = cooling
        self.tin_history: list = []
        self.heat_start_time = None
        self.cool_start_time = None
        self.state = "WAITING_COOL_RESPONSE" if cooling else "WAITING_HEAT_RESPONSE"
        self.last_outcome: dict = {}
        self.stop_response_pending = False
        self.stop_peak_confirmed = False

    def arm(self, now: float) -> None:
        if self.cooling:
            self.cool_start_time = now
        else:
            self.heat_start_time = now

    def respond(self) -> None:
        self.state = "COOLING" if self.cooling else "HEATING"


class _Rig:
    """One pulse of ``kind`` in ``mode``; ``temp(d)`` is the room at ``d`` degrees on the demand side."""

    def __init__(self, kind: str, mode, sign: float) -> None:
        self.kind, self.mode, self.sign = kind, mode, sign
        self.est = _Est(sign < 0)
        self.pulse = MeasurePulse()
        self.heat_max = PULSE_HEAT_MAX_S if kind == "learning" else 7200.0
        self.limits = PulseLimits(
            start_over_c=0.2, gap_s=3600.0, rest_s=1200.0, trough_lead_s=0.0, fall_c_per_s=0.0,
            heat_max_s=self.heat_max, depth_cap_c=0.8, step_max_s=CALIBRATION_STEP_MAX_S,
        )
        self.now = 0.0

    def temp(self, demand: float) -> float:
        return TARGET - self.sign * demand

    def step(self, dt: float, demand: float) -> None:
        self.now += dt
        calibration = self.kind == "calibration"
        self.pulse.update(
            self.now, TARGET, self.temp(demand), self.mode, cooling=self.sign < 0, heat_or_cool=True,
            learning_stage=not calibration, active_deadtime_known=False, passive_deadtime_known=False,
            dt_est=self.est, calibration=calibration, calibration_pulses_left=calibration,
            limits=self.limits if calibration else None,
        )

    def to_heat(self) -> None:
        """Start the pulse at the setpoint, rest until the room is 0.5 C on the demand side, begin the heating step
        with the detection armed at that very instant."""
        self.step(0.0, 0.0)
        assert self.pulse.state == "rest"
        self.step(600.0, 0.5)
        assert self.pulse.state == "heat"
        self.est.arm(self.now)
        self.step(60.0, 0.5)

    def to_stop_after_rise(self) -> None:
        """Heat until the estimator saw the response and the room is back at 0.1 C on the demand side."""
        self.to_heat()
        self.est.respond()
        self.step(60.0, 0.45)
        self.step(60.0, 0.3)
        self.step(60.0, 0.1)
        assert self.pulse.state == "stop"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("mode", "sign"), SIDES)
def test_the_heating_step_goes_on_after_the_rise_until_the_room_is_back_near_the_setpoint(kind, mode, sign):
    rig = _Rig(kind, mode, sign)
    rig.to_heat()
    rig.est.respond()
    rig.step(60.0, 0.45)
    rig.step(60.0, 0.3)                    # the response is seen and stands out, the room is still 0.3 C away
    assert rig.pulse.state == "heat"
    rig.step(60.0, PULSE_HEAT_BACK_C + 0.02)
    assert rig.pulse.state == "heat"
    rig.step(60.0, PULSE_HEAT_BACK_C)
    assert rig.pulse.state == "stop" and rig.pulse.current["heat_end_reason"] == "rise seen"
    assert rig.pulse.current["temp_stop"] == pytest.approx(rig.temp(PULSE_HEAT_BACK_C))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("mode", "sign"), SIDES)
def test_past_its_extension_time_the_heating_step_ends_on_the_rise_alone(kind, mode, sign):
    assert PULSE_HEAT_EXTEND_MAX_S == 1800.0
    rig = _Rig(kind, mode, sign)
    rig.to_heat()
    start = rig.pulse.current["heat_start_s"]
    rig.est.respond()
    rig.step(60.0, 0.45)                   # the rise is seen, the room is still 0.45 C away
    assert rig.pulse.state == "heat"
    rig.step(start + PULSE_HEAT_EXTEND_MAX_S - 60.0 - rig.now, 0.4)
    assert rig.pulse.state == "heat"       # 29 min: the step goes on
    rig.step(60.0, 0.4)
    assert rig.pulse.state == "stop" and rig.pulse.current["heat_end_reason"] == "rise seen"
    assert rig.pulse.current["heat_s"] == PULSE_HEAT_EXTEND_MAX_S


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("mode", "sign"), SIDES)
def test_the_heating_step_still_ends_on_the_other_side_and_on_its_time_limit(kind, mode, sign):
    rig = _Rig(kind, mode, sign)
    rig.to_heat()
    rig.step(60.0, -0.3)                   # no response seen, the room is 0.3 C past the setpoint
    assert rig.pulse.state == "stop" and rig.pulse.current["heat_end_reason"] == "room over the setpoint"
    other = _Rig(kind, mode, sign)
    other.to_heat()
    other.step(other.heat_max, 0.4)        # no response, the room still far: the time limit
    assert other.pulse.state == "stop" and other.pulse.current["heat_end_reason"] == "time limit"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("mode", "sign"), SIDES)
def test_the_stop_is_taken_over_once_the_room_is_deeper_and_at_least_half_a_degree_under(kind, mode, sign):
    rig = _Rig(kind, mode, sign)
    rig.to_stop_after_rise()               # the stop starts at 0.1 C on the demand side
    rig.step(60.0, 0.3)                    # lower than at the start, but not 0.5 C yet
    assert rig.pulse.state == "stop"
    rig.step(60.0, 0.5)
    assert not rig.pulse.active and rig.pulse.history[-1]["end_reason"] == "room under the setpoint"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("mode", "sign"), SIDES)
def test_a_stop_that_starts_far_under_waits_for_a_lower_room_or_the_cap(kind, mode, sign):
    rig = _Rig(kind, mode, sign)
    rig.to_heat()
    rig.step(rig.heat_max, 0.6)            # the heating step ends on its limit at 0.6 C: the stop step runs
    assert rig.pulse.state == "stop"
    rig.step(60.0, 0.6)
    assert rig.pulse.state == "stop"       # not lower than at the start of the stop
    rig.step(60.0, 0.61)
    assert not rig.pulse.active and rig.pulse.history[-1]["end_reason"] == "room under the setpoint"
    capped = _Rig(kind, mode, sign)
    capped.to_heat()
    capped.step(capped.heat_max, PULSE_TAKEOVER_CAP_C)
    assert not capped.pulse.active and capped.pulse.history[-1]["end_reason"] == "room under the setpoint"


def test_the_stop_of_a_learning_pulse_ends_on_the_confirmed_peak_before_any_takeover():
    rig = _Rig("learning", VThermHvacMode_HEAT, 1.0)
    rig.to_stop_after_rise()
    rig.step(60.0, 0.15)                   # the peak is not confirmed yet
    assert rig.pulse.state == "stop"
    rig.est.stop_peak_confirmed = True
    rig.step(1800.0, 0.2)
    assert not rig.pulse.active and rig.pulse.history[-1]["end_reason"] == "peak"
