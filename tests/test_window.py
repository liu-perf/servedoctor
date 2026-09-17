"""Warm-up, the steady window, and the requests that were dropped before counting."""
import pytest
from conftest import open_loop_run, req, run_of

from servedoctor import percentile as P
from servedoctor import window as W


# ------------------------------------------------------------------------ warm-up
def test_the_cold_start_fixture_is_caught_and_quantified(cold_start_run):
    w = W.warmup(cold_start_run, 0.99)
    assert w.count == 1
    assert w.factor > 8.0
    assert w.shift > 2.0, "dropping it more than triples the q99 of a 60-request run"


def test_a_single_cold_start_can_only_be_the_p99_below_a_hundred_requests():
    """The arithmetic behind the fixture being 60 requests long, not 500."""
    assert P.rank_for(60, 0.99) == 60          # the maximum IS the q99
    assert P.rank_for(99, 0.99) == 99
    assert P.rank_for(100, 0.99) == 99         # from here the maximum drops out
    assert P.rank_for(500, 0.99) == 495


def test_a_warm_deployment_reports_no_warmup(open_run):
    w = W.warmup(open_run, 0.99)
    assert w.count == 0
    assert w.shift is None
    assert "already warm" in w.note


def test_only_leading_requests_count_as_warmup():
    """A slow request in the middle is a stall, not a cold start."""
    reqs = [req(i, float(i), 1.0) for i in range(40)]
    reqs[20] = req(20, 20.0, 50.0)
    w = W.warmup(run_of(reqs), 0.99)
    assert w.count == 0


def test_warmup_will_not_swallow_more_than_a_tenth_of_the_run():
    # 15 slow leading requests out of 40 -- fewer than half, so the median is still
    # the fast value and every one of them qualifies. The cap stops at 4 anyway: a
    # run that is 37% warm-up is a transient, not a steady-state measurement with a
    # warm-up attached, and trimming it silently would hide that.
    reqs = [req(i, float(i), 100.0 if i < 15 else 1.0) for i in range(40)]
    w = W.warmup(run_of(reqs), 0.99)
    assert w.count == int(40 * W.MAX_WARMUP_FRACTION) == 4


def test_warmup_needs_ten_requests():
    assert W.warmup(run_of([req(i, float(i), 1.0) for i in range(9)])) is None


# ------------------------------------------------------------------ steady window
def test_the_steady_window_covers_most_of_a_healthy_run(open_run):
    s = W.steady(open_run)
    assert s.fraction > 0.8
    assert s.dropped_tail == 0
    assert s.t1 > s.t0


def test_the_steady_window_is_referenced_to_the_median_not_the_peak(open_run):
    """The first version used the peak and selected 20s of a 318s run.

    The peak in-flight count on this file is a queue spike after a stall. A window
    defined against it calls the other 94% of the run ramp-up, which is the same
    class of bug as telemetrydoctor's cross-check: correct arithmetic on a quantity
    that was not the one the name promised.
    """
    from servedoctor import closure as C
    occ = C.occupancy(open_run)
    peak_floor = W.STEADY_OCCUPANCY * occ.max_in_flight
    median_floor = W.STEADY_OCCUPANCY * occ.median_in_flight
    assert peak_floor > 3 * median_floor
    s = W.steady(open_run)
    assert (s.t1 - s.t0) > 0.8 * open_run.span_s()


def test_a_serial_run_has_no_steady_window_to_find():
    reqs = [req(i, float(i), 0.5) for i in range(40)]   # never two at once
    assert W.steady(run_of(reqs)) is None


def test_ramp_up_is_reported_as_dropped_head():
    run = open_loop_run(n=400, rate=6.0, latency=1.0)
    s = W.steady(run)
    assert s is not None
    assert s.dropped_head >= 0
    assert s.kept + s.dropped_head + s.dropped_tail == len(run.ok_requests())


# --------------------------------------------------------------------- censoring
def test_two_percent_timeouts_delete_the_p99_rather_than_perturb_it(censored_run):
    c = W.censoring(censored_run, 0.99)
    assert c.n_dropped == 12
    assert c.fraction == pytest.approx(0.02)
    assert c.recoverable is False
    assert c.corrected is None
    assert "lies inside it" in c.note


def test_the_same_censoring_still_allows_a_lower_quantile(censored_run):
    c = W.censoring(censored_run, 0.95)
    assert c.recoverable is True
    assert c.effective_q == pytest.approx(0.95 / 0.98)
    assert c.corrected >= c.observed


def test_a_complete_run_needs_no_correction(open_run):
    c = W.censoring(open_run, 0.99)
    assert c.n_dropped == 0
    assert c.corrected == c.observed
    assert c.recoverable is True


def test_the_correction_is_a_quantile_remap_not_a_fudge():
    """10 of 100 dropped: the population q80 is the survivors' q88.9."""
    reqs = [req(i, float(i), float(i + 1)) for i in range(90)]
    reqs += [req(90 + i, float(90 + i), None, ok=False) for i in range(10)]
    c = W.censoring(run_of(reqs), 0.80)
    assert c.fraction == pytest.approx(0.10)
    assert c.effective_q == pytest.approx(0.80 / 0.90)
    assert c.corrected == P.quantile([float(i + 1) for i in range(90)], 0.80 / 0.90)


def test_a_request_that_failed_is_censored_even_with_a_completion_time():
    reqs = [req(i, float(i), 1.0) for i in range(99)]
    reqs.append(req(99, 99.0, 1.0, ok=False))
    c = W.censoring(run_of(reqs), 0.99)
    assert c.n_dropped == 1


def test_censoring_on_an_empty_run_is_none():
    assert W.censoring(run_of([]), 0.99) is None
