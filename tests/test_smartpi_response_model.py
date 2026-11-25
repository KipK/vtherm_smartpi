"""Response model: the profile fit on level crossings, the aggregation per mode, the seed of an earlier state, the
saved state, the published values and the reader of every event."""

import math
import random
import time
from collections import deque
from unittest.mock import MagicMock

import pytest

from custom_components.vtherm_smartpi.hvac_mode import VThermHvacMode_COOL, VThermHvacMode_HEAT
from custom_components.vtherm_smartpi.smartpi import event_reading as er
from custom_components.vtherm_smartpi.smartpi.event_fit import (
    REJECT_CROSSINGS,
    REJECT_RISE,
    REJECT_RMS,
    crossings,
    run_response_fit,
)
from custom_components.vtherm_smartpi.smartpi.learning_start import LearningStart
from custom_components.vtherm_smartpi.smartpi.measure_event import (
    MODE_COOL,
    MODE_HEAT,
    SOURCE_BOOTSTRAP,
    SOURCE_CALIBRATION,
    SOURCE_PULSE,
    SOURCE_REGULATION,
    MeasureEventDetector,
)
from custom_components.vtherm_smartpi.smartpi.response_model import (
    CONFIDENCE_ASSUMED,
    CONFIDENCE_MEASURED,
    CONFIDENCE_PROVISIONAL,
    OUTCOME_ACCEPTED,
    OUTCOME_DAYTIME,
    OUTCOME_DAYTIME_FIRST,
    OUTCOME_LOWER_LIMIT,
    RESPONSE_MODEL_HISTORY,
    ResponseModel,
    peak_sample,
    seed_from_saved,
)


# -- the fit ---------------------------------------------------------------------------------------------------------
def _trace(theta, tau, step, a=0.05, b=0.0005, on_s=3600.0, rest=3600.0, total=6 * 3600.0, obs=60.0, ext=5.0,
           noise=0.0, wave=0.0, seed=1):
    """A step of full power through a delay ``theta`` and a first-order emitter ``tau`` into a 1R1C room, observed every
    ``obs`` seconds by a sensor of step ``step`` that reports its changes (snapshot form of the record). ``noise``
    (standard deviation) and ``wave`` (amplitude of a two-hour oscillation the model cannot follow) disturb the
    measured values."""
    rnd = random.Random(seed)
    dt, temp, heat = 5.0, 19.0, 0.0
    room = [[0.0, round(temp / step) * step]]
    for k in range(int(total / dt)):
        t = k * dt
        heat += ((1.0 if rest <= t - theta < rest + on_s else 0.0) - heat) * (1.0 - math.exp(-dt / tau))
        temp += (a * heat - b * (temp - ext)) * dt / 60.0
        if (t + dt) % obs == 0:
            measured = temp + (rnd.gauss(0.0, noise) if noise else 0.0) + wave * math.sin(2.0 * math.pi * t / 7200.0)
            v = round(round(measured / step) * step, 3)
            if v != room[-1][1]:
                room.append([t + dt, v])
    return {"power": [[0.0, 0.0], [rest, 1.0], [rest + on_s, 0.0], [total, 0.0]], "room": room,
            "ext": [[0.0, ext]], "cooling": False, "start": rest - 1800.0, "end": total, "heat": rest}


CELLS = [(0.0, 300.0), (120.0, 900.0), (600.0, 1200.0), (300.0, 3600.0), (840.0, 840.0), (1800.0, 3900.0)]


@pytest.mark.parametrize("step", [0.01, 0.1])
@pytest.mark.parametrize("theta,tau", CELLS)
def test_the_fit_finds_the_dead_time_and_the_response_time_constant_of_a_synthetic_room(step, theta, tau):
    on = max(1800.0, min(3.0 * (theta + tau), 7200.0))
    snap = _trace(theta, tau, step, on_s=on, total=min(3600.0 + on + 6.0 * (theta + tau) + 1800.0, 24 * 3600.0))
    result = run_response_fit(snap)
    assert result["reject"] is None and result["split_ok"] is True
    assert result["s_s"] == pytest.approx(theta + tau, rel=0.10, abs=30.0)
    # the observation every 60 s delays a 0.1 C crossing by up to a minute: it lands in the dead time
    assert result["theta_s"] == pytest.approx(theta, abs=60.0 if step == 0.1 else 30.0)
    assert result["tau_e_s"] == pytest.approx(tau, rel=0.10)
    assert result["theta_range_s"][0] <= result["theta_s"] <= result["theta_range_s"][1]
    assert result["a_per_min"] == pytest.approx(0.05, rel=0.05)


