"""The tail statistics, pinned exactly.

Everything here is a deterministic function of n and q, which is the reason this
module was written by hand instead of imported: a number that cannot be reproduced
without an environment cannot be argued about in a review.
"""
import math

import pytest

from servedoctor import percentile as P


# ------------------------------------------------------------------ point values
def test_nearest_rank_is_ceil_of_q_times_n():
    assert P.rank_for(100, 0.99) == 99
    assert P.rank_for(200, 0.99) == 198
    assert P.rank_for(1000, 0.99) == 990
    assert P.rank_for(50, 0.99) == 50


def test_rank_never_leaves_the_sample():
    for n in (1, 2, 3, 7, 999):
        assert 1 <= P.rank_for(n, 0.999) <= n
        assert 1 <= P.rank_for(n, 0.001) <= n


def test_quantile_returns_an_observation_not_an_interpolation():
    vals = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert P.quantile(vals, 0.99) in vals
    assert P.quantile(vals, 0.5) in vals


def test_median_averages_the_middle_pair_on_an_even_sample():
    assert P.median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert P.median([1.0, 2.0, 3.0]) == 2.0


def test_mean_and_cv_on_a_known_sample():
    vals = [10.0, 12.0, 14.0]
    assert P.mean(vals) == 12.0
    assert abs(P.stdev(vals) - 2.0) < 1e-12
    assert abs(P.cv_pct(vals) - 100.0 * 2.0 / 12.0) < 1e-9


def test_stdev_needs_two_points():
    assert P.stdev([1.0]) is None
    assert P.mean([]) is None


def test_tail_support_counts_observations_at_or_above_the_quantile():
    assert P.tail_support(100, 0.99) == 2
    assert P.tail_support(200, 0.99) == 3
    assert P.tail_support(1000, 0.99) == 11
    assert P.tail_support(2000, 0.99) == 21


# --------------------------------------------------------------- the binomial CDF
def test_binom_cdf_is_a_distribution():
    cdf = P.binom_cdf(50, 0.9)
    assert abs(cdf[-1] - 1.0) < 1e-12
    assert all(b >= a - 1e-15 for a, b in zip(cdf, cdf[1:]))


def test_binom_cdf_survives_a_tail_that_would_underflow_in_linear_space():
    # (1-p)**n for p=0.999, n=5000 is about 1e-15000: representable only in logs.
    cdf = P.binom_cdf(5000, 0.999)
    assert cdf[0] >= 0.0
    assert abs(cdf[-1] - 1.0) < 1e-9


# -------------------------------------------------------- the confidence interval
def test_the_p99_of_200_requests_has_no_upper_bound_in_its_own_sample():
    lo, hi = P.ci_ranks(200, 0.99, 0.90)
    assert hi == 201, "hi rank must fall off the top of a 200-request sample"
    assert lo < 200


def test_299_is_exactly_where_the_upper_bound_arrives():
    assert P.ci_ranks(298, 0.99, 0.90)[1] == 299     # one past the end
    assert P.ci_ranks(299, 0.99, 0.90)[1] == 299     # the last observation
    assert P.min_n_for(0.99, 0.90) == 299


def test_min_n_matches_the_closed_form_it_claims():
    for q in (0.90, 0.95, 0.99, 0.999):
        for conf in (0.80, 0.90, 0.95, 0.99):
            n = P.min_n_for(q, conf)
            alpha = 1.0 - conf
            assert q ** n <= alpha / 2.0 + 1e-15
            assert q ** (n - 1) > alpha / 2.0
            # and the rank search agrees with the algebra
            assert P.ci_ranks(n, q, conf)[1] <= n
            assert P.ci_ranks(n - 1, q, conf)[1] > n - 1


def test_the_documented_sample_sizes():
    assert P.min_n_for(0.99, 0.90) == 299
    assert P.min_n_for(0.99, 0.95) == 368
    assert P.min_n_for(0.999, 0.90) == 2995
    assert P.min_n_for(0.95, 0.90) == 59


