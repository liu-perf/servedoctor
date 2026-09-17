"""Shared helpers: fixture paths, and synthetic runs built request by request.

Two kinds of input to the tests. The files under `fixtures/` exercise the whole
library against something with realistic shape; the builders here construct runs
with exactly one property, so that when a test fails it names one thing.
"""
import os

import pytest

from servedoctor import parse

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fx(name):
    return os.path.join(FIXTURES, name)


def req(rid, start, latency, ttft=0.1, tokens_in=100, tokens_out=50, ok=True,
        arrival=None):
    """One request described by its durations rather than its timestamps."""
    return parse.Request(
        rid=str(rid), arrival_s=arrival, start_s=start,
        first_token_s=None if ttft is None else start + ttft,
        end_s=None if latency is None else start + latency,
        input_tokens=tokens_in, output_tokens=tokens_out, ok=ok)


def run_of(requests, **kw):
    return parse.Run(requests=requests, source="synthetic", **kw)


def closed_loop_run(workers=4, cycles=30, latency=1.0):
    """Every send lands exactly on a completion: the defining property."""
    free = [0.0] * workers
    reqs = []
    for i in range(workers * cycles):
        w = min(range(workers), key=lambda j: free[j])
        start = free[w]
        # vary the service time so the run is not degenerate, but keep the
        # send-on-completion property exact
        lat = latency * (0.7 + 0.6 * ((i * 7919) % 100) / 100.0)
        reqs.append(req(i, start, lat))
        free[w] = start + lat
    reqs.sort(key=lambda r: r.start_s)
    return run_of(reqs)


def open_loop_run(n=200, rate=4.0, latency=1.0, jitter=0.6, seed=1):
    """Sends on a schedule that ignores completions.

    `seed` exists because two calls with the same arguments return byte-identical
    data, which is right for most tests and useless for the one that needs two
    independent draws from the same distribution.
    """
    reqs = []
    t = 0.0
    x = (seed * 2654435761 + 12345) & 0x7FFFFFFF
    for i in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        u = (x % 10000 + 1) / 10001.0
        t += -(1.0 / rate) * _log(u)
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        lat = latency * (1.0 - jitter / 2 + jitter * (x % 1000) / 1000.0)
        reqs.append(req(i, t, lat, arrival=t))
    return run_of(reqs)


def _log(x):
    import math
    return math.log(x)


@pytest.fixture(scope="session")
def closed_run():
    return parse.load(fx("EXAMPLE_closed_loop.csv"))


@pytest.fixture(scope="session")
def open_run():
    return parse.load(fx("EXAMPLE_open_loop.csv"))


@pytest.fixture(scope="session")
def backlog_run():
    return parse.load(fx("EXAMPLE_open_loop_backlog.csv"))


@pytest.fixture(scope="session")
def short_run():
    return parse.load(fx("EXAMPLE_short_200.csv"))


@pytest.fixture(scope="session")
def censored_run():
    return parse.load(fx("EXAMPLE_censored.csv"))


@pytest.fixture(scope="session")
def short_answers_run():
    return parse.load(fx("EXAMPLE_short_answers.csv"))


@pytest.fixture(scope="session")
def cold_start_run():
    return parse.load(fx("EXAMPLE_cold_start.csv"))


@pytest.fixture(scope="session")
def summary_run():
    return parse.load(fx("EXAMPLE_summary.json"))