def test_the_fit_rejects_a_window_with_few_changes_or_a_small_rise():
    snap = _trace(120.0, 900.0, 0.1, on_s=300.0, a=0.01, total=4 * 3600.0)
    result = run_response_fit(snap)
    assert result["reject"] in (REJECT_CROSSINGS, REJECT_RISE)
    few = run_response_fit(dict(snap, room=snap["room"][:4]))
    assert few["reject"] == REJECT_CROSSINGS and few["lag_s"] > 0.0 and "theta_s" not in few
    assert run_response_fit(dict(snap, ext=[])) is None


def test_the_fit_needs_a_rise_of_a_tenth_of_a_degree_on_a_fine_sensor():
    weak = run_response_fit(_trace(300.0, 1200.0, 0.01, a=0.03, on_s=900.0, total=4 * 3600.0))
    assert 0.05 <= weak["rise_c"] < 0.10 and weak["reject"] == REJECT_RISE      # 5 or more sensor steps, under 0.10 C
    firm = run_response_fit(_trace(300.0, 1200.0, 0.01, a=0.04, on_s=800.0, total=4 * 3600.0))
    assert 0.10 <= firm["rise_c"] < 0.20 and firm["reject"] is None
    coarse = run_response_fit(_trace(300.0, 1200.0, 0.1, a=0.03, on_s=900.0, total=4 * 3600.0))
    assert coarse["rise_c"] < 0.3 and coarse["reject"] == REJECT_RISE           # on a 0.1 C sensor three steps rule


def test_the_fit_rejects_a_room_it_cannot_follow_and_flags_a_time_constant_on_the_lower_bound():
    far = run_response_fit(_trace(300.0, 1200.0, 0.01, wave=0.1))
    assert far["reject"] == REJECT_RMS and far["rms_c"] > 2 * 0.01
    noisy = run_response_fit(_trace(300.0, 1200.0, 0.1, noise=0.04))     # within two steps of a 0.1 C sensor
    assert noisy["reject"] is None and noisy["rms_c"] < 2 * 0.1
    fast = run_response_fit(_trace(0.0, 10.0, 0.01, on_s=1800.0))         # the polish stops on the grid's 60 s
    assert fast["reject"] is None and fast["tau_e_lower_limit"] is True
    assert fast["tau_e_s"] == pytest.approx(60.0, rel=0.03) and fast["rms_c"] <= 2 * 0.01
    model = ResponseModel()
    assert model.add(MODE_HEAT, SOURCE_PULSE, fast, 0.0) == OUTCOME_LOWER_LIMIT
    assert model.values(MODE_HEAT) is not None and model.last["outcome"] == OUTCOME_LOWER_LIMIT
    assert run_response_fit(_trace(300.0, 1200.0, 0.01))["tau_e_lower_limit"] is False


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_dead_time_is_not_told_apart_on_a_noisy_small_step(seed):
    # a weak emitter on a 0.1 C sensor with noise: S is read, the split between theta and tau_e is not
    result = run_response_fit(_trace(300.0, 1200.0, 0.1, a=0.02, noise=0.06, seed=seed))
    assert result["reject"] is None and result["split_ok"] is False
    assert result["theta_range_s"][1] - result["theta_range_s"][0] > 0.5 * result["s_s"]
    assert result["s_s"] == pytest.approx(1500.0, rel=0.10)


