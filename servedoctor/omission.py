"""Coordinated omission: the requests a benchmark did not send, and what they cost.

The name is Gil Tene's and the mechanism is simple enough to state in one sentence:
a load generator that waits for a response before sending the next request stops
sending exactly when the system is slowest, so the samples that would have carried
the tail are missing from the sample, and the tail is reported as short.

There are two ways a harness can be honest about this and this module handles both.

**It wrote down its schedule.** Then every request has a time it was *due* and a
time it was *sent*, and the difference is the harness's own backlog. Latency
measured from the due time is what a user would have experienced; latency measured
from the send time is what the harness felt. Both are in the file, the correction
is exact, and the ratio between them is the size of the problem.

**It did not.** Then the correction is *not available*, and this module says so
rather than estimating one. It can still put a floor under the damage: if the run
was supposed to offer R requests/second for T seconds and only n went out, then
`R*T - n` requests are missing, and they are not missing at random -- every one of
them was skipped during a stall. That is a bound, and it is reported as a bound.

The refusal matters more than the correction. A closed-loop log has no schedule to
correct against, and a "corrected" number invented from one is worse than the
uncorrected number, because it looks like it has already been dealt with.
"""
from . import percentile as _p

# Above this, latency-from-due and latency-from-send are telling different stories
# and only one of them is about the server.
FACTOR_WARN = 1.10
FACTOR_VIOLATION = 1.50


class Omission:
    __slots__ = ("available", "reason", "q", "raw", "corrected", "factor",
                 "max_wait_s", "median_wait_s", "late_fraction", "n")

    def __init__(self, available, reason, q=None, raw=None, corrected=None,
                 factor=None, max_wait_s=None, median_wait_s=None,
                 late_fraction=None, n=0):
        self.available = available
        self.reason = reason
        self.q = q
        self.raw = raw
        self.corrected = corrected
        self.factor = factor
        self.max_wait_s = max_wait_s
        self.median_wait_s = median_wait_s
        self.late_fraction = late_fraction
        self.n = n

    def as_dict(self):
        return {"available": self.available, "reason": self.reason, "q": self.q,
                "raw": self.raw.value if self.raw else None,
                "corrected": self.corrected.value if self.corrected else None,
                "factor": self.factor, "max_wait_s": self.max_wait_s,
                "median_wait_s": self.median_wait_s,
                "late_fraction": self.late_fraction, "n": self.n}


# The threshold for "the harness was late on this request at all". A schedule kept
# to within a millisecond is a schedule kept; anything at or above this is backlog.
LATE_S = 1e-3


def analyse(run, q=0.99, conf=_p.DEFAULT_CONF):
    """Compare latency-from-due against latency-from-send at quantile `q`."""
    if run.evidence != "per_request":
        return Omission(False, "summary report: it carries one latency per statistic "
                               "and no send times, so there is nothing to correct "
                               "against and no way to tell whether it needs it")
    waits = run.values(lambda r: r.queue_wait_s())
    if not waits:
        return Omission(
            False,
            "no schedule column: the log records when each request was sent but not "
            "when it was due, so a request delayed by the harness is indistinguishable "
            "from a request delayed by the server. Re-run with the intended send time "
            "recorded, or read the harness to find out whether it has one.")
    raw = _p.estimate(run.values(lambda r: r.latency_s()), q, conf)
    corrected = _p.estimate(run.values(lambda r: r.corrected_latency_s()), q, conf)
    factor = None
    if raw.value and corrected.value:
        factor = corrected.value / raw.value
    late = sum(1 for w in waits if w >= LATE_S)
    return Omission(True, "schedule present; correction is exact", q, raw, corrected,
                    factor, max(waits), _p.median(waits), late / len(waits), len(waits))


def deficit(run, target_rate, span_s=None):
    """How many requests a schedule at `target_rate` would have contained.

    Returns `(expected, actual, missing)`. This is the floor, not the damage: the
    missing requests are the ones a stall swallowed, so their latencies would all
    have landed in the tail -- but how far into it is not knowable from a log that
    does not contain them.
    """
    span = span_s if span_s is not None else run.span_s()
    if not span or not target_rate:
        return None
    expected = target_rate * span
    actual = run.n
    return expected, actual, max(0.0, expected - actual)


def status(factor):
    if factor is None:
        return "unknown"
    if factor >= FACTOR_VIOLATION:
        return "violation"
    if factor >= FACTOR_WARN:
        return "warn"
    return "ok"
