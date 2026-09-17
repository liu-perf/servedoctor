"""The fourteen rules, each tested on both sides.

A rule that only ever fires is as useless as one that never does, so every rule
below has a case that trips it and a case that does not. The last test runs all
fourteen against every fixture and asserts none of them raises or returns None --
a rule that crashes on an unusual file is a rule that silently stops auditing.
"""
import pytest
from conftest import fx, open_loop_run, req, run_of

from servedoctor import parse
from servedoctor import rules as R
from servedoctor import slo as S

ALL_FIXTURES = [
    "EXAMPLE_closed_loop.csv", "EXAMPLE_open_loop.csv",
    "EXAMPLE_open_loop_backlog.csv", "EXAMPLE_short_200.csv",
    "EXAMPLE_censored.csv", "EXAMPLE_short_answers.csv",
    "EXAMPLE_cold_start.csv", "EXAMPLE_ladder_40.csv", "EXAMPLE_ladder_70.csv",
    "EXAMPLE_ladder_95.csv", "EXAMPLE_ladder_114.csv", "EXAMPLE_summary.json",
]


def only(run, rule, **kw):
    findings, _ctx = R.audit(run, only=[rule], **kw)
    return findings[0]


# --------------------------------------------------------------------- severity
def test_unknown_sits_above_info_and_below_warn():
    assert R.SEVERITY[R.INFO] < R.SEVERITY[R.UNKNOWN] < R.SEVERITY[R.WARN]
    assert R.SEVERITY[R.WARN] < R.SEVERITY[R.VIOLATION]


def test_worst_status_picks_the_worst():
    fs = [R.Finding("a", "a", R.OK, ""), R.Finding("b", "b", R.WARN, ""),
          R.Finding("c", "c", R.UNKNOWN, "")]
    assert R.worst_status(fs) == R.WARN
    assert R.worst_status([]) == R.OK


def test_a_finding_prints_in_the_shape_the_whole_series_uses():
    assert str(R.Finding("SD001", "evidence", R.OK, "fine")) == \
        "evidence: [ok] fine"


def test_no_location_contains_a_colon(open_run):
    """The output format is {location}: [{status}] {message} and must stay parseable."""
    findings, _ctx = R.audit(open_run)
    assert all(":" not in f.location for f in findings)


# ------------------------------------------------------------------ rule by rule
def test_sd001_names_the_evidence_level(open_run, summary_run):
    assert only(open_run, "SD001").status == R.INFO
    assert only(summary_run, "SD001").status == R.UNKNOWN


def test_sd002_both_ways(open_run, closed_run):
    assert only(open_run, "SD002").status == R.OK
    assert only(closed_run, "SD002").status == R.VIOLATION


def test_sd003_both_ways(open_run, backlog_run, closed_run):
    assert only(open_run, "SD003").status == R.OK
    assert only(backlog_run, "SD003").status == R.VIOLATION
    assert only(closed_run, "SD003").status == R.UNKNOWN


def test_sd003_bounds_the_unsent_requests_when_a_rate_is_declared(closed_run):
    f = only(closed_run, "SD003", declared_rate=6.4)
    assert "never sent" in f.message


def test_sd004_both_ways(open_run, short_run):
    assert only(open_run, "SD004").status == R.OK
    bad = only(short_run, "SD004")
    assert bad.status == R.VIOLATION
    assert "n>=299" in bad.message


def test_sd004_suggests_the_quantile_the_sample_can_carry(short_run):
    assert "q98.51" in only(short_run, "SD004").message


def test_sd005_both_ways(open_run, cold_start_run):
    assert only(open_run, "SD005").status == R.OK
    assert only(cold_start_run, "SD005").status == R.WARN


def test_sd006_both_ways(open_run):
    assert only(open_run, "SD006").status == R.OK
    reqs = [req(i, float(i), 0.5) for i in range(40)]      # never two at once
    assert only(run_of(reqs), "SD006").status == R.UNKNOWN


def test_sd007_both_ways(open_run, censored_run):
    assert only(open_run, "SD007").status == R.OK
    assert only(censored_run, "SD007").status == R.VIOLATION


def test_sd008_both_ways(open_run, short_answers_run):
    assert only(open_run, "SD008").status == R.OK
    f = only(short_answers_run, "SD008")
    assert f.status == R.WARN
    assert "33" in f.message


def test_sd009_both_ways(short_answers_run):
    assert only(short_answers_run, "SD009").status == R.WARN
    reqs = [req(i, float(i), 1.0, tokens_in=10, tokens_out=200) for i in range(50)]
    assert only(run_of(reqs), "SD009").status == R.OK


def test_sd010_identity_versus_check(open_run):
    assert only(open_run, "SD010").status == R.INFO
    assert only(open_run, "SD010", declared_concurrency=7).status == R.VIOLATION


