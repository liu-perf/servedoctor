"""Open loop or closed loop, decided from the log."""
import pytest
from conftest import closed_loop_run, open_loop_run, req, run_of

from servedoctor import closure as C


# ------------------------------------------------------------------- the verdicts
def test_the_closed_loop_fixture_is_called_closed(closed_run):
    v = C.classify(closed_run)
    assert v.kind == C.CLOSED_LOOP
    assert v.workers == 7
    assert v.score == pytest.approx(0.0, abs=1e-9)


def test_the_open_loop_fixture_is_called_open(open_run):
    v = C.classify(open_run)
    assert v.kind == C.OPEN_LOOP
    assert v.score > C.OPEN_MIN_SCORE


def test_the_open_loop_coincidence_score_lands_near_the_predicted_one(open_run):
    """Two independent streams of equal rate: the theory says about 1.0."""
    v = C.classify(open_run)
    assert 0.8 < v.score < 1.3


def test_a_summary_report_cannot_answer_this(summary_run):
    v = C.classify(summary_run)
    assert v.kind == C.UNKNOWN
    assert "summary" in v.reason


def test_a_rate_limited_harness_that_hit_its_cap_reads_as_closed(backlog_run):
    """Not a false positive: past the cap it really is a closed loop.

    The file was generated as an open-loop schedule with a 24-request client limit.
    Once the server slowed past that limit, every send waited for a completion --
    which is the definition. The schedule column is what still distinguishes it,
    and SD003 is where that shows up.
    """
    v = C.classify(backlog_run)
    assert v.kind == C.CLOSED_LOOP
    assert v.workers == 24
    assert backlog_run.has_field("arrival_s")


# ------------------------------------------------------------------- the detectors
def test_occupancy_finds_the_worker_count_exactly():
    run = closed_loop_run(workers=5, cycles=40)
    occ = C.occupancy(run)
    assert occ.max_in_flight == 5
    assert occ.at_max_fraction > 0.9


def test_occupancy_median_is_not_the_maximum_on_a_spiky_run(open_run):
    occ = C.occupancy(open_run)
    assert occ.median_in_flight < occ.max_in_flight / 2
    assert occ.mean_in_flight < occ.max_in_flight


def test_a_completion_and_a_send_at_the_same_instant_is_one_in_flight():
    """Ends are applied before starts at equal timestamps."""
    run = run_of([req(1, 0.0, 1.0), req(2, 1.0, 1.0)])
    occ = C.occupancy(run)
    assert occ.max_in_flight == 1


def test_coincidence_is_zero_for_a_closed_loop():
    assert C.coincidence(closed_loop_run(workers=3, cycles=30)) == pytest.approx(0.0)


def test_coincidence_is_order_one_for_an_open_loop():
    s = C.coincidence(open_loop_run(n=400, rate=5.0))
    assert 0.5 < s < 2.0


def test_coincidence_stays_in_the_open_band_when_the_deployment_gets_faster():
    """A ten-fold change in service time must not change the verdict.

    The statistic is not perfectly invariant -- at concurrency well below one, a
    completion trails its own send closely and the ratio drifts upward -- so what
    is asserted is the property that matters: both runs stay open-like, and the
    drift is far smaller than the width of the band.
    """
    slow = C.coincidence(open_loop_run(n=400, rate=5.0, latency=1.0))
    fast = C.coincidence(open_loop_run(n=400, rate=5.0, latency=0.1))
    assert slow > C.OPEN_MIN_SCORE and fast > C.OPEN_MIN_SCORE
    assert abs(slow - fast) < 0.5


def test_coincidence_does_not_move_when_the_concurrency_changes():
    low = C.coincidence(open_loop_run(n=400, rate=2.0, latency=1.0))
    high = C.coincidence(open_loop_run(n=400, rate=20.0, latency=1.0))
    assert low > C.OPEN_MIN_SCORE and high > C.OPEN_MIN_SCORE
    assert abs(low - high) < 0.35


def test_the_index_lag_form_would_have_failed_on_the_closed_fixture(closed_run):
    """The first version of this detector, kept as a test rather than as code.

    `start[i] - end[i-N]` scanned over N scores 0.211 on a file that is a closed
    loop by construction -- inside the inconclusive band. With variable service
    times the i-th send is not the same worker's (i-N)-th, so the index lag
    decorrelates. The replacement has no index arithmetic in it and scores 0.
    """
    reqs = [r for r in closed_run.ok_requests()]
    n = len(reqs)
    service = sorted(r.end_s - r.start_s for r in reqs)[n // 2]
    best = None
    for w in range(1, 32):
        gaps = sorted(abs(reqs[i].start_s - reqs[i - w].end_s) for i in range(w, n))
        score = gaps[len(gaps) // 2] / service
        best = score if best is None else min(best, score)
    assert best > C.CLOSED_MAX_SCORE, "the old detector misses this closed loop"
    assert C.coincidence(closed_run) == pytest.approx(0.0, abs=1e-9)


def test_the_two_detectors_can_disagree():
    """A closed loop with client-side think time: capped, but not coincident.

    Occupancy still says closed (the worker pool bounds in-flight). Coincidence
    says open (sends land a think-time after a completion, not on it). The tool
    must return inconclusive rather than pick one -- if the detectors could not
    disagree they would not be two detectors.
    """
    workers, think = 4, 0.12
    free = [0.0] * workers
    reqs = []
    for i in range(200):
        w = min(range(workers), key=lambda j: free[j])
        start = free[w] + think
        lat = 1.0 + 0.4 * ((i * 7919) % 100) / 100.0
        reqs.append(req(i, start, lat))
        free[w] = start + lat
    reqs.sort(key=lambda r: r.start_s)
    run = run_of(reqs)
    occ = C.occupancy(run)
    assert occ.max_in_flight == workers
    assert C.coincidence(run) > C.CLOSED_MAX_SCORE
    v = C.classify(run)
    assert v.kind == C.INCONCLUSIVE
    assert "do not agree" in v.reason


def test_too_few_requests_is_unknown_not_a_guess():
    run = run_of([req(i, float(i), 1.0) for i in range(5)])
    assert C.classify(run).kind == C.UNKNOWN


# -------------------------------------------------------------- arrival processes
def test_poisson_arrivals_are_recognised():
    cv, kind, src = C.arrival_process(open_loop_run(n=800, rate=5.0))
    assert kind == "poisson_like"
    assert 0.7 <= cv <= 1.4
    assert src == "arrival"


def test_a_fixed_rate_schedule_is_recognised_as_deterministic():
    reqs = [req(i, i * 0.25, 0.5, arrival=i * 0.25) for i in range(200)]
    cv, kind, _src = C.arrival_process(run_of(reqs))
    assert kind == "deterministic"
    assert cv < C.DETERMINISTIC_MAX_CV


def test_bursty_arrivals_are_not_called_poisson():
    reqs = []
    t = 0.0
    for i in range(300):
        t += 0.001 if i % 10 else 5.0        # bunches of ten
        reqs.append(req(i, t, 0.2, arrival=t))
    cv, kind, _src = C.arrival_process(run_of(reqs))
    assert kind == "bursty"
    assert cv > C.POISSON_CV_HI


def test_without_a_schedule_column_the_send_times_are_used_and_said_so():
    run = closed_loop_run(workers=3, cycles=20)
    _cv, _kind, src = C.arrival_process(run)
    assert src == "send"
