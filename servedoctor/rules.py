"""The fourteen rules. Each one is a mistake that ships, not one that could.

Every rule has a provenance line in `docs/rules.md`: a published account of the
failure mode, a shape that appears in real harness output, or an experiment in
`examples/coordinated_omission.py` that runs on any machine with a Python. Three of
them say "source: this repository", because they were mistakes made while writing
it -- the same convention the previous six tools use, and for the same reason. A
rule list assembled by imagining what could go wrong is a list of things that never
do; the ones that actually happen are a small and unobvious subset.

Statuses, in the vocabulary the other six tools use:

    ok          this run does not have this problem
    info        a fact the reader needs before quoting any number from this run
    warn        a figure computed the obvious way would mislead
    violation   a figure computed the obvious way is wrong, by a stated amount
    unknown     the file does not contain what is needed to decide -- NOT a pass

`unknown` sits above `info` and below `warn`, and `--fail-on unknown` is a
reasonable CI setting. A benchmark that cannot be checked has not passed a check.
"""
from . import closure as _cl
from . import littles as _ll
from . import omission as _om
from . import percentile as _p
from . import queueing as _q
from . import slo as _slo
from . import tokens as _tok
from . import window as _win

OK = "ok"
INFO = "info"
WARN = "warn"
VIOLATION = "violation"
UNKNOWN = "unknown"

SEVERITY = {OK: 0, INFO: 1, UNKNOWN: 2, WARN: 3, VIOLATION: 4}

# A token rate that is this much prompt is not a statement about decode speed.
PREFILL_WARN_SHARE = 0.60
# TPOT denominator error at or above this is a first-order reporting error.
TPOT_ERR_WARN = 0.02
# Share of requests that must fall inside the steady window before the whole-run
# statistics are about steady state.
STEADY_WARN = 0.70


class Finding:
    __slots__ = ("rule", "location", "status", "message")

    def __init__(self, rule, location, status, message):
        self.rule = rule
        self.location = location
        self.status = status
        self.message = message

    def as_dict(self):
        return {"rule": self.rule, "location": self.location,
                "status": self.status, "message": self.message}

    def __str__(self):
        return "{}: [{}] {}".format(self.location, self.status, self.message)


class Context:
    """Everything the rules read, computed once."""

    __slots__ = ("run", "q", "conf", "target", "closure", "omission", "warmup",
                 "steady", "censoring", "tokens", "little", "queueing",
                 "attainment", "latency", "declared_rate", "capacity_rps")

    def __init__(self, run, target=None, q=0.99, conf=_p.DEFAULT_CONF,
                 declared_concurrency=None, declared_rate=None, capacity_rps=None):
        self.run = run
        self.q = q
        self.conf = conf
        self.target = target or _slo.Target()
        self.declared_rate = declared_rate if declared_rate is not None \
            else run.declared_rate
        self.capacity_rps = capacity_rps
        self.closure = _cl.classify(run)
        self.omission = _om.analyse(run, q, conf)
        self.warmup = _win.warmup(run, q) if run.evidence == "per_request" else None
        self.steady = _win.steady(run) if run.evidence == "per_request" else None
        self.censoring = _win.censoring(run, q) if run.evidence == "per_request" \
            else None
        self.tokens = _tok.analyse(run) if run.evidence == "per_request" else None
        self.little = _ll.analyse(run, declared_concurrency)
        self.queueing = _q.analyse(run, capacity_rps)
        self.attainment = _slo.attainment(run, self.target) \
            if self.target.any_set() else None
        self.latency = _p.estimate(run.values(lambda r: r.latency_s()), q, conf) \
            if run.evidence == "per_request" else None


