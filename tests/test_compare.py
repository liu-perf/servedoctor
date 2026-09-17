"""Comparable first, different second."""
import pytest
from conftest import open_loop_run, req, run_of

from servedoctor import compare as X
from servedoctor import percentile as P


def named(gates, name):
    return [g for g in gates if g.name == name][0]


# ------------------------------------------------------------------------- gates
def test_two_identical_runs_pass_every_gate():
    a = open_loop_run(n=600, rate=5.0)
    b = open_loop_run(n=600, rate=5.0)
    assert all(g.passed for g in X.gates(a, b))


def test_a_different_prompt_length_blocks_the_comparison():
    a = run_of([req(i, i * 0.2, 1.0, tokens_in=512) for i in range(400)])
    b = run_of([req(i, i * 0.2, 1.0, tokens_in=1024) for i in range(400)])
    g = named(X.gates(a, b), "prompt length")
    assert g.passed is False
    assert "+100.0%" in g.detail


def test_a_five_percent_prompt_difference_is_still_within_tolerance():
    a = run_of([req(i, i * 0.2, 1.0, tokens_in=1000) for i in range(400)])
    b = run_of([req(i, i * 0.2, 1.0, tokens_in=1050) for i in range(400)])
    assert named(X.gates(a, b), "prompt length").passed is True


def test_a_different_answer_length_blocks_the_comparison():
    a = run_of([req(i, i * 0.2, 1.0, tokens_out=128) for i in range(400)])
    b = run_of([req(i, i * 0.2, 1.0, tokens_out=256) for i in range(400)])
    assert named(X.gates(a, b), "answer length").passed is False


def test_too_small_a_sample_blocks_the_comparison():
    a = open_loop_run(n=200, rate=5.0)
    b = open_loop_run(n=200, rate=5.0)
    g = named(X.gates(a, b), "sample size")
    assert g.passed is False
    assert "need 299" in g.detail


def test_wildly_unbalanced_samples_are_blocked():
    a = open_loop_run(n=2000, rate=5.0)
    b = open_loop_run(n=350, rate=5.0)
    assert named(X.gates(a, b), "balance").passed is False


def test_comparing_a_closed_run_with_an_open_one_is_blocked(closed_run, open_run):
    assert named(X.gates(closed_run, open_run), "load shape").passed is False


def test_a_summary_report_cannot_be_compared(open_run, summary_run):
    assert named(X.gates(open_run, summary_run), "evidence").passed is False


def test_a_missing_token_column_is_a_failed_gate_not_a_crash():
    a = run_of([req(i, i * 0.2, 1.0, tokens_in=None) for i in range(400)])
    b = open_loop_run(n=400, rate=5.0)
    g = named(X.gates(a, b), "prompt length")
    assert g.passed is False
    assert "not recorded" in g.detail


# ---------------------------------------------------------------------- verdicts
def test_a_blocked_comparison_reports_no_verdict():
    a = open_loop_run(n=200, rate=5.0)
    b = open_loop_run(n=200, rate=5.0)
    c = X.compare(a, b)
    assert c.verdict == X.BLOCKED
    assert c.ratio is None
    assert "not comparable" in c.note


def test_a_real_regression_is_called_a_difference():
    a = open_loop_run(n=800, rate=5.0, latency=1.0)
    b = open_loop_run(n=800, rate=5.0, latency=2.0)
    c = X.compare(a, b)
    assert c.verdict == X.DIFFERS
    assert c.ratio == pytest.approx(2.0, rel=0.1)
    assert "disjoint" in c.note


def test_two_draws_from_the_same_distribution_are_inconclusive_not_same():
    a = open_loop_run(n=800, rate=5.0, latency=1.0, seed=1)
    b = open_loop_run(n=800, rate=5.0, latency=1.0, seed=2)
    c = X.compare(a, b)
    assert c.verdict == X.INCONCLUSIVE
    assert "not evidence of sameness" in c.note


def test_the_word_same_never_appears_as_a_verdict():
    assert X.INCONCLUSIVE == "inconclusive"
    assert "same" not in (X.DIFFERS, X.INCONCLUSIVE, X.BLOCKED)


def test_a_change_smaller_than_the_interval_stays_inconclusive():
    """Conservative in the direction that matters: it will not cry regression.

    The shift is deliberately set below the width of run A's own confidence
    interval. A test that asserted the same verdict for a shift *larger* than the
    interval would be asserting that the tool is blind, which is the opposite of
    what conservative means.
    """
    a = open_loop_run(n=400, rate=5.0, latency=1.0, seed=3)
    width = P.estimate(a.values(lambda r: r.latency_s()), 0.99, 0.90).width_ratio()
    b = open_loop_run(n=400, rate=5.0, latency=1.0 + width / 3.0, seed=4)
    c = X.compare(a, b)
    assert c.verdict == X.INCONCLUSIVE


def test_the_binomial_p_value_is_reported_as_evidence_not_verdict():
    a = open_loop_run(n=800, rate=5.0, latency=1.0)
    b = open_loop_run(n=800, rate=5.0, latency=2.0)
    c = X.compare(a, b)
    assert 0.0 <= c.p_value <= 1.0
    assert c.k_exceeding > (1.0 - c.q) * b.n
    assert "not the verdict" in c.note


def test_the_p_value_is_a_proper_two_sided_binomial():
    # 8 successes in 8 at p=0.5 -- both tails, so 2 * 0.5**8
    assert X._binom_two_sided(8, 8, 0.5) == pytest.approx(2.0 / 256.0)
    assert X._binom_two_sided(4, 8, 0.5) == pytest.approx(1.0)


def test_comparing_a_different_metric_is_supported():
    a = open_loop_run(n=800, rate=5.0, latency=1.0, seed=5)
    b = open_loop_run(n=800, rate=5.0, latency=1.0, seed=6)
    c = X.compare(a, b, metric=lambda r: r.ttft_s())
    assert c.a.value == pytest.approx(0.1)
    assert c.verdict in (X.DIFFERS, X.INCONCLUSIVE)


def test_the_interval_used_is_the_one_from_percentile():
    a = open_loop_run(n=800, rate=5.0, latency=1.0, seed=7)
    b = open_loop_run(n=800, rate=5.0, latency=1.0, seed=8)
    c = X.compare(a, b)
    direct = P.estimate(a.values(lambda r: r.latency_s()), 0.99, 0.90)
    assert c.a.lo == direct.lo and c.a.hi == direct.hi