def test_the_model_in_force_is_scored_on_the_window_of_a_new_event():
    snap = _trace(300.0, 1200.0, 0.01)
    own = run_response_fit(snap)
    assert own["prediction_rms_c"] is None                                    # no model in force
    right = run_response_fit(dict(snap, model=(300.0, 1200.0)))
    wrong = run_response_fit(dict(snap, model=(0.0, 300.0)))
    assert right["prediction_rms_c"] < 2 * own["rms_c"] < wrong["prediction_rms_c"]
    assert {k: v for k, v in right.items() if k not in ("prediction_rms_c", "seconds")} == \
        {k: v for k, v in own.items() if k not in ("prediction_rms_c", "seconds")}   # the fit itself is unchanged
    model = ResponseModel()
    for k, value in enumerate((0.02, None, 0.04, 0.03)):
        model.add(MODE_HEAT, SOURCE_PULSE, dict(_fit(300.0, 1200.0), prediction_rms_c=value), float(k))
    assert model.last["prediction_rms_c"] == 0.03
    assert model.public_learning(MODE_HEAT)["prediction_rms_c"] == 0.03       # median of 0.02, 0.04, 0.03
    model.add(MODE_HEAT, SOURCE_PULSE, dict(_fit(300.0, 1200.0, reject=REJECT_RISE), prediction_rms_c=0.5), 9.0)
    assert model.last["prediction_rms_c"] is None and model.public_learning(MODE_HEAT)["prediction_rms_c"] == 0.03
    other = ResponseModel()
    other.load(model.save())
    assert other.public_learning(MODE_HEAT)["prediction_rms_c"] == 0.03
    assert other.public_learning(MODE_COOL)["prediction_rms_c"] is None


def test_crossings_take_the_midpoint_of_a_one_step_change_and_the_value_of_a_jump():
    times, values, step = crossings([0.0, 10.0, 20.0, 30.0, 40.0], [20.0, 20.1, 20.1, 20.4, 20.3], 0.0, 40.0)
    assert step == pytest.approx(0.1)
    assert times == [10.0, 30.0, 40.0]
    assert values == pytest.approx([20.05, 20.4, 20.35])


# -- aggregation -----------------------------------------------------------------------------------------------------
def _fit(theta, tau, split_ok=True, reject=None):
    return {"theta_s": theta, "tau_e_s": tau, "s_s": theta + tau, "split_ok": split_ok, "rms_c": 0.01, "reject": reject}


def test_one_event_publishes_a_provisional_model_two_agreeing_events_a_measured_one():
    model = ResponseModel()
    assert model.values(MODE_HEAT) is None
    assert model.add(MODE_HEAT, SOURCE_PULSE, _fit(200.0, 800.0), 100.0) == OUTCOME_ACCEPTED
    values = model.values(MODE_HEAT)
    assert values == {"theta_s": 200.0, "tau_e_s": 800.0, "source": SOURCE_PULSE, "confidence": CONFIDENCE_PROVISIONAL}
    model.add(MODE_HEAT, SOURCE_BOOTSTRAP, _fit(300.0, 1600.0), 200.0)       # S 1900 against 1000: no agreement
    assert model.values(MODE_HEAT)["confidence"] == CONFIDENCE_PROVISIONAL
    model.add(MODE_HEAT, SOURCE_BOOTSTRAP, _fit(250.0, 850.0), 300.0)        # S 1100 agrees with 1000
    values = model.values(MODE_HEAT)
    assert values["confidence"] == CONFIDENCE_MEASURED
    assert values["theta_s"] == 250.0 and values["theta_s"] + values["tau_e_s"] == 1100.0   # medians
    assert model.effective_dead_time_s(MODE_HEAT) == 250.0 + 425.0
    assert model.emitter_lag_s(MODE_HEAT) == 1100.0
    assert model.values(MODE_COOL) is None                                   # per mode


def test_without_an_event_that_tells_the_dead_time_apart_s_is_split_in_halves():
    model = ResponseModel()
    model.add(MODE_HEAT, SOURCE_PULSE, _fit(50.0, 1000.0, split_ok=False), 0.0)
    values = model.values(MODE_HEAT)
    assert values["theta_s"] == 525.0 and values["tau_e_s"] == 525.0
    assert model.effective_dead_time_s(MODE_HEAT) == 0.75 * 1050.0
    assert values["confidence"] == CONFIDENCE_ASSUMED
    fast = ResponseModel()
    fast.add(MODE_HEAT, SOURCE_PULSE, _fit(0.0, 150.0, split_ok=False), 0.0)
    assert fast.values(MODE_HEAT)["theta_s"] == 75.0 and fast.values(MODE_HEAT)["tau_e_s"] == 75.0


