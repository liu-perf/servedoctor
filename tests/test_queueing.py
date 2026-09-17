"""The utilisation/latency trade-off, and the refusal to estimate what is not there."""
import pytest
from conftest import open_loop_run, req, run_of

from servedoctor import queueing as Q


# ------------------------------------------------------------------- the formulas
@pytest.mark.parametrize("rho,ratio", [(0.5, 1.0), (0.8, 4.0), (0.9, 9.0),
                                       (0.95, 19.0), (0.99, 99.0)])
def test_mm1_wait_is_rho_over_one_minus_rho_service_times(rho, ratio):
    assert Q.mm1_wait(rho, 1.0) == pytest.approx(ratio)


def test_md1_is_exactly_half_of_mm1():
    for rho in (0.1, 0.5, 0.8, 0.95):
        assert Q.md1_wait(rho, 1.0) == pytest.approx(Q.mm1_wait(rho, 1.0) / 2.0)


def test_pk_reduces_to_mm1_at_cv_one_and_to_md1_at_cv_zero():
    assert Q.mg1_wait(0.8, 1.0, 1.0) == pytest.approx(Q.mm1_wait(0.8, 1.0))
    assert Q.mg1_wait(0.8, 1.0, 0.0) == pytest.approx(Q.md1_wait(0.8, 1.0))


def test_md1_is_a_floor_for_every_service_distribution():
    """Deterministic service minimises E[S^2] at fixed E[S], so it minimises the wait."""
    floor = Q.md1_wait(0.85, 1.0)
    for cv in (0.0, 0.2, 0.5, 1.0, 3.0):
        assert Q.mg1_wait(0.85, 1.0, cv) >= floor - 1e-12


def test_saturation_and_overload_return_nothing_rather_than_a_negative_number():
    assert Q.mm1_wait(1.0, 1.0) is None
    assert Q.mm1_wait(1.7, 1.0) is None
    assert Q.md1_wait(-0.1, 1.0) is None


def test_the_tradeoff_table_is_monotone_and_matches_the_documented_row():
    rows = Q.tradeoff_table(1.0)
    waits = [mm1 for _rho, mm1, _md1 in rows]
    assert waits == sorted(waits)
    assert dict((r, m) for r, m, _d in rows)[0.95] == pytest.approx(19.0)


def test_headroom_inverts_the_table():
    assert Q.headroom_for(1.0) == pytest.approx(0.5)
    assert Q.headroom_for(9.0) == pytest.approx(0.9)
    assert Q.headroom_for(0.1) == pytest.approx(1.0 / 11.0)
    assert Q.headroom_for(None) is None


def test_more_servers_wait_less_at_the_same_utilisation():
    """The honest counterweight to the 1/(1-rho) scare."""
    # 16 req/s, 1 s service -> offered load 16 erlangs. At 20 servers (rho=0.8) the
    # mean wait is far below one service time, which a single server at 0.8 is not.
    c = Q.erlang_c_servers(lam=16.0, service_s=1.0, target_wait_s=0.05)
    assert c is not None
    assert c < 16 * 2
    assert c > 16, "below 17 servers the system is not stable at all"


def test_erlang_needs_its_three_inputs():
    assert Q.erlang_c_servers(None, 1.0, 0.1) is None
    assert Q.erlang_c_servers(1.0, None, 0.1) is None
    assert Q.erlang_c_servers(1.0, 1.0, None) is None


# -------------------------------------------------------------------- on real runs
def test_utilisation_is_refused_without_a_capacity(open_run):
    """An earlier version clamped lambda*E[S] to 0.999 and called it utilisation.

    That produced a 398-second M/D/1 "floor" on a run whose entire tail was 13
    seconds -- a number that is not slightly wrong but about a different system.
    """
    qq = Q.analyse(open_run)
    assert qq.rho is None
    assert qq.md1_s is None
    assert qq.offered_concurrency > 1.0
    assert "not computed" in qq.note


def test_with_a_capacity_the_utilisation_and_the_bounds_appear(open_run):
    qq = Q.analyse(open_run, capacity_rps=8.0)
    assert 0.0 < qq.rho < 1.0
    assert qq.mm1_s > qq.md1_s > 0
    assert "Utilisation" in qq.note


def test_offered_concurrency_is_lambda_times_service(open_run):
    qq = Q.analyse(open_run)
    assert qq.offered_concurrency == pytest.approx(qq.lam * qq.service_s)


def test_the_service_proxy_is_a_low_quantile_not_the_minimum(open_run):
    qq = Q.analyse(open_run)
    lats = sorted(open_run.values(lambda r: r.latency_s()))
    assert qq.service_s > lats[0]
    assert qq.service_s < lats[len(lats) // 2]


def test_the_consistency_triangle_fires_only_with_a_capacity_and_poisson_arrivals():
    run = open_loop_run(n=600, rate=6.0, latency=1.0, jitter=0.05)
    without = Q.analyse(run)
    assert without.triangle is None
    withcap = Q.analyse(run, capacity_rps=6.3)     # rho ~ 0.95, almost no queueing
    assert withcap.triangle is not None
    assert "One of the three is false" in withcap.triangle


def test_the_triangle_stays_silent_at_low_utilisation():
    run = open_loop_run(n=600, rate=6.0, latency=1.0, jitter=0.05)
    qq = Q.analyse(run, capacity_rps=60.0)         # rho = 0.1
    assert qq.rho < Q.TRIANGLE_MIN_RHO
    assert qq.triangle is None


def test_a_summary_report_has_nothing_to_analyse(summary_run):
    assert Q.analyse(summary_run) is None


def test_a_run_without_latencies_returns_nothing():
    assert Q.analyse(run_of([req(1, 0.0, None)])) is None
