"""Coordinated omission: the exact correction, and the refusal to invent one."""
import pytest
from conftest import closed_loop_run, req, run_of

from servedoctor import omission as O


def test_the_backlog_fixture_has_a_real_correction(backlog_run):
    o = O.analyse(backlog_run, 0.99)
    assert o.available is True
    assert o.factor > 2.0
    assert o.late_fraction > 0.8
    assert O.status(o.factor) == "violation"


def test_a_harness_that_kept_up_shows_a_factor_of_one(open_run):
    o = O.analyse(open_run, 0.99)
    assert o.available is True
    assert o.factor == pytest.approx(1.0)
    assert o.late_fraction == pytest.approx(0.0)
    assert O.status(o.factor) == "ok"


def test_without_a_schedule_the_correction_is_refused_not_estimated(closed_run):
    o = O.analyse(closed_run, 0.99)
    assert o.available is False
    assert o.factor is None
    assert "no schedule column" in o.reason


def test_a_summary_report_cannot_be_corrected(summary_run):
    o = O.analyse(summary_run, 0.99)
    assert o.available is False
    assert "summary" in o.reason


def test_the_correction_is_exact_on_a_hand_built_case():
    # three requests, each due at 0/1/2 but all sent at 3 because the harness stalled
    reqs = [req(i, start=3.0 + i * 0.001, latency=1.0, arrival=float(i))
            for i in range(3)]
    o = O.analyse(run_of(reqs), 0.99)
    assert o.raw.value == pytest.approx(1.0, abs=1e-3)
    # request 0 was due at t=0, sent at t=3.0, done at t=4.0
    assert o.corrected.value == pytest.approx(4.0, abs=1e-3)
    assert o.max_wait_s == pytest.approx(3.0, abs=1e-3)
    assert o.factor == pytest.approx(4.0, abs=1e-2)


def test_late_counts_only_requests_the_harness_actually_delayed():
    reqs = [req(0, 0.0, 1.0, arrival=0.0),
            req(1, 1.0, 1.0, arrival=1.0),
            req(2, 5.0, 1.0, arrival=2.0)]
    o = O.analyse(run_of(reqs), 0.5)
    assert o.late_fraction == pytest.approx(1.0 / 3.0)


def test_sub_millisecond_scheduling_jitter_is_not_backlog():
    reqs = [req(i, i * 0.1 + 1e-5, 1.0, arrival=i * 0.1) for i in range(50)]
    o = O.analyse(run_of(reqs), 0.5)
    assert o.late_fraction == 0.0


# -------------------------------------------------------------------- the bound
def test_deficit_counts_the_requests_that_were_never_sent():
    run = closed_loop_run(workers=4, cycles=25)
    span = run.span_s()
    expected, actual, missing = O.deficit(run, target_rate=20.0)
    assert expected == pytest.approx(20.0 * span)
    assert actual == run.n
    assert missing > 0


def test_deficit_is_zero_when_the_harness_kept_up(open_run):
    rate = open_run.n / open_run.span_s()
    _expected, _actual, missing = O.deficit(open_run, target_rate=rate)
    assert missing == pytest.approx(0.0, abs=1.0)


def test_deficit_needs_a_target_rate():
    assert O.deficit(closed_loop_run(), target_rate=None) is None


def test_status_thresholds():
    assert O.status(None) == "unknown"
    assert O.status(1.0) == "ok"
    assert O.status(1.05) == "ok"
    assert O.status(1.2) == "warn"
    assert O.status(1.6) == "violation"


def test_the_corrected_quantile_is_never_smaller_than_the_raw_one(backlog_run):
    """end - arrival >= end - start whenever start >= arrival, request by request."""
    for r in backlog_run.requests:
        if r.corrected_latency_s() is None:
            continue
        assert r.corrected_latency_s() >= r.latency_s() - 1e-12
    o = O.analyse(backlog_run, 0.99)
    assert o.corrected.value >= o.raw.value
