"""Which part of the run is the measurement, and which requests are missing from it.

Three separate ways a load test measures something other than steady-state service,
all of which move the tail in the flattering direction:

**Warm-up.** The first request through a fresh deployment pays for weight loading,
lazy CUDA context creation, a compile or an autotune, and an empty prefix cache.
It is a real number about a real event and it belongs in a cold-start report, not
in the same distribution as request 4000. Left in, it is one sample -- which is
exactly enough to *be* the P99 of a 200-request run.

**Ramp and drain.** A run has a beginning where concurrency is climbing and an end
where it is falling. Requests served during those stretches met a less loaded
server. Averaged in, they pull every statistic toward the optimistic side, and the
shorter the run the larger the share.

**Censoring.** Requests that failed, timed out, or were still running when the
harness stopped are usually dropped before the statistics are computed. They are
not a random subset: an unfinished request is, by definition, slower than every
finished one. Drop a fraction f of the sample from the top and the reported
q-quantile is really the q(1-f)-quantile -- and when q > 1-f, the quantile asked
for lies entirely inside the discarded part and **cannot be recovered at all**.
A 2% timeout rate does not perturb the P99. It deletes it.

That last one has the same shape as this series' recurring finding: the number is
not slightly wrong, it is a statement about a different population, and the honest
output is a refusal rather than a corrected figure.
"""
from . import closure as _cl
from . import percentile as _p

# A leading request this many times the median is a cold start, not a sample of
# steady-state service. tracedoctor hit the same phenomenon from the other side:
# one 80.45 ms JIT first call dragged a mean-based ratio to a 73x false positive,
# and the fix there was the median. Here the fix is to name the request and let the
# reader decide, because on a cold-start report it is the number that matters.
WARMUP_FACTOR = 3.0
# At most this share of a run may be called warm-up. Beyond it the run is not a
# steady-state measurement with a warm-up attached; it is a transient.
MAX_WARMUP_FRACTION = 0.10
# Occupancy at or above this share of the run's own peak counts as loaded.
STEADY_OCCUPANCY = 0.80


class Warmup:
    __slots__ = ("count", "factor", "first_s", "median_s", "p99_with", "p99_without",
                 "shift", "note")

    def __init__(self, count, factor, first_s, median_s, p99_with, p99_without,
                 shift, note):
        self.count = count
        self.factor = factor
        self.first_s = first_s
        self.median_s = median_s
        self.p99_with = p99_with
        self.p99_without = p99_without
        self.shift = shift
        self.note = note

    def as_dict(self):
        return {"count": self.count, "factor": self.factor, "first_s": self.first_s,
                "median_s": self.median_s, "p99_with": self.p99_with,
                "p99_without": self.p99_without, "shift": self.shift,
                "note": self.note}


def warmup(run, q=0.99):
    """Leading requests that are slower than steady state by more than a factor."""
    reqs = [r for r in run.ok_requests() if r.latency_s() is not None]
    if len(reqs) < 10:
        return None
    reqs = sorted(reqs, key=lambda r: r.start_s)
    lats = [r.latency_s() for r in reqs]
    med = _p.median(lats)
    if not med:
        return None
    limit = max(1, int(len(reqs) * MAX_WARMUP_FRACTION))
    count = 0
    while count < limit and lats[count] > WARMUP_FACTOR * med:
        count += 1
    with_all = _p.quantile(lats, q)
    without = _p.quantile(lats[count:], q) if count else with_all
    shift = None
    if count and with_all and without:
        shift = with_all / without - 1.0
    note = ("the first {} request(s) run {:.1f}x the median; dropping them moves q{:g} "
            "by {:+.1%}".format(count, lats[0] / med, q * 100, shift)
            if count else
            "no leading request exceeds {:.0f}x the median -- either the deployment "
            "was already warm or the harness discarded the warm-up before writing "
            "this file, and the file cannot say which".format(WARMUP_FACTOR))
    return Warmup(count, lats[0] / med, lats[0], med, with_all, without, shift, note)


class Steady:
    __slots__ = ("t0", "t1", "kept", "dropped_head", "dropped_tail", "fraction",
                 "note")

    def __init__(self, t0, t1, kept, dropped_head, dropped_tail, fraction, note):
        self.t0 = t0
        self.t1 = t1
        self.kept = kept
        self.dropped_head = dropped_head
        self.dropped_tail = dropped_tail
        self.fraction = fraction
        self.note = note

    def as_dict(self):
        return {"t0": self.t0, "t1": self.t1, "kept": self.kept,
                "dropped_head": self.dropped_head, "dropped_tail": self.dropped_tail,
                "fraction": self.fraction, "note": self.note}


