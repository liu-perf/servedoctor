"""Attainment, goodput, and the rung a throughput-only report would recommend."""
import pytest
from conftest import fx, req, run_of

from servedoctor import parse
from servedoctor import slo as S


def target(**kw):
    return S.Target(**kw)


# ------------------------------------------------------------------- the deadline
def test_a_target_with_nothing_set_is_not_a_target():
    assert target().any_set() is False
    assert target(ttft_ms=100).any_set() is True


def test_the_target_describes_itself_including_the_attainment():
    d = target(ttft_ms=500, tpot_ms=50).describe()
    assert "TTFT<=500ms" in d and "TPOT<=50ms" in d and "99%" in d


def test_meets_is_conjunctive():
    r = req(1, 0.0, 2.0, ttft=0.1, tokens_out=11)   # ttft 100ms, tpot 190ms
    assert S.meets(r, target(ttft_ms=200)) is True
    assert S.meets(r, target(tpot_ms=100)) is False
    assert S.meets(r, target(ttft_ms=200, tpot_ms=100)) is False


def test_a_request_missing_the_metric_is_undecidable_not_a_pass():
    r = req(1, 0.0, 2.0, ttft=None, tokens_out=11)
    assert S.meets(r, target(ttft_ms=200)) is None


def test_a_target_naming_nothing_the_request_has_returns_none():
    r = req(1, 0.0, 2.0)
    assert S.meets(r, target()) is None


# ------------------------------------------------------------------- attainment
def test_attainment_is_the_share_of_requests_that_met_every_deadline():
    reqs = [req(i, float(i), 1.0, ttft=0.05 if i < 90 else 0.5) for i in range(100)]
    att = S.attainment(run_of(reqs), target(ttft_ms=100))
    assert att.n_met == 90
    assert att.fraction == pytest.approx(0.90)


def test_goodput_counts_only_the_requests_worth_having():
    reqs = [req(i, float(i), 1.0, ttft=0.05 if i < 50 else 0.5) for i in range(100)]
    run = run_of(reqs)
    att = S.attainment(run, target(ttft_ms=100))
    assert att.goodput == pytest.approx(att.throughput / 2.0, rel=0.05)


def test_a_failed_request_meets_nothing():
    reqs = [req(i, float(i), 1.0, ttft=0.01, ok=(i > 0)) for i in range(100)]
    att = S.attainment(run_of(reqs), target(ttft_ms=100))
    assert att.n_met == 99
    assert att.n == 100


def test_status_bands():
    t = target(ttft_ms=100, attainment=0.99)
    reqs = [req(i, float(i), 1.0, ttft=0.01) for i in range(100)]
    assert S.status(S.attainment(run_of(reqs), t), t) == "ok"
    reqs = [req(i, float(i), 1.0, ttft=0.01 if i < 96 else 1.0) for i in range(100)]
    assert S.status(S.attainment(run_of(reqs), t), t) == "warn"
    reqs = [req(i, float(i), 1.0, ttft=0.01 if i < 80 else 1.0) for i in range(100)]
    assert S.status(S.attainment(run_of(reqs), t), t) == "violation"


def test_undecidable_requests_do_not_count_against_attainment():
    reqs = [req(i, float(i), 1.0, ttft=0.01 if i < 50 else None) for i in range(100)]
    att = S.attainment(run_of(reqs), target(ttft_ms=100))
    assert att.n_undecided == 50
    assert att.fraction == pytest.approx(1.0)


def test_attainment_from_the_due_time_is_worse_on_a_backlogged_run(backlog_run):
    t = target(e2e_ms=5000)
    a = S.attainment(backlog_run, t)
    b = S.attainment(backlog_run, t, use_corrected=True)
    assert b.fraction < a.fraction


def test_a_summary_report_cannot_be_scored(summary_run):
    assert S.attainment(summary_run, target(ttft_ms=100)) is None


# ------------------------------------------------------- the mean is not the SLO
def test_the_mean_can_pass_while_the_quantile_fails():
    reqs = [req(i, float(i), 1.0, ttft=0.05 if i < 95 else 2.0) for i in range(100)]
    fooled = S.mean_would_have_passed(run_of(reqs), target(ttft_ms=200))
    assert len(fooled) == 1
    name, mean_ms, q_ms, limit, _e = fooled[0]
    assert name == "TTFT"
    assert mean_ms < limit < q_ms


def test_nothing_is_reported_when_both_agree():
    reqs = [req(i, float(i), 1.0, ttft=0.05) for i in range(100)]
    assert S.mean_would_have_passed(run_of(reqs), target(ttft_ms=200)) == []


# ------------------------------------------------------------------------ sweep
def ladder():
    names = ["EXAMPLE_ladder_40.csv", "EXAMPLE_ladder_70.csv",
             "EXAMPLE_ladder_95.csv", "EXAMPLE_ladder_114.csv"]
    return [(n, parse.load(fx(n))) for n in names]


def test_the_ladder_is_ordered_by_offered_rate():
    rungs, _t, _g = S.sweep(ladder(), target(e2e_ms=3000))
    rates = [r.rate for r in rungs]
    assert rates == sorted(rates)


def test_throughput_keeps_rising_past_where_goodput_turns_over():
    rungs, best_t, best_g = S.sweep(ladder(), target(e2e_ms=3000))
    assert best_t is not None and best_g is not None
    assert best_t > best_g, (
        "the rung a throughput-only report calls best is above the goodput peak")
    assert rungs[best_t].goodput < rungs[best_g].goodput


def test_the_tail_grows_along_the_ladder():
    rungs, _t, _g = S.sweep(ladder(), target(e2e_ms=3000))
    assert rungs[-1].p99_s > rungs[0].p99_s


def test_sweep_without_a_target_still_reports_throughput():
    rungs, best_t, best_g = S.sweep(ladder(), target())
    assert all(r.throughput is not None for r in rungs)
    assert best_t is not None
    assert best_g is None