def test_sd011_covers_all_four_arrival_shapes(open_run, closed_run):
    assert only(open_run, "SD011").status == R.OK
    assert only(closed_run, "SD011").status == R.WARN
    # The service time has to vary, or the run is genuinely indistinguishable from
    # a closed loop: with a constant 0.5s service and a 0.25s period, every send
    # lands exactly on a completion, which is the defining property of a closed
    # loop and not something a detector should be asked to see through.
    fixed = run_of([req(i, i * 0.25, 0.30 + 0.4 * ((i * 37) % 10) / 10.0,
                        arrival=i * 0.25) for i in range(200)])
    assert only(fixed, "SD011").status == R.INFO


def test_a_perfectly_periodic_schedule_is_indistinguishable_from_a_closed_loop():
    """Not a defect: with constant service and a matching period they are the same.

    Sends at every 0.25s with a constant 0.5s service means each send coincides
    exactly with a completion two back. No statistic can separate that from a
    two-worker closed loop, and the tool reports what the file supports.
    """
    from servedoctor import closure as C
    run = run_of([req(i, i * 0.25, 0.5, arrival=i * 0.25) for i in range(200)])
    assert C.coincidence(run) == pytest.approx(0.0, abs=1e-9)


def test_sd012_reports_and_can_warn(open_run):
    assert only(open_run, "SD012").status == R.INFO
    tight = open_loop_run(n=600, rate=6.0, latency=1.0, jitter=0.05)
    assert only(tight, "SD012", capacity_rps=6.3).status == R.WARN


def test_sd013_needs_a_target_and_then_scores_it(open_run):
    assert only(open_run, "SD013").status == R.UNKNOWN
    findings, _ctx = R.audit(open_run, S.Target(e2e_ms=60000), only=["SD013"])
    assert findings[0].status == R.OK
    findings, _ctx = R.audit(open_run, S.Target(e2e_ms=500), only=["SD013"])
    assert findings[0].status == R.VIOLATION


def test_sd013_names_the_mean_that_would_have_passed():
    # 394 fast and 6 slow: mean 79 ms passes a 200 ms target, and the 99th
    # percentile is order statistic 396, which is one of the slow ones.
    reqs = [req(i, float(i), 1.0, ttft=0.05 if i < 394 else 2.0) for i in range(400)]
    findings, _ctx = R.audit(run_of(reqs), S.Target(ttft_ms=200), only=["SD013"])
    assert "An SLO stated as a mean is not an SLO" in findings[0].message


def test_sd014_both_ways(open_run, censored_run):
    assert only(open_run, "SD014").status == R.OK
    assert only(censored_run, "SD014").status == R.WARN


def test_sd014_notices_reconstructed_columns(tmp_path):
    import os
    p = os.path.join(str(tmp_path), "d.csv")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("start_s,ttft_ms,e2e_ms,input_tokens,output_tokens\n")
        for i in range(50):
            fh.write("{},100,1000,100,50\n".format(i * 0.2))
    f = only(parse.load(p), "SD014")
    assert f.status == R.WARN
    assert "reconstructed" in f.message


# ------------------------------------------------------------------- the whole set
def test_audit_returns_one_finding_per_rule(open_run):
    findings, _ctx = R.audit(open_run)
    assert len(findings) == len(R.RULES) == 14
    assert [f.rule for f in findings] == [name for name, _fn in R.RULES]


def test_every_rule_survives_every_fixture():
    for name in ALL_FIXTURES:
        run = parse.load(fx(name))
        findings, _ctx = R.audit(run, S.Target(ttft_ms=500, tpot_ms=50))
        assert len(findings) == 14, name
        for f in findings:
            assert f is not None and f.message, (name, f.rule)
            assert f.status in R.SEVERITY, (name, f.rule, f.status)


def test_a_summary_report_returns_unknown_for_the_per_request_rules(summary_run):
    findings, _ctx = R.audit(summary_run)
    unknowns = [f.rule for f in findings if f.status == R.UNKNOWN]
    assert len(unknowns) >= 9
    assert "SD004" in unknowns and "SD002" in unknowns


def test_selecting_rules_is_case_insensitive(open_run):
    findings, _ctx = R.audit(open_run, only=["sd001"])
    assert len(findings) == 1 and findings[0].rule == "SD001"


def test_describe_rules_covers_every_rule():
    described = R.describe_rules()
    assert len(described) == len(R.RULES)
    assert all(len(f.message) > 40 for f in described)


def test_context_is_computed_once_and_shared(open_run):
    _findings, ctx = R.audit(open_run)
    assert ctx.closure is not None
    assert ctx.latency is not None
    assert ctx.tokens is not None


def test_an_empty_run_does_not_crash_the_audit():
    findings, _ctx = R.audit(run_of([]))
    assert len(findings) == 14
    assert all(f.status in R.SEVERITY for f in findings)


def test_the_q_parameter_flows_through(open_run):
    findings, ctx = R.audit(open_run, q=0.95)
    assert ctx.q == 0.95
    assert any("q95" in f.location for f in findings)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_worst_status_is_defined_for_every_fixture(name):
    findings, _ctx = R.audit(parse.load(fx(name)))
    assert R.worst_status(findings) in R.SEVERITY