# ---------------------------------------------------------------- SD001 evidence
def sd001_evidence(ctx):
    run = ctx.run
    if run.evidence == "per_request":
        cols = ""
        if run.extra_columns:
            cols = "; columns nobody claimed: " + ", ".join(run.extra_columns)
        recon = ""
        if run.reconstructed:
            recon = ("; {} reconstructed from a duration column, so it carries "
                     "whatever rounding the harness applied first"
                     .format(" and ".join(run.reconstructed)))
        return Finding(
            "SD001", "evidence", INFO,
            "per-request log, {} requests{}{}. Every rule below is available."
            .format(run.n, recon, cols))
    unknown = ""
    if run.summary is not None and run.summary.unknown_fields:
        unknown = " ({} field(s) present but unrecognised)".format(
            len(run.summary.unknown_fields))
    return Finding(
        "SD001", "evidence", UNKNOWN,
        "summary report{}: the distributional questions were answered before this "
        "file was written and cannot be re-asked. Nine of the fourteen rules need "
        "per-request timestamps and will return unknown. This is not a defect in the "
        "report -- it is what a summary is -- but a summary cannot be audited, and "
        "'cannot be audited' is different from 'passed'.".format(unknown))


# ------------------------------------------------------------------ SD002 closure
def sd002_closure(ctx):
    v = ctx.closure
    if v.kind == _cl.UNKNOWN:
        return Finding("SD002", "closure", UNKNOWN, v.reason)
    if v.kind == _cl.CLOSED_LOOP:
        return Finding(
            "SD002", "closure", VIOLATION,
            v.reason + " Re-run open-loop, or report this as a throughput "
            "measurement and delete the percentiles from it.")
    if v.kind == _cl.INCONCLUSIVE:
        return Finding("SD002", "closure", UNKNOWN, v.reason)
    return Finding("SD002", "closure", OK, v.reason)


# ----------------------------------------------------------------- SD003 omission
def sd003_omission(ctx):
    o = ctx.omission
    if o is None or not o.available:
        deficit = None
        if ctx.declared_rate:
            deficit = _om.deficit(ctx.run, ctx.declared_rate)
        extra = ""
        if deficit:
            expected, actual, missing = deficit
            if missing > 0:
                extra = (" A declared rate of {:g} req/s over {:.1f}s implies {:.0f} "
                         "requests; {} are in the file, so {:.0f} were never sent -- "
                         "and they were skipped precisely while the server was slow."
                         .format(ctx.declared_rate, ctx.run.span_s() or 0.0,
                                 expected, actual, missing))
        return Finding("SD003", "omission", UNKNOWN,
                       (o.reason if o else "no data") + extra)
    st = _om.status(o.factor)
    msg = ("q{:g} from the send time is {:.4f}s; from the time the request was due it "
           "is {:.4f}s -- {:.2f}x. The harness was late on {:.1%} of requests (median "
           "{:.1f} ms, worst {:.1f} ms)."
           .format(ctx.q * 100, o.raw.value, o.corrected.value, o.factor,
                   o.late_fraction, o.median_wait_s * 1e3, o.max_wait_s * 1e3))
    if st == OK:
        return Finding("SD003", "omission", OK,
                       msg + " The schedule was kept, so the two agree.")
    return Finding(
        "SD003", "omission", st,
        msg + " The second number is the one a user experienced. The gap is not "
        "measurement error: it is the load generator's own queue, and reporting the "
        "first figure moves the deadline to whenever the harness got around to it.")


# ---------------------------------------------------- SD004 quantile has support
def sd004_quantile_support(ctx):
    e = ctx.latency
    if e is None:
        return Finding("SD004", "q{:g}".format(ctx.q * 100), UNKNOWN,
                       "no per-request latencies to build a quantile from")
    if e.value is None:
        return Finding("SD004", "q{:g}".format(ctx.q * 100), UNKNOWN, "no samples")
    if not e.bounded:
        return Finding(
            "SD004", "q{:g}".format(ctx.q * 100), VIOLATION,
            "n={} but a bounded {:.0f}% interval on q{:g} needs n>={}. Only {} "
            "observation(s) sit at or above the reported {:.4f}s, and the upper "
            "confidence rank falls off the top of the sample -- the largest "
            "measurement is not an upper bound on this quantile, it is just the "
            "largest draw. This sample can carry q{:.4g} -- report that, or send {} "
            "more requests. The threshold is solved, not chosen: the upper rank lands "
            "inside the sample exactly when q**n <= (1-conf)/2."
            .format(e.n, ctx.conf * 100, ctx.q * 100, e.min_n, e.support, e.value,
                    (_p.max_q_for(e.n, ctx.conf) or 0.0) * 100, e.min_n - e.n))
    width = e.width_ratio()
    status = WARN if (width is not None and width > 0.25) else OK
    return Finding(
        "SD004", "q{:g}".format(ctx.q * 100), status,
        "q{:g}={:.4f}s, {:.0f}% CI [{:.4f}, {:.4f}] from order statistics {}-{} of "
        "{} ({} observations in the tail, interval is {:.0%} of the point estimate). "
        "{}".format(ctx.q * 100, e.value, ctx.conf * 100, e.lo, e.hi, e.lo_rank,
                    e.hi_rank, e.n, e.support, width or 0.0,
                    "Quote the interval, not the point."
                    if status == WARN else "Narrow enough to quote."))


