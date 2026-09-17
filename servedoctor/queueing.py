"""Why "keep the accelerator busy" and "keep the tail short" are the same dial.

Every capacity conversation eventually contains two sentences that cannot both be
satisfied: *we want the GPUs at high utilisation* and *we want P99 latency flat*.
They are not competing preferences, they are one quantity read from two ends. For
any queue fed by arrivals it does not control, waiting time grows as 1/(1 - rho),
and the growth is not gentle near the top:

    rho     M/M/1 wait, in units of one service time
    0.50    1.0
    0.80    4.0
    0.90    9.0
    0.95    19.0
    0.99    99.0

Going from 80% to 95% utilisation buys 19% more throughput and costs 4.75x the
queueing delay. That trade is the whole of capacity planning, and it is arithmetic,
not opinion.

**What this module does not claim.** An LLM server with continuous batching is not
an M/M/1 queue. It admits many requests at once, they share the accelerator, and
the service rate of any one of them depends on how many others are resident. The
single-server formulas below are a *shape*, not a model of the server, and this
tool uses them for exactly two things:

  1. printing the trade-off table above against the run's own measured service
     time, so a capacity decision is made against numbers rather than a feeling;
  2. one consistency triangle. Under Poisson arrivals at utilisation rho, some
     queueing is unavoidable -- the M/D/1 value is a floor, because deterministic
     service minimises waiting among all service distributions with the same mean.
     A report claiming high utilisation, Poisson arrivals *and* queueing below that
     floor is claiming three things that cannot all be true. It does not say which
     one is false. Usually it is the arrival process, and the reason is that the
     harness was closed-loop -- which `closure.py` can check independently.

Reporting a bound whose assumptions the data can be tested against, and then
testing them, is the difference between a queueing model and a queueing anecdote.
"""
import math

from . import closure as _cl
from . import percentile as _p

# The unloaded-service-time proxy. Requests that met the least queueing are the
# fastest ones; their quantile is the closest thing a log has to a service time.
# Not the minimum: a single lucky request is not an estimate.
SERVICE_QUANTILE = 0.05
# Below this utilisation the 1/(1-rho) shape has not started to matter and the
# consistency triangle has no force.
TRIANGLE_MIN_RHO = 0.50
# Slack on the floor. The proxy service time is an estimate, and an estimate 20%
# high would manufacture a violation on its own.
FLOOR_SLACK = 0.80


def mm1_wait(rho, service_s):
    """Mean queueing delay for exponential service. Infinite at rho >= 1."""
    if rho is None or service_s is None or rho >= 1.0 or rho < 0:
        return None
    return service_s * rho / (1.0 - rho)


def md1_wait(rho, service_s):
    """Mean queueing delay for deterministic service -- the M/G/1 floor."""
    if rho is None or service_s is None or rho >= 1.0 or rho < 0:
        return None
    return service_s * rho / (2.0 * (1.0 - rho))


def mg1_wait(rho, service_s, service_cv):
    """Pollaczek-Khinchine: the general single-server mean wait."""
    if rho is None or service_s is None or service_cv is None or rho >= 1.0:
        return None
    return service_s * rho * (1.0 + service_cv ** 2) / (2.0 * (1.0 - rho))


def tradeoff_table(service_s, rhos=(0.50, 0.80, 0.90, 0.95, 0.99)):
    rows = []
    for rho in rhos:
        rows.append((rho, mm1_wait(rho, service_s), md1_wait(rho, service_s)))
    return rows


