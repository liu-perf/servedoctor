"""Command line entry point.

    servedoctor audit     run.csv --ttft-ms 500 --tpot-ms 50
    servedoctor latency   run.csv
    servedoctor closure   run.csv
    servedoctor slo       run.csv --e2e-ms 2000
    servedoctor queueing  run.csv
    servedoctor sweep     rate*.csv --e2e-ms 2000
    servedoctor compare   before.csv after.csv
    servedoctor rules

Output is one finding per line in the same shape the other six tools use:

    {location}: [{status}] {message}

`--json` emits the same content as a list of objects. `--fail-on` turns a status
into a non-zero exit for CI; like the other tools it is a threshold and not an
equality, so `--fail-on warn` also trips on `violation`.

Exit codes: 0 clean or gate not reached, 1 gate tripped, 2 could not read the input.
"""
import argparse
import json
import os
import sys

from . import closure as _cl
from . import compare as _cmp
from . import parse as _parse
from . import percentile as _p
from . import queueing as _q
from . import rules as _rules
from . import slo as _slo


def _fail_reached(statuses, gate):
    if not gate:
        return False
    if gate == "any":
        gate = "info"
    floor = _rules.SEVERITY.get(gate)
    if floor is None:
        return False
    return any(_rules.SEVERITY.get(s, 0) >= floor for s in statuses)


def _emit(findings, as_json):
    if as_json:
        print(json.dumps([f.as_dict() for f in findings], indent=2, ensure_ascii=False))
    else:
        for f in findings:
            print(f)


def _finish(findings, args):
    _emit(findings, args.json)
    return 1 if _fail_reached([f.status for f in findings], args.fail_on) else 0


def _load(path, args):
    return _parse.load(path, fmt=None if args.format == "auto" else args.format)


def _target(args):
    return _slo.Target(ttft_ms=args.ttft_ms, tpot_ms=args.tpot_ms,
                       e2e_ms=args.e2e_ms, attainment=args.attainment)


def _header(run):
    span = run.span_s()
    return _rules.Finding(
        "input", "source", _rules.INFO,
        "{}: {}, {}{}".format(
            run.path or "<input>", run.source,
            "{} requests".format(run.n) if run.n else "summary only",
            "; span {:.2f}s".format(span) if span else ""))


# ------------------------------------------------------------------------ audit
def cmd_audit(args):
    run = _load(args.file, args)
    findings, _ctx = _rules.audit(
        run, _target(args), args.q, args.conf, only=args.rule or None,
        declared_concurrency=args.concurrency, declared_rate=args.rate,
        capacity_rps=args.capacity)
    _emit([_header(run)] + findings, args.json)
    return 1 if _fail_reached([f.status for f in findings], args.fail_on) else 0


# ---------------------------------------------------------------------- latency
QUANTILES = (0.50, 0.90, 0.95, 0.99, 0.999)


def cmd_latency(args):
    run = _load(args.file, args)
    out = [_header(run)]
    if run.evidence != _parse.PER_REQUEST:
        out.append(_rules.Finding(
            "latency", "all", _rules.UNKNOWN,
            "summary report: the quantiles in it were computed elsewhere and carry no "
            "sample size, so none of them can be given an interval here"))
        return _finish(out, args)

    metrics = (("e2e", lambda r: r.latency_s()),
               ("e2e_from_due", lambda r: r.corrected_latency_s()),
               ("ttft", lambda r: r.ttft_s()),
               ("tpot", lambda r: r.tpot_s()))
    for name, fn in metrics:
        vals = run.values(fn)
        if not vals:
            out.append(_rules.Finding("latency", name, _rules.UNKNOWN,
                                      "not present in this file"))
            continue
        out.append(_rules.Finding(
            "latency", name + ".mean", _rules.INFO,
            "{:.4f}s over {} samples (CV {:.1f}%)".format(
                _p.mean(vals), len(vals), _p.cv_pct(vals) or 0.0)))
        for q in QUANTILES:
            e = _p.estimate(vals, q, args.conf)
            status = _rules.INFO if e.bounded else _rules.WARN
            out.append(_rules.Finding(
                "latency", "{}.q{:g}".format(name, q * 100), status, str(e)))
    return _finish(out, args)