@pytest.mark.parametrize("source", [SOURCE_PULSE, SOURCE_BOOTSTRAP, SOURCE_CALIBRATION, SOURCE_REGULATION])
def test_a_daylight_event_is_kept_only_while_the_mode_has_none_until_one_in_darkness(source):
    model = ResponseModel()
    assert model.add(MODE_HEAT, source, _fit(100.0, 500.0), 0.0, daytime=True) == OUTCOME_DAYTIME_FIRST
    assert model.values(MODE_HEAT)["theta_s"] == 100.0 and model.last["daytime"] is True
    assert model.add(MODE_HEAT, source, _fit(100.0, 900.0), 1.0, daytime=True) == OUTCOME_DAYTIME
    assert model.event_count(MODE_HEAT) == 1 and model.last["outcome"] == OUTCOME_DAYTIME
    assert model.add(MODE_HEAT, source, _fit(200.0, 800.0), 2.0) == OUTCOME_ACCEPTED
    assert model.event_count(MODE_HEAT) == 1 and model.values(MODE_HEAT)["theta_s"] == 200.0   # replaced
    assert model.add(MODE_HEAT, source, _fit(100.0, 500.0), 3.0, daytime=True) == OUTCOME_DAYTIME
    other = ResponseModel()
    other.add(MODE_HEAT, source, _fit(100.0, 500.0), 0.0, daytime=True)
    restored = ResponseModel()
    restored.load(other.save())
    restored.add(MODE_HEAT, source, _fit(200.0, 800.0), 2.0)
    assert restored.event_count(MODE_HEAT) == 1                        # the flag survives a restart


def test_rejected_events_are_shown_never_kept_and_the_history_is_bounded():
    model = ResponseModel()
    assert model.add(MODE_HEAT, SOURCE_PULSE, _fit(100.0, 500.0, reject=REJECT_RISE), 0.0) == REJECT_RISE
    assert model.add(MODE_HEAT, SOURCE_PULSE, _fit(100.0, 500.0, reject=REJECT_RISE), 0.0, daytime=True) == REJECT_RISE
    assert model.values(MODE_HEAT) is None
    assert model.last["daytime"] is True and model.last["outcome"] == REJECT_RISE
    for k in range(RESPONSE_MODEL_HISTORY + 3):
        model.add(MODE_HEAT, SOURCE_REGULATION, _fit(100.0 + k, 500.0), float(k))
    assert model.event_count(MODE_HEAT) == RESPONSE_MODEL_HISTORY


def test_two_agreeing_calibration_events_far_from_the_model_replace_its_history():
    model = ResponseModel()
    for k in range(3):
        model.add(MODE_HEAT, SOURCE_REGULATION, _fit(200.0, 800.0), float(k))
    model.start_calibration()
    model.add(MODE_HEAT, SOURCE_CALIBRATION, _fit(400.0, 1600.0), 10.0)
    assert model.event_count(MODE_HEAT) == 4
    model.add(MODE_HEAT, SOURCE_CALIBRATION, _fit(420.0, 1700.0), 11.0)     # agree, S off by more than 30 %
    assert model.event_count(MODE_HEAT) == 2 and model.values(MODE_HEAT)["source"] == SOURCE_CALIBRATION
    close = ResponseModel()
    for k in range(3):
        close.add(MODE_HEAT, SOURCE_REGULATION, _fit(200.0, 800.0), float(k))
    close.start_calibration()
    close.add(MODE_HEAT, SOURCE_CALIBRATION, _fit(210.0, 850.0), 10.0)
    close.add(MODE_HEAT, SOURCE_CALIBRATION, _fit(200.0, 820.0), 11.0)       # within 30 %: they only join
    assert close.event_count(MODE_HEAT) == 5


def test_a_peak_sample_follows_the_first_order_emitter():
    # a q decays from s + l at the cut + theta; the peak comes when a q = l
    theta, tau, slope, loss = 120.0, 900.0, 0.02, 0.005
    delay = theta + tau * math.log(1.0 + slope / loss)
    assert peak_sample(theta, delay, slope, loss) == pytest.approx(tau)
    assert peak_sample(theta, delay, 0.004, loss) is None                  # slope under the loss rate
    assert peak_sample(theta, theta + 300.0, slope, loss) is None          # peak too close to the dead time


# -- migration, persistence, publication -----------------------------------------------------------------------------
def _saved(dead=None, reading_lag=None, balance_lags=(), response_lag=None, mode=MODE_HEAT):
    slot = "heat" if mode == MODE_HEAT else "cool"
    state = {"dt_est_state": {"model_hvac_mode": mode, f"deadtime_{slot}_s": dead,
                              f"deadtime_{slot}_reliable": dead is not None},
             "lw_state": {"heating_balance": {"lags": list(balance_lags), "results": []},
                          "response_lag": {"hvac_mode": mode, "published": response_lag}}}
    if reading_lag is not None:
        state["event_reading"] = {"kept": {mode: {"a_per_min": 0.05, "lag_s": reading_lag}}}
    return state