# ------------------------------------------------------------------- SD005 warmup
def sd005_warmup(ctx):
    w = ctx.warmup
    if w is None:
        return Finding("SD005", "warmup", UNKNOWN,
                       "fewer than 10 complete requests, or no per-request data")
    if w.count == 0:
        return Finding("SD005", "warmup", OK, w.note)
    return Finding(
        "SD005", "warmup", WARN,
        w.note + ". A cold start is a real measurement of a real event and belongs "
        "in a cold-start report; mixed into steady-state percentiles it is one "
        "sample, which on a short run is enough to *be* the P99.")


# ------------------------------------------------------------- SD006 steady window
def sd006_steady_window(ctx):
    s = ctx.steady
    if s is None:
        return Finding("SD006", "steady", UNKNOWN,
                       "concurrency never exceeded 1, or too few requests, so ramp-up "
                       "and steady state cannot be told apart")
    status = OK if s.fraction >= STEADY_WARN else WARN
    tail = ("" if status == OK else
            " Only {:.0%} of requests met a fully loaded server; the rest met a "
            "ramping or draining one, and averaging them in moves every statistic "
            "toward the optimistic side.".format(s.fraction))
    return Finding("SD006", "steady", status, s.note + "." + tail)


# --------------------------------------------------------------- SD007 censoring
def sd007_censoring(ctx):
    c = ctx.censoring
    if c is None:
        return Finding("SD007", "censoring", UNKNOWN, "no per-request data")
    if c.n_dropped == 0:
        return Finding("SD007", "censoring", OK, c.note)
    if not c.recoverable:
        return Finding("SD007", "censoring", VIOLATION, c.note)
    return Finding("SD007", "censoring", WARN, c.note)


# ----------------------------------------------------------- SD008 TPOT denominator
def sd008_tpot_denominator(ctx):
    t = ctx.tokens
    if t is None or t.tpot_error is None:
        return Finding("SD008", "tpot", UNKNOWN,
                       "no first-token timestamps and output-token counts together, "
                       "so TPOT cannot be formed either way")
    status = WARN if t.tpot_error >= TPOT_ERR_WARN else OK
    extra = ("" if status == OK else
             " On a median answer of {:g} tokens the denominator is a reporting "
             "decision, not a rounding one. This is the class of workload -- "
             "classification, routing, extraction, structured output -- where the "
             "error is largest and where nobody looks, because the habit was formed "
             "on thousand-token generations where it is 0.1%.".format(t.median_output))
    return Finding("SD008", "tpot", status, t.tpot_note + "." + extra)


# --------------------------------------------------------- SD009 token composition
def sd009_token_composition(ctx):
    t = ctx.tokens
    if t is None:
        s = _tok.summary_view(ctx.run)
        if s:
            return Finding("SD009", "tokens", INFO, s)
        return Finding("SD009", "tokens", UNKNOWN, "no token counts in this file")
    if t.prefill_share is None:
        return Finding("SD009", "tokens", UNKNOWN,
                       "no prompt-token column: the prompt/output mix of this run is "
                       "not recorded, so its token rate cannot be compared with any "
                       "other run's")
    status = WARN if t.prefill_share >= PREFILL_WARN_SHARE else OK
    extra = ("" if status == OK else
             " At this mix a 'tokens/s' headline is mostly prefill, which is "
             "compute-bound and parallel over sequence length, while decode is "
             "bandwidth-bound and serial. Adding them yields a number whose value "
             "depends on the traffic it was measured on.")
    return Finding("SD009", "tokens", status, t.rate_note + "." + extra)