# ---------------------------------------------------------------------- closure
def cmd_closure(args):
    run = _load(args.file, args)
    v = _cl.classify(run)
    status = {_cl.CLOSED_LOOP: _rules.VIOLATION, _cl.OPEN_LOOP: _rules.OK,
              _cl.INCONCLUSIVE: _rules.UNKNOWN,
              _cl.UNKNOWN: _rules.UNKNOWN}[v.kind]
    out = [_header(run), _rules.Finding("closure", v.kind, status, v.reason)]
    if v.occ:
        out.append(_rules.Finding(
            "closure", "occupancy", _rules.INFO,
            "max in flight {}, mean {:.2f}, at maximum {:.1%} of wall time"
            .format(v.occ.max_in_flight, v.occ.mean_in_flight, v.occ.at_max_fraction)))
    if v.score is not None:
        out.append(_rules.Finding(
            "closure", "coincidence", _rules.INFO,
            "sends land a median of {:.4f} inter-send gaps after a completion "
            "(closed below {}, open above {})"
            .format(v.score, _cl.CLOSED_MAX_SCORE, _cl.OPEN_MIN_SCORE)))
    return _finish(out, args)


# -------------------------------------------------------------------------- slo
def cmd_slo(args):
    run = _load(args.file, args)
    target = _target(args)
    out = [_header(run)]
    if not target.any_set():
        out.append(_rules.Finding(
            "slo", "target", _rules.UNKNOWN,
            "no deadline given; pass --ttft-ms / --tpot-ms / --e2e-ms. Without one "
            "there is throughput at an unstated latency, which is not a result"))
        return _finish(out, args)
    out.append(_rules.Finding("slo", "target", _rules.INFO, target.describe()))
    att = _slo.attainment(run, target)
    if att is None or att.fraction is None:
        out.append(_rules.Finding("slo", "attainment", _rules.UNKNOWN,
                                  "this file does not carry the metrics the SLO names"))
        return _finish(out, args)
    out.append(_rules.Finding("slo", "attainment", _slo.status(att, target), att.note))
    corrected = _slo.attainment(run, target, use_corrected=True)
    if corrected and corrected.fraction is not None \
            and abs(corrected.fraction - att.fraction) > 1e-12:
        out.append(_rules.Finding(
            "slo", "attainment.from_due", _slo.status(corrected, target),
            "measured from the time each request was due instead of when the harness "
            "managed to send it: {:.4g}% attainment, goodput {:.2f} req/s"
            .format(corrected.fraction * 100, corrected.goodput)))
    for name, m, qv, limit, _e in _slo.mean_would_have_passed(run, target) or []:
        out.append(_rules.Finding(
            "slo", "mean." + name, _rules.WARN,
            "mean {:.1f} ms passes the {:g} ms target; q{:.4g} is {:.1f} ms and does "
            "not".format(m, limit, target.attainment * 100, qv)))
    return _finish(out, args)


# --------------------------------------------------------------------- queueing
def cmd_queueing(args):
    run = _load(args.file, args)
    out = [_header(run)]
    qq = _q.analyse(run, args.capacity)
    if qq is None:
        out.append(_rules.Finding("queueing", "all", _rules.UNKNOWN,
                                  "needs per-request latencies"))
        return _finish(out, args)
    out.append(_rules.Finding("queueing", "observed", _rules.INFO, qq.note))
    for rho, mm1, md1 in _q.tradeoff_table(qq.service_s):
        out.append(_rules.Finding(
            "queueing", "rho={:.2f}".format(rho), _rules.INFO,
            "M/M/1 wait {:.1f} ms ({:.1f}x service), M/D/1 floor {:.1f} ms"
            .format(mm1 * 1e3, mm1 / qq.service_s, md1 * 1e3)))
    if qq.triangle:
        out.append(_rules.Finding("queueing", "consistency", _rules.WARN, qq.triangle))
    return _finish(out, args)


# ------------------------------------------------------------------------ sweep
def cmd_sweep(args):
    target = _target(args)
    runs = []
    for path in args.files:
        # The label becomes the {location} field, which must not contain a colon or
        # whitespace or the one-line format stops being parseable. A full path on
        # Windows contains both -- a drive letter carries a colon, and run
        # directories acquire spaces -- so the basename is used, with any remaining
        # colon replaced. telemetrydoctor hit the same thing with a phase label of
        # `active[36:54]`.
        label = os.path.basename(path).replace(":", "-").replace(" ", "_")
        runs.append((label, _load(path, args)))
    rungs, best_t, best_g = _slo.sweep(runs, target)
    out = []
    for r in rungs:
        out.append(_rules.Finding(
            "sweep", r.label, _rules.INFO,
            "{:.2f} req/s offered, throughput {:.2f}, goodput {}, attainment {}, "
            "q99 {}".format(
                r.rate or 0.0, r.throughput or 0.0,
                "{:.2f}".format(r.goodput) if r.goodput is not None else "n/a",
                "{:.4g}%".format(r.attainment * 100)
                if r.attainment is not None else "n/a",
                "{:.4f}s".format(r.p99_s) if r.p99_s is not None else "n/a")))
    if best_t is None or best_g is None:
        out.append(_rules.Finding("sweep", "knee", _rules.UNKNOWN,
                                  "not enough rungs carry both figures"))
        return _finish(out, args)
    if best_t == best_g:
        out.append(_rules.Finding(
            "sweep", "knee", _rules.OK,
            "throughput and goodput peak on the same rung ({}), so the ladder did not "
            "reach saturation -- extend it upward before calling this the capacity"
            .format(rungs[best_t].label)))
    else:
        out.append(_rules.Finding(
            "sweep", "knee", _rules.VIOLATION,
            "throughput peaks at {} but goodput peaks at {}. Every rung between them "
            "serves more requests and fewer useful ones; a throughput-only report "
            "recommends the first, and users experience the difference."
            .format(rungs[best_t].label, rungs[best_g].label)))
    return _finish(out, args)