def test_the_seed_keeps_the_effective_dead_time_at_the_saved_dead_time():
    theta, tau = seed_from_saved(_saved(dead=600.0, reading_lag=900.0, balance_lags=[2000.0]))[MODE_HEAT]
    assert (theta, tau) == (300.0, 600.0) and theta + tau / 2 == 600.0          # the reading's lag comes first
    theta, tau = seed_from_saved(_saved(dead=600.0, balance_lags=[2000.0, 1000.0]))[MODE_HEAT]
    assert (theta, tau) == (0.0, 1200.0)                                         # 2L - S < 0: theta 0
    theta, tau = seed_from_saved(_saved(dead=600.0, response_lag=400.0))[MODE_HEAT]
    assert (theta, tau) == (600.0, 0.0)                                          # S < L: theta L
    assert seed_from_saved(_saved(dead=600.0))[MODE_HEAT] == (400.0, 400.0)                   # S / 2 each, 3 S / 4 = L
    assert seed_from_saved(_saved(dead=60.0))[MODE_HEAT] == (40.0, 40.0)
    assert seed_from_saved(_saved(reading_lag=900.0)) == {}
    cool = seed_from_saved(_saved(dead=300.0, balance_lags=[2000.0], mode=MODE_COOL))   # the balance is HEAT only
    assert cool == {MODE_COOL: (200.0, 200.0)}


def test_a_seed_is_previous_and_provisional_until_the_first_accepted_event():
    model = ResponseModel()
    model.seed(MODE_HEAT, 300.0, 600.0)
    assert model.public_model(MODE_HEAT) == {"dead_time_s": 300, "response_time_constant_s": 600,
                                             "response_source": "previous version",
                                             "response_confidence": CONFIDENCE_PROVISIONAL}
    model.add(MODE_HEAT, SOURCE_PULSE, _fit(100.0, 500.0), 0.0)
    assert model.values(MODE_HEAT)["source"] == SOURCE_PULSE
    model.reset()
    assert model.values(MODE_HEAT) is None


def test_a_state_of_an_earlier_version_loads_without_its_dead_time_after_a_stop():
    """A state saved before the response model (dead times after a stop, heating balance, response lag) loads: the
    dead time after a stop is ignored in both modes, the one after a start is kept and seeds the model."""
    from custom_components.vtherm_smartpi.algo import SmartPI

    def _algo():
        return SmartPI(hass=MagicMock(), cycle_min=10.0, minimal_activation_delay=0, minimal_deactivation_delay=0,
                       name="Earlier state")

    state = _algo().save_state()
    del state["response_state"]
    state["dt_est_state"] = {
        "model_hvac_mode": MODE_HEAT,
        "deadtime_heat_s": 600.0, "deadtime_heat_reliable": True, "history_heat": [600.0],
        "deadtime_cool_s": 2400.0, "deadtime_cool_reliable": True, "history_cool": [2400.0],
        "mode_stash": {MODE_COOL: {
            "deadtime_heat_s": 1800.0, "deadtime_heat_reliable": True, "history_heat": [1800.0],
            "deadtime_cool_s": 300.0, "deadtime_cool_reliable": True, "history_cool": [300.0]}},
        "tin_history": [], "tin_lp_history": [],
        "detection": {"state": "WAITING_COOL_RESPONSE", "last_power": 0.0, "waiting_power_on": False,
                      "cool_start_wall": time.time() - 600.0, "cool_peak_temp": 20.0, "trend_slope": 0.0},
    }
    state["lw_state"]["heating_balance"] = {"lags": [2000.0], "results": [], "restart": None}
    state["lw_state"]["response_lag"] = {"hvac_mode": MODE_HEAT, "published": 900.0}

    algo = _algo()
    algo.load_state(state)
    algo.dt_est.resume_after_restart(True)
    dt_est = algo.dt_est
    assert (dt_est.deadtime_heat_s, dt_est.deadtime_heat_reliable) == (600.0, True)
    assert (dt_est.deadtime_cool_s, dt_est.deadtime_cool_reliable) == (None, False)
    assert dt_est.state == "OFF"
    assert dt_est.save_state()["mode_stash"][MODE_COOL]["deadtime_heat_s"] is None
    # seeds of the response model: theta + tau_e / 2 = the saved dead time after a start
    assert algo._start.model.values(MODE_HEAT)["theta_s"] == 0.0
    assert algo._start.model.values(MODE_HEAT)["tau_e_s"] == pytest.approx(1200.0)
    assert algo._start.model.values(MODE_COOL)["theta_s"] + 0.5 * algo._start.model.values(MODE_COOL)["tau_e_s"] == (
        pytest.approx(300.0))
    model = algo.get_published_diagnostics()["model"]
    assert model["deadtime_heat_s"] == 600.0 and "deadtime_cool_s" not in model
    algo.calculate(target_temp=20.0, current_temp=19.0, ext_current_temp=5.0, hvac_mode=VThermHvacMode_HEAT)
    # the mode set aside comes back with its dead time after a start only
    assert dt_est.ensure_hvac_mode(VThermHvacMode_COOL) is True
    assert (dt_est.deadtime_cool_s, dt_est.deadtime_heat_s) == (300.0, None)