# --------------------------------------------------------------- SD010 Little's Law
def sd010_littles_law(ctx):
    lt = ctx.little
    st = _ll.status(lt)
    return Finding("SD010", "concurrency", st, lt.note)


# ------------------------------------------------------------ SD011 arrival process
def sd011_arrival_process(ctx):
    v = ctx.closure
    if v.arrival_kind == _cl.UNKNOWN or v.arrival_cv is None:
        return Finding("SD011", "arrivals", UNKNOWN,
                       "fewer than three send times, so the arrival process is not "
                       "characterised")
    if v.kind == _cl.CLOSED_LOOP:
        return Finding(
            "SD011", "arrivals", WARN,
            "inter-send CV {:.2f} ({}), but this was a closed loop, so there is no "
            "arrival process to describe: the send times are the completion times "
            "shifted. A --request-rate flag on a closed-loop harness sets a ceiling, "
            "not a rate.".format(v.arrival_cv, v.arrival_kind))
    if v.arrival_kind == "deterministic":
        return Finding(
            "SD011", "arrivals", INFO,
            "inter-send CV {:.2f}: a fixed-rate schedule. Honest and reproducible, "
            "but it under-queues relative to real traffic -- a D/D/1 queue below "
            "capacity never waits at all, while Poisson traffic at the same mean rate "
            "does. Tail numbers from a fixed-rate generator are a lower bound on what "
            "bursty arrivals produce.".format(v.arrival_cv))
    if v.arrival_kind == "poisson_like":
        return Finding("SD011", "arrivals", OK,
                       "inter-send CV {:.2f}: Poisson-like, which is the assumption "
                       "every queueing bound in this tool needs and the one most "
                       "harnesses silently violate.".format(v.arrival_cv))
    return Finding("SD011", "arrivals", INFO,
                   "inter-send CV {:.2f}: {} -- burstier than Poisson. Real traffic "
                   "often is; just do not then quote a Poisson bound."
                   .format(v.arrival_cv, v.arrival_kind))


# -------------------------------------------------------------- SD012 queueing shape
def sd012_queueing(ctx):
    qq = ctx.queueing
    if qq is None:
        return Finding("SD012", "queueing", UNKNOWN,
                       "no per-request latencies, so utilisation and queueing cannot "
                       "be estimated")
    if qq.triangle:
        return Finding("SD012", "queueing", WARN, qq.triangle)
    return Finding("SD012", "queueing", INFO, qq.note)


# --------------------------------------------------------------------- SD013 SLO
def sd013_slo(ctx):
    if not ctx.target.any_set():
        return Finding("SD013", "slo", UNKNOWN,
                       "no SLO given (--ttft-ms / --tpot-ms / --e2e-ms), so whether "
                       "this run met one is not a question this file can answer. A "
                       "benchmark without a deadline reports throughput at an "
                       "unstated latency.")
    att = ctx.attainment
    if att is None or att.fraction is None:
        return Finding("SD013", "slo", UNKNOWN,
                       "the SLO names a metric this file does not carry")
    st = _slo.status(att, ctx.target)
    fooled = _slo.mean_would_have_passed(ctx.run, ctx.target) or []
    extra = ""
    for name, m, qv, limit, _e in fooled:
        extra += (" Note: mean {} is {:.1f} ms and would have passed the {:g} ms "
                  "target, while the q{:.4g} is {:.1f} ms and does not. An SLO stated "
                  "as a mean is not an SLO."
                  .format(name, m, limit, ctx.target.attainment * 100, qv))
    if att.throughput and att.goodput is not None and att.throughput > 0:
        share = att.goodput / att.throughput
        if share < 0.999:
            extra += (" {:.1%} of the finished work was too late to be worth having, "
                      "and a throughput-only report counts all of it."
                      .format(1.0 - share))
    return Finding("SD013", "slo", st, att.note + extra)


