"""Was this a closed-loop run or an open-loop one, and how do I know from the log?

This is the first question, and it is asked before any latency number is read,
because the answer decides whether the latency numbers describe the server at all.

A **closed-loop** harness holds N workers; each sends a request, waits for the
answer, and sends the next. When the server slows down, the harness slows down with
it. The offered load is therefore a function of the server's own performance, and
the tail of the latency distribution is truncated by construction -- the requests
that would have piled up during a stall were never sent. This is Gil Tene's
coordinated omission, and it is not a subtle bias: on the fixture in this repo it
moves the P99 by a factor this library prints for you.

An **open-loop** harness sends on a schedule that ignores the server. When the
server stalls, requests keep arriving and queue, and the measured tail contains
that queueing. It is the harder run to set up and the only one whose tail means
what people read it as meaning.

Two independent detectors, kept independent on purpose:

  A. **Occupancy.** Under a closed loop the number of requests in flight is capped
     at N and sits *at* N nearly all the time. Sweep the start/end events and
     measure the fraction of wall time spent at the maximum. This also names N.
  B. **Coincidence.** Under a closed loop every send happens at the instant some
     earlier request completed, so the distance from each send back to the nearest
     preceding completion is zero. Normalise that distance by the median gap
     between sends and the statistic is scale-free: a closed loop scores ~0, and an
     open loop scores ~1, because completions occur at the same average rate as
     sends, so a send lands about half an inter-completion gap after one.

The first attempt at B was `start[i] - end[i-N]` scanned over candidate N, which
looks equivalent and is not: with variable service times the i-th send is not the
same worker's (i-N)-th, the index lag decorrelates, and on the closed-loop fixture
in this repo it scored 0.211 -- inside the inconclusive band, on a file that is a
closed loop by construction. The nearest-preceding-completion form has no index
arithmetic in it and scores 0.000 on the same file.

A never looks at which request is which, only at how many are open; B never looks
at how many are open, only at the ordering of two event streams. Neither can be
rearranged into the other -- which is the property telemetrydoctor's cross-check
lacked in its first version, where the second "independent" method turned out to be
the first one restated as algebra. `test_the_two_detectors_can_disagree` holds a
run where they do.
"""
from . import percentile as _p

CLOSED_LOOP = "closed_loop"
OPEN_LOOP = "open_loop"
INCONCLUSIVE = "inconclusive"
UNKNOWN = "unknown"

# Alignment score = median |start[i] - end[i-N]| / median service time. A closed
# loop scores the harness's own dispatch overhead divided by a request, which on
# real harnesses is well under a percent. An open loop has no reason to align with
# any N and scores order-one. The band between is left as inconclusive rather than
# split down the middle, the same way nodebench leaves 0.30-0.55 unjudged on its
# P2P ratio: a detector that always answers is a detector that cannot be trusted
# when it answers.
CLOSED_MAX_SCORE = 0.05
OPEN_MIN_SCORE = 0.25
# Below this fraction of wall time spent at maximum occupancy, the run is not
# worker-capped whatever the alignment says.
CLOSED_MIN_AT_MAX = 0.60
# Candidate worker counts need enough repetitions to mean anything.
MIN_CYCLES = 4

# Inter-arrival CV bands. Poisson is exactly 1.0; a fixed-rate generator is 0.
DETERMINISTIC_MAX_CV = 0.30
POISSON_CV_LO, POISSON_CV_HI = 0.70, 1.40


class Occupancy:
    __slots__ = ("max_in_flight", "at_max_fraction", "mean_in_flight",
                 "median_in_flight", "span_s")

    def __init__(self, max_in_flight, at_max_fraction, mean_in_flight,
                 median_in_flight, span_s):
        self.max_in_flight = max_in_flight
        self.at_max_fraction = at_max_fraction
        self.mean_in_flight = mean_in_flight
        # Time-weighted median, which is the level the run actually ran at. The mean
        # is pulled up by the queue spikes a stall produces, and the maximum IS a
        # queue spike -- so anything that wants "how loaded was this normally" has to
        # use this one. Using the maximum instead is what made the first version of
        # window.steady() report a 20-second steady window inside a 318-second run.
        self.median_in_flight = median_in_flight
        self.span_s = span_s


def occupancy(run):
    """Time-weighted distribution of requests in flight, by event sweep."""
    events = []
    for r in run.ok_requests():
        if r.start_s is None or r.end_s is None:
            continue
        events.append((r.start_s, 1))
        events.append((r.end_s, -1))
    if not events:
        return None
    # Ends before starts at an identical timestamp: a worker that frees and
    # re-sends in the same microsecond must not read as two in flight.
    events.sort(key=lambda e: (e[0], e[1]))
    cur = 0
    prev_t = events[0][0]
    time_at = {}
    peak = 0
    area = 0.0
    for t, delta in events:
        dt = t - prev_t
        if dt > 0:
            time_at[cur] = time_at.get(cur, 0.0) + dt
            area += cur * dt
        cur += delta
        peak = max(peak, cur)
        prev_t = t
    span = sum(time_at.values())
    if span <= 0:
        return Occupancy(peak, 0.0, 0.0, 0, 0.0)
    acc, med = 0.0, peak
    for level in sorted(time_at):
        acc += time_at[level]
        if acc >= span / 2.0:
            med = level
            break
    return Occupancy(peak, time_at.get(peak, 0.0) / span, area / span, med, span)