def test_the_saved_model_loads_back_and_malformed_parts_are_skipped():
    model = ResponseModel()
    model.add(MODE_HEAT, SOURCE_PULSE, _fit(150.0, 700.0), 0.0)
    model.seed(MODE_COOL, 60.0, 120.0)
    model.add_peak(MODE_HEAT, 650.0)
    other = ResponseModel()
    other.load(model.save())
    assert other.values(MODE_HEAT) == model.values(MODE_HEAT) and other.values(MODE_COOL) == model.values(MODE_COOL)
    assert other.public_learning(MODE_HEAT) == model.public_learning(MODE_HEAT)
    broken = model.save()
    broken["events"][MODE_HEAT].append({"theta_s": "x"})
    broken["seed"]["off"] = {"theta_s": 1.0, "tau_e_s": 1.0}
    other.load(broken)
    assert other.event_count(MODE_HEAT) == 1 and other.values("off") is None
    other.load("nonsense")
    assert other.values(MODE_HEAT) is None


def test_the_published_block_uses_plain_words():
    model = ResponseModel()
    model.refuse("heating too short")
    assert model.public_learning(MODE_HEAT) == {"events": 0, "last": None, "last_reason": "heating too short",
                                               "peak_samples": [], "prediction_rms_c": None}
    model.add(MODE_HEAT, SOURCE_PULSE, _fit(150.4, 700.6), 0.0)
    model.add_peak(MODE_HEAT, 640.2)
    model.add_peak(MODE_COOL, 100.0)
    block = model.public_learning(MODE_HEAT)
    assert block["events"] == 1 and block["peak_samples"] == [640]
    assert block["last"] == {"source": "measurement pulses", "dead_time_s": 150, "response_time_constant_s": 701,
                             "rms_c": 0.01, "outcome": OUTCOME_ACCEPTED, "daytime": False, "prediction_rms_c": None}
    assert model.public_effective_dead_time_s(MODE_HEAT) == round(150.4 + 700.6 / 2)


def test_learning_start_saves_the_model_and_seeds_an_earlier_state():
    start = LearningStart("bootstrap", MagicMock())
    start.model.add(MODE_HEAT, SOURCE_PULSE, _fit(150.0, 700.0), 0.0)
    state = start.save(0.0)
    assert "response_state" in state
    other = LearningStart("bootstrap", MagicMock())
    other.load(state)
    assert other.model.values(MODE_HEAT) == start.model.values(MODE_HEAT)
    earlier = LearningStart("bootstrap", MagicMock())
    earlier.load(_saved(dead=600.0, reading_lag=900.0))
    assert earlier.model.values(MODE_HEAT)["source"] == "previous"


# -- the reader of every event ---------------------------------------------------------------------------------------
STEP_S = 60.0