# ---------------------------------------------------------------------- compare
def cmd_compare(args):
    a = _load(args.a, args)
    b = _load(args.b, args)
    c = _cmp.compare(a, b, args.q, args.conf)
    out = []
    for g in c.gates:
        out.append(_rules.Finding(
            "compare", "gate." + g.name.replace(" ", "_"),
            _rules.OK if g.passed else _rules.VIOLATION, g.detail))
    status = {_cmp.DIFFERS: _rules.WARN, _cmp.INCONCLUSIVE: _rules.INFO,
              _cmp.BLOCKED: _rules.VIOLATION}[c.verdict]
    out.append(_rules.Finding("compare", c.verdict, status, c.note))
    return _finish(out, args)


# ------------------------------------------------------------------------ rules
def cmd_rules(args):
    _emit(_rules.describe_rules(), args.json)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="servedoctor",
        description="Audit an inference-serving benchmark for the mistakes that make "
                    "it a measurement of the load generator: closed-loop tails, "
                    "coordinated omission, quantiles with no sample behind them, "
                    "censored failures, token rates of two different units, and "
                    "throughput reported where goodput was meant.")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, files=1):
        if files == 1:
            sp.add_argument("file", help="per-request CSV or summary JSON")
        elif files == 2:
            sp.add_argument("a", help="baseline run")
            sp.add_argument("b", help="candidate run")
        elif files < 0:
            sp.add_argument("files", nargs="+", help="one file per rung of the ladder")
        if files != 0:
            sp.add_argument("--format", default="auto",
                            choices=("auto", "requests", "summary"))
        sp.add_argument("--q", type=float, default=0.99, metavar="Q",
                        help="quantile to audit, as a fraction (default 0.99)")
        sp.add_argument("--conf", type=float, default=_p.DEFAULT_CONF,
                        metavar="C", help="confidence for quantile intervals "
                                          "(default 0.90)")
        sp.add_argument("--ttft-ms", type=float, default=None)
        sp.add_argument("--tpot-ms", type=float, default=None)
        sp.add_argument("--e2e-ms", type=float, default=None)
        sp.add_argument("--attainment", type=float,
                        default=_slo.DEFAULT_TARGET_ATTAINMENT,
                        help="share of requests the SLO must hold for (default 0.99)")
        sp.add_argument("--concurrency", type=float, default=None,
                        help="concurrency the harness was configured for; this is "
                             "what makes Little's Law a check instead of an identity")
        sp.add_argument("--rate", type=float, default=None, metavar="RPS",
                        help="request rate the harness was asked for, used to bound "
                             "how many requests it never sent")
        sp.add_argument("--capacity", type=float, default=None, metavar="RPS",
                        help="saturated throughput of the deployment, if known; "
                             "without it utilisation is estimated as lambda*E[S]")
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--fail-on", default=None,
                        choices=("info", "unknown", "warn", "violation", "any"),
                        help="exit 1 when any finding reaches this status or worse")

    sp = sub.add_parser("audit", help="run the fourteen rules")
    common(sp)
    sp.add_argument("--rule", action="append", metavar="SDNNN",
                    help="run only this rule; repeatable")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("latency", help="quantiles with exact confidence intervals")
    common(sp)
    sp.set_defaults(func=cmd_latency)

    sp = sub.add_parser("closure", help="open loop or closed loop, and how it was told")
    common(sp)
    sp.set_defaults(func=cmd_closure)

    sp = sub.add_parser("slo", help="attainment and goodput against a deadline")
    common(sp)
    sp.set_defaults(func=cmd_slo)

    sp = sub.add_parser("queueing", help="utilisation against the delay it implies")
    common(sp)
    sp.set_defaults(func=cmd_queueing)

    sp = sub.add_parser("sweep", help="a rate ladder, and where goodput turns over")
    common(sp, files=-1)
    sp.set_defaults(func=cmd_sweep)

    sp = sub.add_parser("compare", help="two runs: comparable at all, then different")
    common(sp, files=2)
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("rules", help="what each rule checks")
    common(sp, files=0)
    sp.set_defaults(func=cmd_rules)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 2
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print("input: [error] {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
