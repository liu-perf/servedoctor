"""Little's Law, and the one thing nobody says about it: when it is a tautology.

L = lambda * W is the standard sanity check on a load test -- concurrency equals
throughput times latency, so if the three printed numbers do not satisfy it, one of
them is wrong. Genuinely useful advice, and it is why this module exists.

It is also, on a per-request log, **exactly an identity**, and this is worth being
precise about because getting it wrong wastes a check:

    lambda = n / T
    W      = (sum of latencies) / n
    L      = (integral of in-flight count) / T = (sum of latencies) / T

so lambda * W = (n/T) * (sum/n) = sum/T = L, for any log, of any shape, with any
amount of queueing, steady state or not. Computing all three from the timestamps
and observing that they agree tests nothing at all -- it tests that division works.
`test_littles_law_is_an_identity_on_per_request_data` asserts equality to floating
point, so the identity cannot quietly come back as a "cross-check".

Where the law does have force:

  * **against a declared concurrency.** The harness was configured for N workers.
    The measured in-flight average L is a different number from a different source.
    L well below N means the harness never had N requests outstanding -- it was the
    client, not the server, that ran out. This is the check that finds the load
    generator running out of CPU, and it is invisible in every summary statistic.
  * **on a summary report**, where throughput, mean latency and concurrency come
    from three different places in the harness and nothing forced them to agree.
    There the disagreement is real information.

Same lesson as telemetrydoctor's cross-check, which shipped a first version whose
two "independent" segmentations were algebraically the same computation. A check
that cannot fail is not a check; the useful move is to find the pair of numbers
that were not derived from each other.
"""
from . import closure as _cl

IDENTITY = "identity"
CHECK = "check"
UNAVAILABLE = "unavailable"

# How far the measured average concurrency may sit below the configured one before
# the harness, not the server, is the thing being measured. Some slack is normal:
# ramp-up, the last drain, and any request that errored out early all pull it down.
UNDERFILL_WARN = 0.90
UNDERFILL_VIOLATION = 0.70
# Summary reports round. Agreement inside this is agreement.
SUMMARY_TOL = 0.05


class Little:
    __slots__ = ("kind", "lam", "w", "l_identity", "l_sweep", "declared", "fill",
                 "residual", "note")

    def __init__(self, kind, lam=None, w=None, l_identity=None, l_sweep=None,
                 declared=None, fill=None, residual=None, note=""):
        self.kind = kind
        self.lam = lam
        self.w = w
        self.l_identity = l_identity
        self.l_sweep = l_sweep
        self.declared = declared
        self.fill = fill
        self.residual = residual
        self.note = note

    def as_dict(self):
        return {"kind": self.kind, "lambda": self.lam, "w": self.w,
                "l_identity": self.l_identity, "l_sweep": self.l_sweep,
                "declared_concurrency": self.declared, "fill": self.fill,
                "residual": self.residual, "note": self.note}


def analyse(run, declared=None):
    if run.evidence == "per_request":
        return _per_request(run, declared)
    return _summary(run)


def _per_request(run, declared):
    span = run.span_s()
    lats = run.values(lambda r: r.latency_s())
    if not span or not lats:
        return Little(UNAVAILABLE, note="no usable timestamps")
    lam = len(lats) / span
    w = sum(lats) / len(lats)
    l_identity = lam * w
    occ = _cl.occupancy(run)
    l_sweep = occ.mean_in_flight if occ else None

    decl = declared if declared is not None else run.declared_concurrency
    if decl:
        fill = l_identity / float(decl)
        return Little(CHECK, lam, w, l_identity, l_sweep, float(decl), fill, None,
                      "measured average concurrency {:.2f} against a declared {:g} "
                      "({:.0%} filled). The declared figure did not come out of this "
                      "arithmetic, so this comparison can fail."
                      .format(l_identity, float(decl), fill))
    return Little(IDENTITY, lam, w, l_identity, l_sweep, None, None, None,
                  "lambda*W = {:.3f} and the in-flight integral = {:.3f}; these are "
                  "the same quantity computed twice, not a cross-check. Supply the "
                  "configured concurrency (--concurrency) to get a comparison that "
                  "can actually fail.".format(l_identity, l_sweep or float("nan")))


def _summary(run):
    s = run.summary
    if s is None:
        return Little(UNAVAILABLE, note="nothing to read")
    lam = s.get("request_throughput")
    if lam is None and s.has("completed", "duration_s") and s.get("duration_s"):
        lam = s.get("completed") / s.get("duration_s")
    w_ms = s.get("mean_e2el_ms")
    decl = s.get("max_concurrency")
    if lam is None or w_ms is None:
        return Little(UNAVAILABLE, None, None, None, None, decl, None, None,
                      "the report does not carry both a request throughput and a mean "
                      "end-to-end latency, so the law has nothing to be applied to")
    w = w_ms / 1000.0
    implied = lam * w
    if decl is None:
        return Little(CHECK, lam, w, implied, None, None, None, None,
                      "implied average concurrency {:.2f} from {:.3f} req/s x {:.1f} ms. "
                      "No configured concurrency in the report to compare it against -- "
                      "record it: it is the one number that makes this report checkable."
                      .format(implied, lam, w_ms))
    fill = implied / float(decl)
    residual = abs(implied - decl) / float(decl)
    return Little(CHECK, lam, w, implied, None, float(decl), fill, residual,
                  "implied concurrency {:.2f} from {:.3f} req/s x {:.1f} ms, against a "
                  "declared {:g} ({:+.1%}). These three numbers were produced by "
                  "different parts of the harness, so this one can fail -- and on a "
                  "closed-loop run it should come out at almost exactly 1.00, because "
                  "a closed loop pins concurrency by construction."
                  .format(implied, lam, w_ms, float(decl), fill - 1.0))


def status(little):
    if little.kind == UNAVAILABLE:
        return "unknown"
    if little.fill is None:
        return "info"
    if little.fill < UNDERFILL_VIOLATION or little.fill > 1.0 + 3 * SUMMARY_TOL:
        return "violation"
    if little.fill < UNDERFILL_WARN or little.fill > 1.0 + SUMMARY_TOL:
        return "warn"
    return "ok"