class _Rig:
    """A room behind a delay and a first-order emitter, observed every minute, with the model reader attached."""

    THETA, TAU = 300.0, 900.0

    def __init__(self, source: str, position=(48.8, 2.3)) -> None:
        self.model = ResponseModel()
        self.reader = er.ModelReader(self.model)
        if position is not None:
            self.reader.set_position(*position)
        self.detector = MeasureEventDetector("test", lambda: self.source)
        self.detector.model_reader = self.reader
        self.source = source
        self.temp, self.heat, self.now, self.buf = 19.0, 0.0, 1000.0, []
        self.history: deque = deque()

    def calculation(self, u: float) -> None:
        for _ in range(6):
            self.buf.append(u)
            delayed = self.buf[-1 - int(self.THETA / 10)] if len(self.buf) > self.THETA / 10 else 0.0
            self.heat += (delayed - self.heat) * (1.0 - math.exp(-10.0 / self.TAU))
            self.temp += (0.05 * self.heat - 0.0005 * (self.temp - 5.0)) * 10.0 / 60.0
        self.now += STEP_S
        self.reader.note_control(self.now, None, 0.0005)
        self.reader.step(self.now, self.detector.phase_start)
        self.history.append((self.now, round(self.temp, 2)))
        self.detector.observe(now=self.now, dt_s=STEP_S, u=u, hvac_mode=VThermHvacMode_HEAT,
                              tin_history=self.history, context="regulation", ext=5.0)
        self.reader.run_queued_sync()

    def run(self, u: float, minutes: int) -> None:
        for _ in range(minutes):
            self.calculation(u)

    def event(self, heat_min: int = 60) -> None:
        self.run(0.0, 120)
        self.run(1.0, heat_min)
        self.run(0.0, 600)


def test_pulse_events_feed_the_model_whatever_values_are_kept(monkeypatch):
    rig = _Rig(SOURCE_PULSE)
    monkeypatch.setattr(er.ModelReader, "_dark_end", _dark_for(86400.0))
    rig.event()
    assert rig.model.last["prediction_rms_c"] is None                         # no model before the first event
    rig.event()
    assert 0.0 < rig.model.last["prediction_rms_c"] < 0.02
    values = rig.model.values(MODE_HEAT)
    assert values is not None and rig.model.last["outcome"] == OUTCOME_ACCEPTED
    assert values["theta_s"] + values["tau_e_s"] == pytest.approx(rig.THETA + rig.TAU, rel=0.15)
    assert rig.reader.record.rows > 0 and rig.reader.wants_record(False, VThermHvacMode_HEAT)


def _dark_for(seconds):
    """Darkness that ends ``seconds`` after any instant (None: daylight)."""
    return lambda self, t: None if seconds is None else t + seconds


@pytest.mark.parametrize("source", [SOURCE_PULSE, SOURCE_BOOTSTRAP, SOURCE_CALIBRATION, SOURCE_REGULATION])
def test_every_source_in_daylight_is_used_only_until_an_event_in_darkness(monkeypatch, source):
    rig = _Rig(source)
    monkeypatch.setattr(er.ModelReader, "_dark_end", _dark_for(None))
    rig.event()
    assert rig.model.last["outcome"] == OUTCOME_DAYTIME_FIRST and rig.model.last["daytime"] is True
    assert rig.model.event_count(MODE_HEAT) == 1
    rig.event()
    assert rig.model.last["outcome"] == OUTCOME_DAYTIME and rig.model.event_count(MODE_HEAT) == 1
    monkeypatch.setattr(er.ModelReader, "_dark_end", _dark_for(86400.0))
    rig.event()
    assert rig.model.last["outcome"] == OUTCOME_ACCEPTED and rig.model.event_count(MODE_HEAT) == 1
    assert rig.model.save()["events"][MODE_HEAT][0]["daytime"] is False


def test_without_a_home_position_no_event_is_in_daylight():
    rig = _Rig(SOURCE_PULSE, position=None)
    rig.event()
    assert rig.model.last["outcome"] == OUTCOME_ACCEPTED and rig.model.last["daytime"] is False


@pytest.mark.parametrize("dark_lags,outcome", [(4.0, OUTCOME_ACCEPTED), (2.0, OUTCOME_DAYTIME_FIRST)])
def test_the_window_ends_at_sunrise_and_needs_three_lags_of_darkness(monkeypatch, dark_lags, outcome):
    rig = _Rig(SOURCE_PULSE)
    lag = rig.THETA + rig.TAU
    monkeypatch.setattr(er.ModelReader, "_dark_end", _dark_for(dark_lags * lag))
    rig.event()
    reading = rig.reader.snapshot()["readings"][-1]
    assert reading["state"] == er.STATE_COMPLETE and rig.model.last["outcome"] == outcome
    assert reading["fits"][-1]["end_s"] - reading["heat_start_s"] == pytest.approx(dark_lags * lag, abs=60.0)


