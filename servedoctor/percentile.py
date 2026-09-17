"""Tail quantiles, and how much of one a sample can actually support.

A P99 printed to three decimals from 200 requests is the most common false number
in a serving benchmark, and it is false in a way no amount of care in the harness
can fix: at n = 200 the 90% confidence interval on the 99th percentile has **no
upper end inside the sample at all**. The largest observation is not an upper bound
on the true P99; it is simply the largest thing that happened to be drawn.

That is not a rule of thumb here. It is arithmetic, and the whole of it is in
`min_n_for`:

    the upper confidence rank falls inside the sample only when q**n <= alpha/2

which for q = 0.99 and 90% confidence is n >= 299. Below that this module refuses
to report a P99 as a number -- the same way telemetrydoctor refuses to integrate a
counter it knows the duty cycle of, and for the same reason: the refusal carries
more information than the number would have.

The interval is the exact non-parametric one built on order statistics: the number
of observations at or below the true q-quantile is Binomial(n, q), so a confidence
interval for the quantile is a pair of order statistics chosen from that binomial's
tails. No normal approximation, no bootstrap, no resampling, no seed -- the answer
is a deterministic function of n and q, which also means it is a function this
library's tests can pin exactly.
"""
import math

# Everything downstream states confidence as a fraction, and 90% is the default
# because it is what makes the n >= 299 line concrete. A 95% interval needs 368.
DEFAULT_CONF = 0.90


# ------------------------------------------------------------- point estimates
def mean(values):
    return sum(values) / len(values) if values else None


def stdev(values):
    if len(values) < 2:
        return None
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def cv_pct(values):
    """Coefficient of variation in percent -- the same shape nodebench reports."""
    m, s = mean(values), stdev(values)
    if not m or s is None:
        return None
    return 100.0 * s / m


def rank_for(n, q):
    """1-based nearest-rank index of the q-quantile in a sorted sample of n.

    Nearest-rank, not linear interpolation, and the choice matters at the sizes
    people actually run: interpolation invents a value between two observations
    and then reports it to three decimals, which reads as precision earned from
    data. At n = 200 the P99 sits between the 198th and 199th observation; the
    interpolated number is a weighted average of exactly two measurements.
    """
    if n <= 0:
        return None
    return max(1, min(n, int(math.ceil(q * n))))


def quantile(values, q):
    if not values:
        return None
    s = sorted(values)
    return s[rank_for(len(s), q) - 1]


