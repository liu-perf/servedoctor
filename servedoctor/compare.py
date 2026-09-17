"""Two runs: first whether they may be compared at all, then whether they differ.

The order is not stylistic. Almost every serving comparison that goes wrong goes
wrong at the first step -- the two runs used different prompt lengths, or different
`max_tokens`, or one was closed-loop and the other open, or one was 200 requests
and the other 5000 -- and no amount of statistics applied afterwards repairs it.
So the gates run first, and a failed gate stops the comparison instead of
annotating it.

For runs that pass, the question "is this P99 regression real" is the same question
regressiondoctor asked about node benchmarks, but the answer here can be sharper.
regressiondoctor had to *measure* a noise floor from repeated runs because nothing
about a bandwidth number tells you its own uncertainty. A quantile does tell you:
the exact order-statistic interval in `percentile.py` is derived from n and q alone.
So the verdict is CI overlap, with two properties worth stating plainly:

  * **Disjoint intervals mean a real difference.** That direction is sound.
  * **Overlapping intervals do not mean no difference.** Non-overlap is a
    conservative test -- it can miss a real change, especially at small n. It is
    reported as `inconclusive`, never as `same`, and the word is chosen the way
    nodebench chose it for its P2P band.

A second, sharper reading is printed alongside and labelled as what it is: holding
run A's quantile fixed, the number of B's requests exceeding it is Binomial(n_B,
1-q) under the null, which gives an exact p-value. It ignores A's own uncertainty,
so it is evidence, not the verdict.
"""
from . import closure as _cl
from . import percentile as _p

DIFFERS = "differs"
INCONCLUSIVE = "inconclusive"
BLOCKED = "blocked"

# Distributional gates. A 5% difference in median prompt length is a different
# workload, not a different build.
TOKEN_TOL = 0.05
# Below this ratio of sample sizes the smaller run governs everything and the
# comparison is really a comparison with the smaller run's uncertainty.
SIZE_RATIO_WARN = 0.25


class Gate:
    __slots__ = ("name", "passed", "detail")

    def __init__(self, name, passed, detail):
        self.name = name
        self.passed = passed
        self.detail = detail

    def as_dict(self):
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _median_tokens(run, attr):
    vals = [getattr(r, attr) for r in run.ok_requests() if getattr(r, attr) is not None]
    return _p.median(vals) if vals else None


def gates(a, b, q=0.99, conf=_p.DEFAULT_CONF):
    """Everything that has to hold before a difference means anything."""
    out = []
    out.append(Gate("evidence",
                    a.evidence == b.evidence == "per_request",
                    "{} vs {}".format(a.evidence, b.evidence)))

    for label, attr in (("prompt length", "input_tokens"),
                        ("answer length", "output_tokens")):
        ma, mb = _median_tokens(a, attr), _median_tokens(b, attr)
        if ma is None or mb is None:
            out.append(Gate(label, False,
                            "not recorded in {}".format("A" if ma is None else "B")))
            continue
        rel = abs(mb - ma) / ma if ma else None
        out.append(Gate(label, rel is not None and rel <= TOKEN_TOL,
                        "median {:g} vs {:g} ({:+.1%})".format(ma, mb, (mb / ma) - 1.0)))

    need = _p.min_n_for(q, conf)
    out.append(Gate("sample size", a.n >= need and b.n >= need,
                    "n={} and n={}, need {} each for a bounded q{:g}"
                    .format(a.n, b.n, need, q * 100)))
    if a.n and b.n:
        ratio = min(a.n, b.n) / float(max(a.n, b.n))
        out.append(Gate("balance", ratio >= SIZE_RATIO_WARN,
                        "smaller run is {:.0%} of the larger".format(ratio)))

    va, vb = _cl.classify(a), _cl.classify(b)
    out.append(Gate("load shape", va.kind == vb.kind and va.kind != _cl.UNKNOWN,
                    "{} vs {}".format(va.kind, vb.kind)))
    return out


def _binom_two_sided(k, n, p):
    """Exact two-sided p-value for k successes out of n at probability p."""
    if n <= 0:
        return None
    cdf = _p.binom_cdf(n, p)
    pmf = [cdf[0]] + [cdf[i] - cdf[i - 1] for i in range(1, n + 1)]
    obs = pmf[k]
    # The usual "sum every outcome no more likely than the observed one" rule.
    tol = 1e-12
    return min(1.0, sum(x for x in pmf if x <= obs + tol))


class Comparison:
    __slots__ = ("verdict", "q", "a", "b", "ratio", "p_value", "k_exceeding",
                 "gates", "note")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self):
        return {"verdict": self.verdict, "q": self.q,
                "a": self.a.as_dict() if self.a else None,
                "b": self.b.as_dict() if self.b else None,
                "ratio": self.ratio, "p_value": self.p_value,
                "k_exceeding": self.k_exceeding,
                "gates": [g.as_dict() for g in (self.gates or ())],
                "note": self.note}


def compare(a, b, q=0.99, conf=_p.DEFAULT_CONF, metric=None):
    """Compare the q-quantile of `metric` (default end-to-end latency)."""
    metric = metric or (lambda r: r.latency_s())
    gs = gates(a, b, q, conf)
    blocked = [g for g in gs if not g.passed]
    va = a.values(metric)
    vb = b.values(metric)
    ea = _p.estimate(va, q, conf) if va else None
    eb = _p.estimate(vb, q, conf) if vb else None
    if blocked:
        return Comparison(
            verdict=BLOCKED, q=q, a=ea, b=eb, ratio=None, p_value=None,
            k_exceeding=None, gates=gs,
            note="not comparable: " + "; ".join(
                "{} ({})".format(g.name, g.detail) for g in blocked))
    ratio = (eb.value / ea.value) if (ea and eb and ea.value) else None
    k = sum(1 for v in vb if v > ea.value) if ea and ea.value is not None else None
    pval = _binom_two_sided(k, len(vb), 1.0 - q) if k is not None else None

    disjoint = False
    if ea and eb and ea.bounded and eb.bounded:
        disjoint = ea.hi < eb.lo or eb.hi < ea.lo
    verdict = DIFFERS if disjoint else INCONCLUSIVE
    note = ("A q{:g}={:.4f}s {:.0f}% CI [{:.4f}, {:.4f}]; B q{:g}={:.4f}s CI "
            "[{:.4f}, {:.4f}]; B/A = {:.3f}. Intervals are {}."
            .format(q * 100, ea.value, conf * 100, ea.lo, ea.hi, q * 100, eb.value,
                    eb.lo, eb.hi, ratio, "disjoint" if disjoint else "overlapping")
            if (ea and eb and ea.bounded and eb.bounded) else
            "at least one quantile has no upper confidence bound in its own sample")
    if verdict == INCONCLUSIVE:
        note += (" Overlap is not evidence of sameness -- this test is conservative, "
                 "and at these sample sizes it can miss a real change.")
    if pval is not None:
        note += (" Holding A's quantile fixed, {} of B's {} requests exceed it "
                 "against {:.1f} expected; exact two-sided p = {:.4f} (evidence, not "
                 "the verdict: it ignores A's own uncertainty)."
                 .format(k, len(vb), (1.0 - q) * len(vb), pval))
    return Comparison(verdict=verdict, q=q, a=ea, b=eb, ratio=ratio, p_value=pval,
                      k_exceeding=k, gates=gs, note=note)