def test_the_darkness_of_a_heating_step_ends_at_the_next_sunrise():
    reader = er.ModelReader(ResponseModel())
    reader.set_position(48.8, 2.3)
    if reader.position is None:
        pytest.skip("solar position not available")
    night = 1_700_000_000.0                                  # a November evening, 22:13 UTC
    t = time.monotonic() + (night - time.time())
    end = reader._dark_end(t)
    assert end is not None and 7 * 3600.0 < end - t < 10 * 3600.0
    assert reader._dark_end(t + 14 * 3600.0) is None        # mid-day


def _regulated_day(rig, hours=14):
    rig.run(0.0, 120)
    rig.run(0.3, hours * 60)
    rig.run(0.0, 600)


def test_a_regulation_reading_ends_six_published_lags_after_its_heating_step(monkeypatch):
    rig = _Rig(SOURCE_REGULATION)
    monkeypatch.setattr(er.ModelReader, "_dark_end", _dark_for(86400.0))
    rig.model.add(MODE_HEAT, SOURCE_PULSE, _fit(300.0, 900.0), 0.0)
    _regulated_day(rig)
    reading = rig.reader.snapshot()["readings"][-1]
    assert reading["state"] == er.STATE_COMPLETE and len(reading["fits"]) == 1
    assert reading["fits"][0]["end_s"] - reading["heat_start_s"] == pytest.approx(6 * 1200.0, abs=60.0)
    assert rig.model.event_count(MODE_HEAT) == 2


def test_without_a_published_model_a_regulation_reading_is_fitted_again_on_six_lags_of_its_fit(monkeypatch):
    rig = _Rig(SOURCE_REGULATION)
    monkeypatch.setattr(er.ModelReader, "_dark_end", _dark_for(86400.0))
    _regulated_day(rig)
    reading = rig.reader.snapshot()["readings"][-1]
    fits = reading["fits"]
    assert len(fits) >= 2 and fits[0]["end_s"] - reading["heat_start_s"] > 12 * 3600.0
    window = fits[-1]["end_s"] - reading["heat_start_s"]
    assert reading["state"] == er.STATE_COMPLETE and window <= 6 * fits[-1]["lag_s"] / er.READING_COVERAGE + 60.0
    assert fits[-1]["lag_s"] == pytest.approx(rig.THETA + rig.TAU, rel=0.15)


@pytest.mark.parametrize("case,reason", [
    ("short", "heating too short"), ("excluded", "SmartPI not in control"), ("position", "home position unknown"),
])
def test_a_regulation_event_out_of_the_rules_is_not_read(monkeypatch, case, reason):
    rig = _Rig(SOURCE_REGULATION, position=None if case == "position" else (48.8, 2.3))
    monkeypatch.setattr(er.ModelReader, "_dark_end", _dark_for(86400.0))
    if case == "excluded":
        rig.run(0.0, 100)
        rig.reader.note_control(rig.now, "command overridden", 0.0005)
        rig.run(0.0, 20)
        rig.run(1.0, 60)
        rig.run(0.0, 600)
    else:
        rig.event(heat_min=5 if case == "short" else 60)
    assert rig.model.values(MODE_HEAT) is None
    assert rig.model.last_reason == reason or (case == "short" and rig.model.last_reason == "temperature rise too small")


def test_a_published_model_gives_peak_samples_of_the_confirmed_peaks():
    rig = _Rig(SOURCE_PULSE)
    rig.event()
    rig.event()
    assert rig.model.public_learning(MODE_HEAT)["peak_samples"]


def test_the_model_reader_saves_its_readings_with_their_source(monkeypatch):
    rig = _Rig(SOURCE_PULSE)
    monkeypatch.setattr(er.ModelReader, "_dark_end", _dark_for(86400.0))
    monkeypatch.setattr(er.ModelReader, "run_queued_sync", lambda self: None)   # the fit waits
    rig.run(0.0, 120)
    rig.run(1.0, 60)
    rig.run(0.0, 40)                         # the peak is not confirmed yet: no reading
    rig.run(0.0, 30)
    saved = rig.reader.save()
    other = er.ModelReader(ResponseModel())
    other.load(saved)
    assert other.snapshot()["readings"] and all(r["source"] == SOURCE_PULSE for r in other.snapshot()["readings"])
    for mine, restored in zip(rig.reader.snapshot()["readings"], other.snapshot()["readings"]):
        assert restored["dark_end_s"] == pytest.approx(mine["dark_end_s"]) and restored["daytime"] is False