def test_interval_actually_covers_at_the_stated_confidence():
    for n in (60, 299, 500, 1000, 2000):
        cdf = P.binom_cdf(n, 0.99)
        lo, hi = P.ci_ranks(n, 0.99, 0.90)
        if hi > n:
            continue
        coverage = cdf[hi - 1] - cdf[lo - 1]
        assert coverage >= 0.90 - 1e-12, (n, coverage)


def test_coverage_is_conservative_not_exact():
    # Discreteness means the interval over-covers; that direction is the safe one,
    # and a test that demanded equality would be asserting a bug.
    cdf = P.binom_cdf(1000, 0.99)
    lo, hi = P.ci_ranks(1000, 0.99, 0.90)
    assert cdf[hi - 1] - cdf[lo - 1] > 0.90


def test_max_q_inverts_min_n():
    # Asserted on the defining inequality rather than by round-tripping through
    # min_n_for: max_q_for lands exactly on the boundary q**n == alpha/2, where a
    # round trip can come back one larger purely from floating point.
    for n in (60, 200, 299, 1000):
        q = P.max_q_for(n, 0.90)
        assert q ** n <= 0.05 * (1.0 + 1e-9)
        assert min(0.99999, q + 1e-4) ** n > 0.05
        assert P.min_n_for(q, 0.90) in (n, n + 1)


def test_200_requests_can_carry_a_q985():
    q = P.max_q_for(200, 0.90)
    assert 0.985 <= q <= 0.986


# --------------------------------------------------------------------- Estimate
def test_estimate_reports_unbounded_below_the_threshold():
    e = P.estimate([float(i) for i in range(200)], 0.99, 0.90)
    assert e.bounded is False
    assert e.hi is None
    assert e.min_n == 299
    assert "UNBOUNDED" in str(e)


def test_estimate_reports_an_interval_above_the_threshold():
    e = P.estimate([float(i) for i in range(2000)], 0.99, 0.90)
    assert e.bounded is True
    assert e.lo <= e.value <= e.hi
    assert e.lo_rank < e.hi_rank
    assert e.support == 21
    assert "CI" in str(e)


def test_estimate_interval_narrows_as_the_sample_grows():
    widths = []
    for n in (400, 1000, 4000):
        vals = [math.sin(i) + 2.0 for i in range(n)]
        e = P.estimate(vals, 0.99, 0.90)
        widths.append(e.width_ratio())
    assert widths[0] > widths[1] > widths[2]


def test_estimate_on_an_empty_sample_says_so_instead_of_raising():
    e = P.estimate([], 0.99)
    assert e.value is None
    assert "no data" in str(e)


def test_a_wider_confidence_needs_a_bigger_sample():
    e90 = P.estimate([float(i) for i in range(300)], 0.99, 0.90)
    e95 = P.estimate([float(i) for i in range(300)], 0.99, 0.95)
    assert e90.bounded is True
    assert e95.bounded is False, "368 needed at 95%, only 300 given"


# ---------------------------------------------------------------------- merging
def test_merging_quantiles_is_refused_rather_than_approximated():
    with pytest.raises(P.MergeRefused):
        P.merge_quantiles(1.0, 2.0)


def test_merging_samples_is_the_supported_operation():
    a = [1.0, 2.0, 3.0]
    b = [10.0, 20.0]
    assert sorted(P.merge_samples(a, b)) == [1.0, 2.0, 3.0, 10.0, 20.0]


def test_the_average_of_two_p99s_is_not_the_p99_of_the_union():
    """Why merge_quantiles refuses: a worked counter-example, not an assertion."""
    a = [1.0] * 50 + [100.0] * 50        # half the requests are slow
    b = [1.0] * 100                      # none of them are
    qa, qb = P.quantile(a, 0.99), P.quantile(b, 0.99)
    union = P.quantile(P.merge_samples(a, b), 0.99)
    assert (qa, qb) == (100.0, 1.0)
    assert union == 100.0
    assert (qa + qb) / 2.0 == 50.5
    assert (qa + qb) / 2.0 != union, "averaging the two halves the answer"
