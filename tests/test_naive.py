"""The first version, asserted to be STILL WRONG.

`PASSED` in this file does not mean a healthy result. Every assertion here says
that `naive.summarise_naive` continues to produce the plausible, well-formatted,
biased report it produced on the first afternoon -- the same convention as
regressiondoctor's `diff_naive` and fitdoctor's `budget_naive`.

The last test is the control. Without it these tests would only show that the
library disagrees with something, not that it disagrees for the stated reasons.
"""
import pytest
from conftest import req, run_of

from servedoctor import naive as N
from servedoctor import percentile as P
from servedoctor import tokens as T
from servedoctor import window as W


def test_the_naive_p99_is_a_number_where_the_library_refuses_one(short_run):
    out = N.summarise_naive(short_run)
    assert out["p99_latency_s"] > 0            # it happily prints one
    e = P.estimate(short_run.values(lambda r: r.latency_s()), 0.99, 0.90)
    assert e.bounded is False                  # and there is no bound behind it


def test_the_naive_p99_index_is_not_even_the_nearest_rank():
    """int(0.99*n) is a different order statistic from ceil(0.99*n)."""
    vals = [float(i) for i in range(1, 201)]
    run = run_of([req(i, float(i), v) for i, v in enumerate(vals)])
    out = N.summarise_naive(run)
    assert out["p99_latency_s"] == vals[int(0.99 * 200)]      # index 198 -> 199.0
    assert P.quantile(vals, 0.99) == vals[197]                # rank 198 -> 198.0
    assert out["p99_latency_s"] != P.quantile(vals, 0.99)


def test_the_naive_token_rate_is_the_one_that_moves_with_prompt_length(
        short_answers_run):
    out = N.summarise_naive(short_answers_run)
    view = T.analyse(short_answers_run)
    assert out["tokens_per_s"] == pytest.approx(view.total_tps, rel=1e-9)
    assert out["tokens_per_s"] > 100 * view.output_tps


def test_the_naive_tpot_understates_by_m_over_m_minus_one(short_answers_run):
    out = N.summarise_naive(short_answers_run)
    view = T.analyse(short_answers_run)
    assert out["mean_tpot_s"] < view.tpot_median
    assert view.tpot_error == pytest.approx(1.0 / 3.0, rel=1e-3)


def test_the_naive_mean_hides_a_tail_the_quantile_does_not(open_run):
    out = N.summarise_naive(open_run)
    e = P.estimate(open_run.values(lambda r: r.latency_s()), 0.99, 0.90)
    assert e.value > 3 * out["mean_latency_s"]


def test_the_naive_summary_counts_censored_requests_out_without_saying_so(
        censored_run):
    out = N.summarise_naive(censored_run)
    c = W.censoring(censored_run, 0.99)
    assert out["requests"] == c.n_total - c.n_dropped
    assert c.recoverable is False, "and the q99 it printed is not recoverable"


def test_the_naive_summary_reports_a_p99_on_a_closed_loop_without_comment(
        closed_run):
    from servedoctor import closure as C
    out = N.summarise_naive(closed_run)
    assert out["p99_latency_s"] > 0
    assert C.classify(closed_run).kind == C.CLOSED_LOOP


def test_a_run_with_no_completions_returns_nothing_rather_than_dividing_by_zero():
    assert N.summarise_naive(run_of([req(1, 0.0, None)])) is None


def test_the_control_the_request_count_agrees(open_run):
    """The one honest number in the naive report, and the reason it is asserted.

    If every comparison above failed, these tests would prove only that two pieces
    of code disagree. This one fixes the direction: they agree where they should.
    """
    out = N.summarise_naive(open_run)
    assert out["requests"] == len(open_run.ok_requests()) == open_run.n


def test_the_naive_mean_is_the_same_mean(open_run):
    """A second control: the arithmetic mean is not what this library disputes."""
    out = N.summarise_naive(open_run)
    assert out["mean_latency_s"] == pytest.approx(
        P.mean(open_run.values(lambda r: r.latency_s())), rel=1e-12)
