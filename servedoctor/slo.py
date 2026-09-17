"""Goodput: throughput counted only over the requests that were actually useful.

Throughput past the saturation point keeps rising while every request misses its
deadline. That is not a paradox, it is what a queue does: admit more, finish more,
finish all of them late. A benchmark that reports throughput alone therefore has a
maximum in the wrong place, and "the best configuration" read off it is the one
where the service is least usable.

Goodput fixes this by refusing to count a request that missed its SLO. The
definition is deliberately per-request and conjunctive -- a request counts only if
it met *every* deadline it was given -- because that is how a user experiences it.
An answer whose first token arrived on time and whose remaining tokens crawled is
not four-fifths useful.

Two more things this module insists on:

**An SLO is a statement about a quantile, not a mean.** "Mean TTFT was 180 ms
against a 200 ms SLO" is not compliance with anything; it is compatible with a
quarter of all requests missing. The attainment number here is the fraction of
requests that met the target, which is the quantity the SLO was about all along,
and it is compared against the target attainment (99%, 99.9%) rather than against 1.

**The knee is found, not assumed.** `sweep` takes the runs of a rate ladder and
reports where goodput turns over. On the fixtures in this repo, throughput is still
climbing two rungs past the point where goodput has already begun to fall -- so the
rung a throughput-only report would call best is two rungs into the region where
the deployment is failing its users.
"""
from . import percentile as _p

DEFAULT_TARGET_ATTAINMENT = 0.99


class Target:
    """The deadlines a request has to meet. Any of them may be left unset."""

    __slots__ = ("ttft_ms", "tpot_ms", "e2e_ms", "attainment")

    def __init__(self, ttft_ms=None, tpot_ms=None, e2e_ms=None,
                 attainment=DEFAULT_TARGET_ATTAINMENT):
        self.ttft_ms = ttft_ms
        self.tpot_ms = tpot_ms
        self.e2e_ms = e2e_ms
        self.attainment = attainment

    def any_set(self):
        return any(v is not None for v in (self.ttft_ms, self.tpot_ms, self.e2e_ms))

    def describe(self):
        bits = []
        if self.ttft_ms is not None:
            bits.append("TTFT<={:g}ms".format(self.ttft_ms))
        if self.tpot_ms is not None:
            bits.append("TPOT<={:g}ms".format(self.tpot_ms))
        if self.e2e_ms is not None:
            bits.append("E2E<={:g}ms".format(self.e2e_ms))
        bits.append("at {:.4g}% of requests".format(self.attainment * 100))
        return ", ".join(bits)

    def as_dict(self):
        return {"ttft_ms": self.ttft_ms, "tpot_ms": self.tpot_ms,
                "e2e_ms": self.e2e_ms, "attainment": self.attainment}


def meets(req, target, use_corrected=False):
    """Does one request meet every deadline it was given? Missing data -> None."""
    checked = False
    if target.ttft_ms is not None:
        t = req.ttft_s()
        if t is None:
            return None
        if req.arrival_s is not None and use_corrected and req.first_token_s is not None:
            t = req.first_token_s - req.arrival_s
        checked = True
        if t * 1e3 > target.ttft_ms:
            return False
    if target.tpot_ms is not None:
        t = req.tpot_s()
        if t is None:
            return None
        checked = True
        if t * 1e3 > target.tpot_ms:
            return False
    if target.e2e_ms is not None:
        t = req.corrected_latency_s() if use_corrected else req.latency_s()
        if t is None:
            t = req.latency_s()
        if t is None:
            return None
        checked = True
        if t * 1e3 > target.e2e_ms:
            return False
    return True if checked else None


class Attainment:
    __slots__ = ("n", "n_met", "n_undecided", "fraction", "target", "throughput",
                 "goodput", "span_s", "corrected", "note")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self):
        d = {k: getattr(self, k) for k in self.__slots__}
        d["target"] = self.target.as_dict() if self.target else None
        return d