class Queueing:
    __slots__ = ("rho", "lam", "service_s", "service_cv", "observed_wait_s",
                 "mm1_s", "md1_s", "mg1_s", "arrival_kind", "arrival_cv",
                 "offered_concurrency", "triangle", "note")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def analyse(run, capacity_rps=None):
    """Utilisation, the model's floor, and whether the run's own claims cohere.

    `capacity_rps` is the saturated throughput of the deployment, if known. Without
    it, utilisation is estimated as `lambda * E[S]`, which is the single-server
    reading and will be wrong on a batching server by roughly the batch size -- so
    the estimate is labelled as such everywhere it appears.
    """
    if run.evidence != "per_request":
        return None
    lats = run.values(lambda r: r.latency_s())
    span = run.span_s()
    if not lats or not span:
        return None
    lam = len(lats) / span
    service = _p.quantile(lats, SERVICE_QUANTILE)
    if not service:
        return None
    slow = [x for x in lats if x > service]
    service_cv = (_p.cv_pct(lats) or 0.0) / 100.0
    observed_wait = max(0.0, _p.mean(lats) - service)
    offered = lam * service
    cv, kind, _src = _cl.arrival_process(run)

    # Utilisation requires knowing the capacity. `lambda * E[S]` is the offered
    # concurrency, and on any deployment that serves more than one request at a time
    # it exceeds 1 without anything being wrong -- this run offers 6.3. An earlier
    # version clamped it to 0.999 and called it utilisation, which produced a 398
    # second M/D/1 "floor" on a run whose entire tail was 13 seconds. Refusing to
    # estimate is the fix; the trade-off table below needs no measured rho, because
    # its rho values are the hypothetical ones being asked about.
    rho = (lam / capacity_rps) if capacity_rps else None
    mm1 = mm1_wait(rho, service) if rho is not None else None
    md1 = md1_wait(rho, service) if rho is not None else None
    mg1 = mg1_wait(rho, service, service_cv) if rho is not None else None

    triangle = None
    if (rho is not None and kind == "poisson_like" and rho >= TRIANGLE_MIN_RHO
            and md1 and observed_wait < FLOOR_SLACK * md1):
        triangle = ("arrivals look Poisson (CV {:.2f}), utilisation is {:.0%} against "
                    "the {:g} req/s capacity you gave, and the mean wait above the "
                    "q{:g} service time is {:.1f} ms -- below the {:.1f} ms M/D/1 "
                    "floor those two imply. One of the three is false, and this tool "
                    "does not say which. Check the arrival process first: a harness "
                    "that waits for each response before sending is not Poisson, "
                    "whatever its --request-rate flag says."
                    .format(cv if cv is not None else float("nan"), rho,
                            capacity_rps, SERVICE_QUANTILE * 100,
                            observed_wait * 1e3, md1 * 1e3))

    head = ("service time proxy {:.1f} ms (q{:g} of end-to-end; {} requests are slower "
            "than it); lambda {:.2f} req/s; offered concurrency lambda*E[S] = {:.2f}, "
            "so this deployment serves at least {:.0f} requests at once; observed mean "
            "above the service proxy is {:.1f} ms"
            .format(service * 1e3, SERVICE_QUANTILE * 100, len(slow), lam, offered,
                    max(1.0, offered), observed_wait * 1e3))
    if rho is None:
        head += (". Utilisation is not computed: it needs the saturated throughput "
                 "(--capacity), and lambda*E[S] is not it on a server that batches")
    else:
        head += (". Utilisation {:.0%} against the given capacity; M/M/1 would wait "
                 "{:.1f} ms there, M/D/1 {:.1f} ms, Pollaczek-Khinchine at the "
                 "observed CV {:.2f} would wait {:.1f} ms"
                 .format(rho, mm1 * 1e3, md1 * 1e3, service_cv, mg1 * 1e3))
    return Queueing(rho=rho, lam=lam, service_s=service, service_cv=service_cv,
                    observed_wait_s=observed_wait, mm1_s=mm1, md1_s=md1, mg1_s=mg1,
                    arrival_kind=kind, arrival_cv=cv, offered_concurrency=offered,
                    triangle=triangle, note=head + ".")


def headroom_for(target_wait_ratio):
    """The utilisation at which M/M/1 queueing reaches `ratio` service times.

    The inverse of the table: rho = r / (1 + r). Wanting queueing held to one
    service time means running at 50%; to a tenth of one, at 9%. This is the number
    that makes "we will just add capacity later" expensive, and it does not depend
    on the hardware.
    """
    if target_wait_ratio is None or target_wait_ratio < 0:
        return None
    return target_wait_ratio / (1.0 + target_wait_ratio)


def erlang_c_servers(lam, service_s, target_wait_s, max_servers=1024):
    """Smallest c for which M/M/c mean wait is under target. Whole-number capacity.

    Included because the single-server table above overstates the pain for a
    deployment that is really c replicas behind a load balancer: at the same
    utilisation, more servers wait less. It is the honest counterweight to the
    1/(1-rho) scare, and it is why consolidation onto fewer, larger replicas is a
    latency decision and not only a cost one.
    """
    if not lam or not service_s or not target_wait_s:
        return None
    a = lam * service_s
    for c in range(max(1, int(math.ceil(a))), max_servers + 1):
        rho = a / c
        if rho >= 1.0:
            continue
        # Erlang C, computed with the numerically stable recurrence rather than
        # factorials, which overflow long before c gets interesting.
        inv = 1.0
        term = 1.0
        for k in range(1, c):
            term *= a / k
            inv += term
        term *= a / c
        top = term / (1.0 - rho)
        pw = top / (inv + top)
        if pw * service_s / (c * (1.0 - rho)) <= target_wait_s:
            return c
    return None