def median(values):
    """True median: the average of the middle pair on an even-sized sample."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def tail_support(n, q):
    """How many observations sit at or above the reported quantile."""
    r = rank_for(n, q)
    return None if r is None else n - r + 1


# ------------------------------------------------------- the confidence interval
def binom_cdf(n, p):
    """P(Binomial(n, p) <= k) for every k, in log space so the tails survive."""
    lg = math.lgamma
    logs = []
    lp, lq = math.log(p), math.log1p(-p)
    for k in range(n + 1):
        logs.append(lg(n + 1) - lg(k + 1) - lg(n - k + 1) + k * lp + (n - k) * lq)
    top = max(logs)
    w = [math.exp(x - top) for x in logs]
    total = sum(w)
    out, acc = [], 0.0
    for x in w:
        acc += x / total
        out.append(min(acc, 1.0))
    return out


def ci_ranks(n, q, conf=DEFAULT_CONF):
    """1-based order-statistic ranks bounding the q-quantile at `conf` confidence.

    Returns `(lo_rank, hi_rank)`. `hi_rank > n` is not an error and not a rounding
    artefact: it is the sample telling you that its own maximum is not a
    confidence bound on the quantile you asked for.
    """
    if n <= 0:
        return None
    alpha = 1.0 - conf
    cdf = binom_cdf(n, q)
    # lo is the largest rank r whose P(B <= r-1) is still inside the lower tail;
    # hi is the smallest r whose P(B <= r-1) has covered all but the upper tail.
    # Coverage is then cdf[hi-1] - cdf[lo-1] >= 1 - alpha, by construction.
    lo = 1
    for r in range(1, n + 1):
        if cdf[r - 1] <= alpha / 2.0:
            lo = r
        else:
            break
    hi = n + 1
    for r in range(1, n + 1):
        if cdf[r - 1] >= 1.0 - alpha / 2.0:
            hi = r
            break
    return lo, hi


def min_n_for(q, conf=DEFAULT_CONF):
    """Smallest sample size whose own maximum bounds the q-quantile from above.

    The upper rank is the smallest r with P(Binom(n, q) <= r - 1) >= 1 - alpha/2.
    At r = n that probability is 1 - q**n, so the rank falls inside the sample
    exactly when

        q**n <= alpha/2      i.e.      n >= ln(alpha/2) / ln(q)

    q=0.99, 90% -> 299.  q=0.99, 95% -> 368.  q=0.999, 90% -> 2995.
    Nothing here was chosen; it was solved.
    """
    alpha = 1.0 - conf
    return int(math.ceil(math.log(alpha / 2.0) / math.log(q)))


def max_q_for(n, conf=DEFAULT_CONF):
    """The highest quantile this many samples can bound from above.

    The inverse of `min_n_for`: q <= (alpha/2) ** (1/n). It is the number to put in
    a report instead of a P99 you do not have the sample for -- 200 requests can
    carry a q98.5, and saying so is more useful than saying no.
    """
    if n <= 0:
        return None
    alpha = 1.0 - conf
    return (alpha / 2.0) ** (1.0 / n)


class Estimate:
    """A quantile with everything needed to decide whether to quote it."""

    __slots__ = ("q", "n", "value", "lo", "hi", "lo_rank", "hi_rank",
                 "support", "conf", "bounded", "min_n")

    def __init__(self, q, n, value, lo, hi, lo_rank, hi_rank, support, conf,
                 bounded, min_n):
        self.q = q
        self.n = n
        self.value = value
        self.lo = lo
        self.hi = hi
        self.lo_rank = lo_rank
        self.hi_rank = hi_rank
        self.support = support
        self.conf = conf
        self.bounded = bounded
        self.min_n = min_n

    def width_ratio(self):
        """CI width as a fraction of the point estimate. None when unbounded."""
        if not self.bounded or not self.value:
            return None
        return (self.hi - self.lo) / self.value

    def as_dict(self):
        return {"q": self.q, "n": self.n, "value": self.value, "lo": self.lo,
                "hi": self.hi, "support": self.support, "conf": self.conf,
                "bounded": self.bounded, "min_n": self.min_n}

    def __str__(self):
        if self.value is None:
            return "q{:g}: no data".format(self.q * 100)
        if not self.bounded:
            return ("q{:g}={:.4f} but UNBOUNDED ABOVE: n={} < {} needed at {:.0f}% "
                    "confidence, so the sample maximum is not an upper bound"
                    .format(self.q * 100, self.value, self.n, self.min_n,
                            self.conf * 100))
        return ("q{:g}={:.4f}  {:.0f}% CI [{:.4f}, {:.4f}] (ranks {}-{} of {}, "
                "{} obs in the tail)".format(
                    self.q * 100, self.value, self.conf * 100, self.lo, self.hi,
                    self.lo_rank, self.hi_rank, self.n, self.support))


def estimate(values, q, conf=DEFAULT_CONF):
    """The q-quantile plus its exact interval, or a statement of why there isn't one."""
    n = len(values)
    min_n = min_n_for(q, conf)
    if n == 0:
        return Estimate(q, 0, None, None, None, None, None, 0, conf, False, min_n)
    s = sorted(values)
    point = s[rank_for(n, q) - 1]
    lo_rank, hi_rank = ci_ranks(n, q, conf)
    bounded = hi_rank <= n
    lo = s[lo_rank - 1]
    hi = s[hi_rank - 1] if bounded else None
    return Estimate(q, n, point, lo, hi, lo_rank, hi_rank if bounded else None,
                    tail_support(n, q), conf, bounded, min_n)


# ------------------------------------------------------------------- merging
class MergeRefused(ValueError):
    """Raised on any attempt to combine quantiles instead of samples."""


def merge_quantiles(*_quantiles):
    """There is no such operation. This function exists to say so out loud.

    A P99 of two P99s is not the P99 of the union, and no weighting fixes it: the
    quantile of a mixture depends on the whole shape of both components, not on
    one point from each. The mistake shows up in three routine places -- averaging
    per-shard percentiles, averaging per-minute percentiles into an hour, and
    averaging percentiles over repeated runs to "reduce noise". The last one is the
    worst, because it moves the number toward the middle and calls it stability.
    """
    raise MergeRefused(
        "cannot combine quantiles; combine the samples and re-quantile them. "
        "The quantile of a union is not a function of the quantiles of its parts.")


def merge_samples(*samples):
    out = []
    for s in samples:
        out.extend(s)
    return out
