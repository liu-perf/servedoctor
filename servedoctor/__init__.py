"""servedoctor -- did this benchmark measure the server, or the load generator?

The seventh and last tool in a series about not fooling yourself with a
measurement. `nodebench` measures a node, `benchdoctor` reads the measuring script,
`tracedoctor` reads a kernel trace, `regressiondoctor` compares two runs,
`fitdoctor` says what a number should have been, `telemetrydoctor` says what
arithmetic a monitoring column supports. This one reads the output of a serving
load test and asks the question that comes before every latency number in it: was
the offered load independent of the server's own speed, and does the sample
actually support the quantile printed from it?

    from servedoctor import audit, load, Target

    run = load("run.csv")
    findings, ctx = audit(run, Target(ttft_ms=500, tpot_ms=50))
    for f in findings:
        print(f)
"""
# Every re-exported name here is suffixed or renamed so that none of them collides
# with a submodule of this package. That is not style. `from .omission import analyse
# as omission` binds the *function* to the package attribute `omission`, and the
# next `from . import omission` anywhere in the library then returns the function --
# so `omission.analyse` raises AttributeError from inside a module that imported the
# submodule correctly. telemetrydoctor shipped the same bug with `audit`, which is
# why its rule module is called `rules.py`; this package hit it again with three
# names at once (omission, littles, compare) because the API reads better without
# suffixes. It reads better and does not work.
from .closure import classify as closure_of
from .compare import compare as compare_runs
from .littles import analyse as littles_law
from .omission import analyse as omission_report
from .parse import PER_REQUEST, SUMMARY_ONLY, Request, Run, Summary, load
from .percentile import Estimate, estimate, max_q_for, min_n_for, quantile
from .rules import RULES, Context, Finding, audit, describe_rules, worst_status
from .slo import Target, attainment, sweep
from .window import censoring, steady, warmup

__version__ = "0.1.0"

__all__ = [
    "Context", "Estimate", "Finding", "PER_REQUEST", "RULES", "Request", "Run",
    "SUMMARY_ONLY", "Summary", "Target", "attainment", "audit", "censoring",
    "closure_of", "compare_runs", "describe_rules", "estimate", "littles_law",
    "load", "max_q_for", "min_n_for", "omission_report", "quantile", "steady",
    "sweep", "warmup", "worst_status",
]
