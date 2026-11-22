"""Peak search after a cut: bins, confirmation, outcomes, saved bins."""

import pytest

from custom_components.vtherm_smartpi.smartpi.const import (
    HEATING_BALANCE_PEAK_BIN_S,
    HEATING_BALANCE_PEAK_CONFIRM_S,
    HEATING_BALANCE_PEAK_MAX_S,
)
from custom_components.vtherm_smartpi.smartpi.peak_search import (
    PEAK_CONFIRMED,
    PEAK_CUT_SHORT,
    PEAK_NONE,
    PeakSearch,
)

BIN = HEATING_BALANCE_PEAK_BIN_S
CONFIRM_BINS = int(HEATING_BALANCE_PEAK_CONFIRM_S // BIN)


def _feed(search: PeakSearch, values: list[float], step_s: float = 60.0, t0: float = 0.0) -> int | None:
    """Feed one sample per step; the index of the sample that confirms the peak, else None."""
    for i, v in enumerate(values):
        if search.feed(t0 + (i + 1) * step_s, v):
            return i
    return None


def _hump(peak_at_s: float, n: int, step_s: float = 60.0) -> list[float]:
    """Rise to the peak time, then fall."""
    return [20.0 - 0.001 * abs((i + 1) * step_s - peak_at_s) for i in range(n)]


def test_peak_is_confirmed_one_confirmation_span_after_the_best_bin():
    search = PeakSearch(0.0)
    values = _hump(1200.0, 120)
    index = _feed(search, values)
    assert index is not None
    assert search.peak_delay_s == pytest.approx((search.best_k + 0.5) * BIN)
    assert abs(search.peak_delay_s - 1200.0) <= BIN
    # the confirming sample is the first one of a bin lying the confirmation span after the best bin
    assert (index + 1) * 60.0 >= (search.best_k + CONFIRM_BINS + 1) * BIN - 60.0


def test_bin_value_is_the_mean_of_its_samples_and_best_is_the_earliest_strict_maximum():
    search = PeakSearch(0.0)
    # three bins on a plateau: equal means keep the first one
    for t, v in ((10.0, 20.0), (100.0, 20.0), (310.0, 20.0), (400.0, 20.0), (610.0, 20.0), (700.0, 19.0)):
        search.feed(t, v)
    assert search.best_k == 0
    assert search.best_mean == pytest.approx(20.0)
    assert search.last_k == 1


def test_empty_bins_are_skipped():
    search = PeakSearch(0.0)
    search.feed(10.0, 20.0)
    search.feed(5 * BIN + 10.0, 19.0)  # closes bin 0 on the first sample of bin 5
    assert (search.best_k, search.last_k, search.bin_k) == (0, 0, 5)


def test_search_without_confirmation_gives_none_until_it_expires():
    search = PeakSearch(100.0)
    assert _feed(search, [20.0 + 0.001 * i for i in range(300)], t0=100.0) is None
    assert not search.expired(100.0 + HEATING_BALANCE_PEAK_MAX_S - 1.0)
    assert search.expired(100.0 + HEATING_BALANCE_PEAK_MAX_S)


def test_heating_again_before_the_confirmation_keeps_a_peak_that_is_not_the_last_bin():
    search = PeakSearch(0.0)
    for t, v in ((10.0, 20.0), (310.0, 19.9), (610.0, 19.8)):
        search.feed(t, v)
    assert search.cut_short() == PEAK_CUT_SHORT
    assert search.best_k == 0


def test_heating_again_while_still_rising_gives_no_peak():
    search = PeakSearch(0.0)
    for t, v in ((10.0, 20.0), (310.0, 20.1), (610.0, 20.2)):
        search.feed(t, v)
    assert search.cut_short() == PEAK_NONE


def test_heating_again_without_a_sample_gives_no_peak():
    assert PeakSearch(0.0).cut_short() == PEAK_NONE


def test_cooling_searches_the_minimum():
    search = PeakSearch(0.0, -1.0)
    values = [20.0 + 0.001 * abs((i + 1) * 60.0 - 900.0) for i in range(120)]
    assert _feed(search, values) is not None
    assert abs(search.peak_delay_s - 900.0) <= BIN
    assert search.peak_value == pytest.approx(min(values), abs=0.2)
    assert search.sign == -1.0


def test_mark_of_the_best_bin_is_the_one_given_when_the_bin_closed():
    search = PeakSearch(0.0)
    search.feed(10.0, 20.0, mark=1.0)
    search.feed(310.0, 19.0, mark=2.0)   # closes bin 0 with mark 2
    search.feed(610.0, 19.0, mark=3.0)
    assert search.best_k == 0
    assert search.best_mark == 2.0


def test_samples_before_the_cut_fall_in_the_first_bin():
    search = PeakSearch(100.0)
    search.feed(99.0, 20.0)
    assert search.bin_k == 0


def test_bins_resume_from_their_fields():
    first = PeakSearch(50.0)
    second = PeakSearch(50.0)
    values = _hump(1500.0, 120)
    for i, v in enumerate(values[:40]):
        first.feed(50.0 + (i + 1) * 60.0, v)
        second.feed(50.0 + (i + 1) * 60.0, v)
    resumed = PeakSearch.from_fields(50.0, 1.0, first.fields())
    confirmed = None
    for i, v in enumerate(values[40:], start=40):
        t = 50.0 + (i + 1) * 60.0
        a = second.feed(t, v)
        b = resumed.feed(t, v)
        assert a == b
        if a:
            confirmed = i
            break
    assert confirmed is not None
    assert resumed.peak_delay_s == second.peak_delay_s
    assert resumed.best_mark == second.best_mark


def test_fields_are_plain_numbers():
    search = PeakSearch(0.0)
    search.feed(10.0, 20.0)
    assert set(search.fields()) == {"bin_k", "bin_sum", "bin_n", "best_k", "best_mean", "best_mark", "last_k"}
