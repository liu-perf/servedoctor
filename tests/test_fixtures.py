"""The fixtures are test data, so they are tested too.

Three properties: they say what they are inside the data, they can be regenerated
byte-for-byte from their generator, and each one actually exhibits the phenomenon
it was built for. The last is the one that rots -- a fixture whose behaviour has
drifted still parses, still passes every other test, and quietly stops being
evidence for the thing it is cited as evidence for.
"""
import hashlib
import os
import runpy
import shutil

import pytest
from conftest import FIXTURES, fx

from servedoctor import closure as C
from servedoctor import parse
from servedoctor import percentile as P

CSVS = sorted(f for f in os.listdir(FIXTURES) if f.startswith("EXAMPLE_")
              and f.endswith(".csv"))


def test_there_are_the_expected_fixtures():
    assert len(CSVS) == 11
    assert os.path.exists(fx("EXAMPLE_summary.json"))


@pytest.mark.parametrize("name", CSVS)
def test_every_csv_declares_itself_in_its_own_first_line(name):
    """The declaration lives in the data, not the filename.

    A filename does not survive being pasted into a slide, an issue comment or a
    spreadsheet. The first line does.
    """
    with open(fx(name), encoding="utf-8") as fh:
        first = fh.readline()
    assert first.startswith("# EXAMPLE")
    assert "synthetic" in first


def test_the_summary_json_declares_itself_too():
    run = parse.load(fx("EXAMPLE_summary.json"))
    assert "_comment" in run.summary.unknown_fields


@pytest.mark.parametrize("name", CSVS)
def test_every_csv_parses_into_requests(name):
    run = parse.load(fx(name))
    assert run.n > 0
    assert run.evidence == parse.PER_REQUEST


def test_the_generator_reproduces_every_fixture_byte_for_byte(tmp_path):
    before = {}
    for name in os.listdir(FIXTURES):
        if name.startswith("EXAMPLE_"):
            with open(fx(name), "rb") as fh:
                before[name] = hashlib.sha256(fh.read()).hexdigest()
    backup = os.path.join(str(tmp_path), "backup")
    shutil.copytree(FIXTURES, backup)
    try:
        runpy.run_path(fx("make_fixtures.py"), run_name="__main__")
        for name, digest in before.items():
            with open(fx(name), "rb") as fh:
                assert hashlib.sha256(fh.read()).hexdigest() == digest, name
    finally:
        for name in before:
            shutil.copy(os.path.join(backup, name), fx(name))


# ------------------------------------------------- each fixture does its one job
def test_the_pair_shares_a_server_and_differs_only_in_the_harness(closed_run,
                                                                  open_run):
    assert C.classify(closed_run).kind == C.CLOSED_LOOP
    assert C.classify(open_run).kind == C.OPEN_LOOP
    assert closed_run.n == open_run.n == 2000


def test_the_closed_loop_delivered_less_than_it_was_configured_for(closed_run):
    rate = closed_run.n / closed_run.span_s()
    assert rate == pytest.approx(5.84, abs=0.05)
    assert rate / 6.4 < 0.95, "configured for 6.4 req/s, delivered 91% of it"


def test_the_open_loop_delivered_what_it_was_configured_for(open_run):
    rate = open_run.n / open_run.span_s()
    assert rate == pytest.approx(6.4, rel=0.05)


def test_the_tails_differ_by_the_documented_factor(closed_run, open_run):
    qc = P.quantile(closed_run.values(lambda r: r.latency_s()), 0.99)
    qo = P.quantile(open_run.values(lambda r: r.latency_s()), 0.99)
    assert qc == pytest.approx(2.859, abs=0.01)
    assert qo == pytest.approx(13.482, abs=0.01)
    assert qo / qc == pytest.approx(4.72, abs=0.05)


def test_but_the_two_maxima_are_close(closed_run, open_run):
    """The closed loop does record the stall -- it just never reaches a percentile.

    This is the sharper statement of coordinated omission, and it is the reason a
    large gap between the maximum and the P99 is itself a warning sign.
    """
    mc = max(closed_run.values(lambda r: r.latency_s()))
    mo = max(open_run.values(lambda r: r.latency_s()))
    assert mc == pytest.approx(12.50, abs=0.05)
    assert mo == pytest.approx(14.70, abs=0.05)
    assert 1.0 < mo / mc < 1.25


def test_the_closed_loop_hides_its_own_stall_behind_its_p99(closed_run):
    lats = closed_run.values(lambda r: r.latency_s())
    assert max(lats) / P.quantile(lats, 0.99) > 4.0


def test_the_short_fixture_is_exactly_below_the_threshold(short_run):
    assert short_run.n == 200
    assert P.min_n_for(0.99, 0.90) == 299
    assert P.estimate(short_run.values(lambda r: r.latency_s()), 0.99).bounded is False


def test_the_censored_fixture_drops_exactly_the_slowest(censored_run):
    dropped = [r for r in censored_run.requests if not r.ok]
    kept = [r.latency_s() for r in censored_run.requests
            if r.ok and r.latency_s() is not None]
    assert len(dropped) == 12
    assert all(r.end_s is None for r in dropped)
    assert len(kept) == 588
    # they were chosen as the 12 slowest, so 2% is exactly the top of the tail
    assert 12 / 600.0 == pytest.approx(0.02)


def test_the_short_answer_fixture_has_short_answers(short_answers_run):
    outs = [r.output_tokens for r in short_answers_run.requests]
    assert P.median(outs) == 4
    assert any(m < 2 for m in outs), "and some with no TPOT at all"


def test_the_cold_start_fixture_is_short_enough_for_one_sample_to_matter(
        cold_start_run):
    assert cold_start_run.n == 60
    assert P.rank_for(60, 0.99) == 60
    lats = cold_start_run.values(lambda r: r.latency_s())
    assert lats[0] == max(lats), "the first request is the slowest"


def test_the_backlog_fixture_actually_fell_behind(backlog_run):
    waits = backlog_run.values(lambda r: r.queue_wait_s())
    assert max(waits) > 20.0
    assert sum(1 for w in waits if w > 1e-3) / len(waits) > 0.8


def test_the_ladder_rungs_are_ordered_and_distinct():
    rates = []
    for mult in (40, 70, 95, 114):
        run = parse.load(fx("EXAMPLE_ladder_{}.csv".format(mult)))
        rates.append(run.n / run.span_s())
    assert rates == sorted(rates)
    assert rates[-1] / rates[0] > 2.0


def test_the_summary_is_the_open_loop_run_reduced(summary_run, open_run):
    assert summary_run.summary.get("completed") == open_run.n
    assert summary_run.summary.get("duration_s") == pytest.approx(
        open_run.span_s(), rel=1e-6)


def test_the_summary_carries_a_concurrency_that_does_not_fit_its_own_numbers(
        summary_run):
    """Deliberate, and documented in the file: a value carried over from the pair.

    Little's Law is the only thing in a summary report that can catch it.
    """
    s = summary_run.summary
    implied = s.get("request_throughput") * s.get("mean_e2el_ms") / 1000.0
    assert s.get("max_concurrency") == 7
    assert implied > 25