def steady(run):
    """The stretch of wall time during which the server was as loaded as it usually was.

    Referenced to the *median* in-flight count, not the maximum. The maximum is a
    queue spike -- on the open-loop fixture here it is 86 against a median of 27 --
    so a window defined against it selects the twenty seconds after a stall and
    calls the other three hundred "ramp-up". That was the first version, and it is
    a good example of the class of bug this whole series is about: the arithmetic
    was right, the reference point was a different quantity than the name implied.
    """
    occ = _cl.occupancy(run)
    reqs = [r for r in run.ok_requests()
            if r.start_s is not None and r.end_s is not None]
    if occ is None or occ.median_in_flight < 2 or len(reqs) < 10:
        return None
    floor = STEADY_OCCUPANCY * occ.median_in_flight
    events = []
    for r in reqs:
        events.append((r.start_s, 1))
        events.append((r.end_s, -1))
    events.sort(key=lambda e: (e[0], e[1]))
    cur, t0, t1 = 0, None, None
    for t, delta in events:
        cur += delta
        if cur >= floor:
            if t0 is None:
                t0 = t
            t1 = t
    if t0 is None or t1 is None or t1 <= t0:
        return None
    head = sum(1 for r in reqs if r.start_s < t0)
    tail = sum(1 for r in reqs if r.start_s > t1)
    kept = len(reqs) - head - tail
    frac = kept / float(len(reqs))
    return Steady(t0, t1, kept, head, tail, frac,
                  "in-flight count is at or above {:.0%} of its median ({} of a peak "
                  "{}) between {:.3f}s and {:.3f}s; {} of {} requests were sent inside "
                  "that window ({} during ramp-up, {} during drain)"
                  .format(STEADY_OCCUPANCY, occ.median_in_flight, occ.max_in_flight,
                          t0, t1, kept, len(reqs), head, tail))


class Censoring:
    __slots__ = ("n_total", "n_dropped", "fraction", "q", "recoverable",
                 "effective_q", "observed", "corrected", "note")

    def __init__(self, n_total, n_dropped, fraction, q, recoverable, effective_q,
                 observed, corrected, note):
        self.n_total = n_total
        self.n_dropped = n_dropped
        self.fraction = fraction
        self.q = q
        self.recoverable = recoverable
        self.effective_q = effective_q
        self.observed = observed
        self.corrected = corrected
        self.note = note

    def as_dict(self):
        return {"n_total": self.n_total, "n_dropped": self.n_dropped,
                "fraction": self.fraction, "q": self.q,
                "recoverable": self.recoverable, "effective_q": self.effective_q,
                "observed": self.observed, "corrected": self.corrected,
                "note": self.note}


def censoring(run, q=0.99):
    """What dropping the failed and unfinished requests did to the reported quantile.

    Assumes only that a censored request would have been slower than every request
    that finished. That is not a modelling choice: it is what "did not finish"
    means. Everything below follows from it and from counting.
    """
    total = len(run.requests)
    if total == 0:
        return None
    kept = [r for r in run.requests if r.ok and r.end_s is not None]
    dropped = total - len(kept)
    frac = dropped / float(total)
    lats = [r.latency_s() for r in kept if r.latency_s() is not None]
    observed = _p.quantile(lats, q) if lats else None
    if dropped == 0:
        return Censoring(total, 0, 0.0, q, True, q, observed, observed,
                         "every request finished, so the reported quantile is over "
                         "the whole population")
    if q > 1.0 - frac:
        return Censoring(
            total, dropped, frac, q, False, None, observed, None,
            "{} of {} requests ({:.2%}) did not finish and were dropped. Since an "
            "unfinished request is slower than every finished one, the top {:.2%} of "
            "the true distribution is exactly the part that was discarded -- and q{:g} "
            "lies inside it. The observed q{:g} of {:.4f}s is not an estimate of the "
            "true one; it is the q{:g} of the survivors."
            .format(dropped, total, frac, frac, q * 100, q * 100,
                    observed if observed else float("nan"), q * 100))
    eff = q / (1.0 - frac)
    corrected = _p.quantile(lats, eff) if lats else None
    return Censoring(
        total, dropped, frac, q, True, eff, observed, corrected,
        "{} of {} requests ({:.2%}) were dropped; they are the slow end by "
        "construction, so the population q{:g} is the survivors' q{:.4g} = {:.4f}s, "
        "not their q{:g} = {:.4f}s".format(
            dropped, total, frac, q * 100, eff * 100,
            corrected if corrected else float("nan"), q * 100,
            observed if observed else float("nan")))