def attainment(run, target, use_corrected=False):
    if run.evidence != "per_request" or not target.any_set():
        return None
    span = run.span_s()
    met = undecided = 0
    total = 0
    for r in run.requests:
        total += 1
        if not r.ok:
            continue                      # a failed request met nothing
        v = meets(r, target, use_corrected)
        if v is None:
            undecided += 1
        elif v:
            met += 1
    decided = total - undecided
    frac = (met / float(decided)) if decided else None
    tput = (len(run.ok_requests()) / span) if span else None
    good = (met / span) if span else None
    note = ""
    if frac is not None and tput:
        note = ("{}/{} requests met {} -> attainment {:.4g}%{}. Throughput {:.2f} "
                "req/s, goodput {:.2f} req/s ({:.0%} of it counted)."
                .format(met, decided, target.describe(), frac * 100,
                        "" if not undecided else
                        " ({} undecidable for missing fields)".format(undecided),
                        tput, good, good / tput if tput else 0.0))
    return Attainment(n=total, n_met=met, n_undecided=undecided, fraction=frac,
                      target=target, throughput=tput, goodput=good, span_s=span,
                      corrected=use_corrected, note=note)


def status(att, target):
    if att is None or att.fraction is None:
        return "unknown"
    if att.fraction >= target.attainment:
        return "ok"
    if att.fraction >= target.attainment - 0.05:
        return "warn"
    return "violation"


def mean_would_have_passed(run, target):
    """Does the mean meet the SLO while the required quantile does not?

    The exact failure the word "average" causes in an SLO conversation, reported
    with both numbers so the gap is visible rather than argued about.
    """
    if run.evidence != "per_request":
        return None
    out = []
    checks = (("TTFT", target.ttft_ms, lambda r: r.ttft_s()),
              ("TPOT", target.tpot_ms, lambda r: r.tpot_s()),
              ("E2E", target.e2e_ms, lambda r: r.latency_s()))
    for name, limit, fn in checks:
        if limit is None:
            continue
        vals = run.values(fn)
        if not vals:
            continue
        m = _p.mean(vals) * 1e3
        q = target.attainment
        est = _p.estimate(vals, q)
        qv = est.value * 1e3 if est.value is not None else None
        if qv is not None and m <= limit < qv:
            out.append((name, m, qv, limit, est))
    return out


class Rung:
    __slots__ = ("label", "rate", "throughput", "goodput", "attainment", "p99_s",
                 "n")

    def __init__(self, label, rate, throughput, goodput, att, p99_s, n):
        self.label = label
        self.rate = rate
        self.throughput = throughput
        self.goodput = goodput
        self.attainment = att
        self.p99_s = p99_s
        self.n = n

    def as_dict(self):
        return {"label": self.label, "rate": self.rate,
                "throughput": self.throughput, "goodput": self.goodput,
                "attainment": self.attainment, "p99_s": self.p99_s, "n": self.n}


def sweep(labelled_runs, target):
    """Build the rate ladder and find where goodput turns over.

    Returns `(rungs, best_throughput_index, best_goodput_index)`. When the two
    indices differ, every rung between them is a configuration a throughput-only
    report would recommend and a user would experience as broken.
    """
    rungs = []
    for label, run in labelled_runs:
        att = attainment(run, target)
        span = run.span_s()
        lats = run.values(lambda r: r.latency_s())
        rungs.append(Rung(
            label=label,
            rate=(run.n / span) if span else None,
            throughput=att.throughput if att else ((run.n / span) if span else None),
            goodput=att.goodput if att else None,
            att=att.fraction if att else None,
            p99_s=_p.quantile(lats, 0.99) if lats else None,
            n=run.n))
    rungs.sort(key=lambda r: (r.rate is None, r.rate))
    best_t = best_g = None
    for i, r in enumerate(rungs):
        if r.throughput is not None and (best_t is None
                                         or r.throughput > rungs[best_t].throughput):
            best_t = i
        if r.goodput is not None and (best_g is None
                                      or r.goodput > rungs[best_g].goodput):
            best_g = i
    return rungs, best_t, best_g