# ------------------------------------------------------------ SD014 input hygiene
def sd014_input_hygiene(ctx):
    run = ctx.run
    problems = []
    if run.evidence == "per_request":
        no_end = sum(1 for r in run.requests if r.end_s is None)
        no_ttft = sum(1 for r in run.requests if r.first_token_s is None)
        no_tokens = sum(1 for r in run.requests if r.output_tokens is None)
        neg = sum(1 for r in run.requests
                  if r.end_s is not None and r.end_s < r.start_s)
        if no_end:
            problems.append("{} row(s) have no completion time".format(no_end))
        if no_ttft:
            problems.append("{} row(s) have no first-token time, so TTFT and TPOT "
                            "are unavailable for them".format(no_ttft))
        if no_tokens:
            problems.append("{} row(s) have no output-token count".format(no_tokens))
        if neg:
            problems.append("{} row(s) finish before they start".format(neg))
        if run.reconstructed:
            problems.append("{} reconstructed from duration columns"
                            .format("/".join(run.reconstructed)))
        if run.extra_columns:
            problems.append("unclaimed columns: " + ", ".join(run.extra_columns))
    elif run.summary is not None:
        missing = [f for f in ("max_concurrency", "request_rate", "output_throughput")
                   if run.summary.fields.get(f) is None]
        if missing:
            problems.append("report omits " + ", ".join(missing))
    if not problems:
        return Finding("SD014", "input", OK,
                       "every row carries a completion time, a first-token time and a "
                       "token count, and every column was recognised")
    return Finding("SD014", "input", WARN,
                   "; ".join(problems) + ". A field this tool could not read is a "
                   "field whose unit nobody checked.")


RULES = [
    ("SD001", sd001_evidence),
    ("SD002", sd002_closure),
    ("SD003", sd003_omission),
    ("SD004", sd004_quantile_support),
    ("SD005", sd005_warmup),
    ("SD006", sd006_steady_window),
    ("SD007", sd007_censoring),
    ("SD008", sd008_tpot_denominator),
    ("SD009", sd009_token_composition),
    ("SD010", sd010_littles_law),
    ("SD011", sd011_arrival_process),
    ("SD012", sd012_queueing),
    ("SD013", sd013_slo),
    ("SD014", sd014_input_hygiene),
]


def audit(run, target=None, q=0.99, conf=_p.DEFAULT_CONF, only=None,
          declared_concurrency=None, declared_rate=None, capacity_rps=None):
    ctx = Context(run, target, q, conf, declared_concurrency, declared_rate,
                  capacity_rps)
    wanted = None if not only else {s.upper() for s in only}
    out = []
    for name, fn in RULES:
        if wanted and name not in wanted:
            continue
        out.append(fn(ctx))
    return out, ctx


def worst_status(findings):
    worst = OK
    for f in findings:
        if SEVERITY.get(f.status, 0) > SEVERITY.get(worst, 0):
            worst = f.status
    return worst


def describe_rules():
    """One line per rule, for `servedoctor rules`."""
    text = {
        "SD001": "what kind of evidence this file is, and therefore what may be "
                 "concluded from it at all",
        "SD002": "open loop or closed loop, decided from the log by two independent "
                 "detectors",
        "SD003": "coordinated omission: latency from the send time vs from the time "
                 "the request was due",
        "SD004": "does the sample support the quantile being reported -- exact "
                 "order-statistic interval, and a refusal below n=299 for P99",
        "SD005": "a cold start left in with the steady-state requests, where one "
                 "sample is enough to be the P99 of a short run",
        "SD006": "how much of the run was ramp-up and drain rather than steady state",
        "SD007": "failed and unfinished requests dropped from the tail they define",
        "SD008": "TPOT divided by m instead of m-1, which understates it by "
                 "m/(m-1) and is invisible on long generations",
        "SD009": "prompt tokens and output tokens added into one rate",
        "SD010": "Little's Law against a declared concurrency -- and where it is an "
                 "identity instead of a check",
        "SD011": "the arrival process, which every queueing claim depends on",
        "SD012": "utilisation against the queueing floor it implies",
        "SD013": "SLO attainment as a quantile, and goodput rather than throughput",
        "SD014": "fields this tool could not read, which are the fields nobody "
                 "checked the units of",
    }
    return [Finding(name, name, INFO, text[name]) for name, _fn in RULES]