def coincidence(run):
    """Median distance from a send back to the nearest earlier completion.

    Normalised by the median inter-send gap, so the statistic does not move when
    the deployment gets faster or the concurrency changes. Closed loop -> 0. Open
    loop -> about 1, and that value is not a calibration constant: for two point
    processes of equal rate, a point of one lands a median of ln(2)/rate after a
    point of the other, and the median inter-send gap is also ln(2)/rate.
    """
    reqs = [r for r in run.ok_requests()
            if r.start_s is not None and r.end_s is not None]
    if len(reqs) < MIN_CYCLES * 2:
        return None
    sends = sorted(r.start_s for r in reqs)
    ends = sorted(r.end_s for r in reqs)
    gaps = [b - a for a, b in zip(sends, sends[1:])]
    unit = _p.median(gaps)
    if not unit:
        return None
    dists = []
    j = 0
    for s in sends:
        # advance j to the last completion at or before this send
        while j < len(ends) and ends[j] <= s + 1e-12:
            j += 1
        if j == 0:
            continue                     # nothing has finished yet: the first batch
        dists.append(max(0.0, s - ends[j - 1]))
    if not dists:
        return None
    return _p.median(dists) / unit


def arrival_process(run):
    """(cv, label) of the inter-arrival times the harness intended."""
    times = [r.arrival_s for r in run.requests if r.arrival_s is not None]
    used = "arrival"
    if len(times) < 3:
        times = [r.start_s for r in run.requests if r.start_s is not None]
        used = "send"
    if len(times) < 3:
        return None, UNKNOWN, used
    times.sort()
    gaps = [b - a for a, b in zip(times, times[1:])]
    cv = _p.cv_pct(gaps)
    if cv is None:
        return None, UNKNOWN, used
    cv /= 100.0
    if cv <= DETERMINISTIC_MAX_CV:
        return cv, "deterministic", used
    if POISSON_CV_LO <= cv <= POISSON_CV_HI:
        return cv, "poisson_like", used
    if cv > POISSON_CV_HI:
        return cv, "bursty", used
    return cv, "sub_poisson", used


class Verdict:
    __slots__ = ("kind", "workers", "score", "occ", "arrival_cv", "arrival_kind",
                 "arrival_source", "agree", "reason")

    def __init__(self, kind, workers, score, occ, arrival_cv, arrival_kind,
                 arrival_source, agree, reason):
        self.kind = kind
        self.workers = workers
        self.score = score
        self.occ = occ
        self.arrival_cv = arrival_cv
        self.arrival_kind = arrival_kind
        self.arrival_source = arrival_source
        self.agree = agree
        self.reason = reason

    def as_dict(self):
        return {"kind": self.kind, "workers": self.workers, "score": self.score,
                "max_in_flight": self.occ.max_in_flight if self.occ else None,
                "at_max_fraction": self.occ.at_max_fraction if self.occ else None,
                "mean_in_flight": self.occ.mean_in_flight if self.occ else None,
                "arrival_cv": self.arrival_cv, "arrival_kind": self.arrival_kind,
                "detectors_agree": self.agree, "reason": self.reason}


def classify(run):
    if run.evidence != "per_request":
        return Verdict(UNKNOWN, None, None, None, None, UNKNOWN, None, None,
                       "summary report: it records what the harness measured, not "
                       "when it sent anything, so how it offered load is not in the "
                       "file. The single most consequential property of a serving "
                       "benchmark is the one summaries never carry.")
    occ = occupancy(run)
    score = coincidence(run)
    cv, kind, src = arrival_process(run)
    if occ is None or score is None:
        return Verdict(UNKNOWN, None, score, occ, cv, kind, src, None,
                       "not enough complete request records to decide")

    a_closed = occ.at_max_fraction >= CLOSED_MIN_AT_MAX
    b_closed = score <= CLOSED_MAX_SCORE
    b_open = score >= OPEN_MIN_SCORE
    agree = (a_closed and b_closed) or (not a_closed and b_open)

    if a_closed and b_closed:
        return Verdict(
            CLOSED_LOOP, occ.max_in_flight, score, occ, cv, kind, src, True,
            "occupancy sits at {} for {:.1%} of the run, and every send lands a "
            "median of {:.3f} inter-send gaps after a completion -- that is a worker "
            "pool of {}, not a schedule. Offered load was therefore a function of the "
            "server's own speed: when it slowed down, this harness sent less. Any "
            "percentile below is a statement about the pair, and the server alone is "
            "not recoverable from it."
            .format(occ.max_in_flight, occ.at_max_fraction, score,
                    occ.max_in_flight))
    if not a_closed and b_open:
        return Verdict(
            OPEN_LOOP, None, score, occ, cv, kind, src, True,
            "occupancy is at its maximum of {} only {:.1%} of the time and sends fall "
            "a median of {:.3f} inter-send gaps after a completion, which is where "
            "independent streams fall. Mean in flight {:.2f}. Arrivals look {} "
            "(inter-arrival CV {:.2f}, from the {} column)."
            .format(occ.max_in_flight, occ.at_max_fraction, score,
                    occ.mean_in_flight, kind,
                    cv if cv is not None else float("nan"), src))
    return Verdict(
        INCONCLUSIVE, occ.max_in_flight, score, occ, cv, kind, src, agree,
        "the two detectors do not agree: occupancy sits at its maximum of {} for "
        "{:.1%} of the run ({}), while sends fall a median of {:.3f} inter-send gaps "
        "after a completion ({}). This tool does not guess in between -- a "
        "rate-limited harness that hit a concurrency cap really is a closed loop for "
        "part of the run, and a closed loop with client-side think time really is "
        "neither. Read the harness, or record the intended send times and let SD003 "
        "answer it arithmetically."
        .format(occ.max_in_flight, occ.at_max_fraction,
                "closed-like" if a_closed else "open-like", score,
                "closed-like" if b_closed else
                ("open-like" if b_open else "in between")))
