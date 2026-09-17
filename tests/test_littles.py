"""Little's Law -- including a test whose whole job is to keep it from being a check.

The identity assertion below is the load-bearing one. telemetrydoctor shipped a
cross-check whose two "independent" methods were algebraically the same expression,
and the test that caught it computed both side by side and asserted they agreed
exactly. This is that test, for this library.
"""
import pytest
from conftest import closed_loop_run, open_loop_run, req, run_of

from servedoctor import closure as C
from servedoctor import littles as L


def test_littles_law_is_an_identity_on_per_request_data():
    """lambda*W and the in-flight integral are one quantity computed twice.

    lambda = n/T, W = sum(lat)/n, so lambda*W = sum(lat)/T -- which is exactly the
    time-average of the in-flight count. Verified to floating point on three runs
    of different shape, so nobody can later present this agreement as evidence of
    anything.
    """
    for run in (closed_loop_run(workers=5, cycles=30),
                open_loop_run(n=300, rate=4.0),
                open_loop_run(n=300, rate=25.0, latency=2.0)):
        span = run.span_s()
        lats = run.values(lambda r: r.latency_s())
        lam = len(lats) / span
        w = sum(lats) / len(lats)
        integral = sum(lats) / span
        assert lam * w == pytest.approx(integral, rel=1e-12)


def test_the_sweep_and_the_identity_agree_because_they_must(open_run):
    """The event sweep is a third route to the same number, not a fourth check."""
    lt = L.analyse(open_run)
    occ = C.occupancy(open_run)
    assert lt.l_identity == pytest.approx(occ.mean_in_flight, rel=1e-6)


def test_without_a_declared_concurrency_the_result_is_labelled_identity(open_run):
    lt = L.analyse(open_run)
    assert lt.kind == L.IDENTITY
    assert "not a cross-check" in lt.note
    assert L.status(lt) == "info"


def test_with_a_declared_concurrency_it_becomes_a_check_that_can_fail(open_run):
    lt = L.analyse(open_run, declared=7)
    assert lt.kind == L.CHECK
    assert lt.fill > 3.0
    assert L.status(lt) == "violation"


def test_a_closed_loop_fills_its_declared_concurrency_exactly(closed_run):
    lt = L.analyse(closed_run, declared=7)
    assert lt.fill == pytest.approx(1.0, abs=0.02)
    assert L.status(lt) == "ok"


def test_a_client_that_never_filled_its_pool_is_a_violation():
    """The check that finds the load generator, not the server, running out."""
    run = open_loop_run(n=300, rate=1.0, latency=0.5)   # about 0.5 in flight
    lt = L.analyse(run, declared=32)
    assert lt.fill < L.UNDERFILL_VIOLATION
    assert L.status(lt) == "violation"


def test_a_slightly_underfilled_pool_is_a_warning_not_a_violation():
    run = closed_loop_run(workers=10, cycles=40)
    lt = L.analyse(run, declared=11)
    assert L.UNDERFILL_VIOLATION <= lt.fill < L.UNDERFILL_WARN
    assert L.status(lt) == "warn"


def test_no_timestamps_means_unavailable():
    run = run_of([req(1, 0.0, None)])
    assert L.analyse(run).kind == L.UNAVAILABLE
    assert L.status(L.analyse(run)) == "unknown"


# ------------------------------------------------------------- on summary reports
def test_on_a_summary_the_three_numbers_come_from_different_places(summary_run):
    lt = L.analyse(summary_run)
    assert lt.kind == L.CHECK
    assert lt.declared == 7
    assert lt.l_identity > 20, "6.278 req/s x 4.34 s is about 27 in flight"
    assert L.status(lt) == "violation"


def test_the_summary_check_would_pass_on_a_consistent_report(tmp_path):
    import json
    import os

    from servedoctor import parse
    p = os.path.join(str(tmp_path), "s.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"request_throughput": 8.0, "mean_e2el_ms": 1000.0,
                   "max_concurrency": 8}, fh)
    lt = L.analyse(parse.load(p))
    assert lt.fill == pytest.approx(1.0)
    assert L.status(lt) == "ok"


def test_throughput_is_derived_when_the_report_omits_it(tmp_path):
    import json
    import os

    from servedoctor import parse
    p = os.path.join(str(tmp_path), "s.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"completed": 100, "duration": 20.0, "mean_e2el_ms": 400.0,
                   "max_concurrency": 2}, fh)
    lt = L.analyse(parse.load(p))
    assert lt.lam == pytest.approx(5.0)
    assert lt.l_identity == pytest.approx(2.0)


def test_a_summary_without_latency_cannot_be_checked(tmp_path):
    import json
    import os

    from servedoctor import parse
    p = os.path.join(str(tmp_path), "s.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"request_throughput": 8.0, "max_concurrency": 8}, fh)
    lt = L.analyse(parse.load(p))
    assert lt.kind == L.UNAVAILABLE
