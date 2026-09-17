"""The command line: output shape, exit codes, and the threshold semantics."""
import json
import re

import pytest
from conftest import fx

from servedoctor import cli

# {location}: [{status}] {message} -- the same line format all seven tools use.
LINE = re.compile(r"^[^:\s]+: \[(ok|info|warn|violation|unknown|error)\] .+$")


def run(capsys, argv):
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, [ln for ln in out.out.splitlines() if ln.strip()], out.err


@pytest.mark.parametrize("cmd", ["audit", "latency", "closure", "slo", "queueing"])
def test_every_line_of_every_subcommand_matches_the_format(capsys, cmd):
    _code, lines, _err = run(capsys, [cmd, fx("EXAMPLE_open_loop.csv"),
                                      "--ttft-ms", "500"])
    assert lines
    for ln in lines:
        assert LINE.match(ln), ln


def test_rules_lists_all_fourteen(capsys):
    code, lines, _err = run(capsys, ["rules"])
    assert code == 0
    assert len(lines) == 14
    assert all(LINE.match(ln) for ln in lines)


def test_compare_matches_the_format(capsys):
    _code, lines, _err = run(capsys, ["compare", fx("EXAMPLE_ladder_40.csv"),
                                      fx("EXAMPLE_ladder_70.csv")])
    assert all(LINE.match(ln) for ln in lines)


def test_sweep_matches_the_format(capsys):
    _code, lines, _err = run(capsys, [
        "sweep", fx("EXAMPLE_ladder_40.csv"), fx("EXAMPLE_ladder_70.csv"),
        fx("EXAMPLE_ladder_95.csv"), fx("EXAMPLE_ladder_114.csv"),
        "--e2e-ms", "3000"])
    assert len(lines) == 5
    assert all(LINE.match(ln) for ln in lines)


# ------------------------------------------------------------------------- json
@pytest.mark.parametrize("cmd", ["audit", "latency", "closure", "rules"])
def test_json_is_parseable_and_carries_the_same_fields(capsys, cmd):
    argv = [cmd] + ([] if cmd == "rules" else [fx("EXAMPLE_open_loop.csv")])
    _code, lines, _err = run(capsys, argv + ["--json"])
    payload = json.loads("\n".join(lines))
    assert isinstance(payload, list) and payload
    for item in payload:
        assert set(item) == {"rule", "location", "status", "message"}


# ------------------------------------------------------------------ exit codes
def test_a_clean_gate_exits_zero(capsys):
    code, _lines, _err = run(capsys, ["audit", fx("EXAMPLE_open_loop.csv"),
                                      "--rule", "SD004", "--fail-on", "violation"])
    assert code == 0


def test_a_tripped_gate_exits_one(capsys):
    code, _lines, _err = run(capsys, ["audit", fx("EXAMPLE_short_200.csv"),
                                      "--rule", "SD004", "--fail-on", "violation"])
    assert code == 1


def test_fail_on_is_a_threshold_not_an_equality(capsys):
    """--fail-on warn must also trip on violation, as in the other six tools."""
    code, _lines, _err = run(capsys, ["audit", fx("EXAMPLE_short_200.csv"),
                                      "--rule", "SD004", "--fail-on", "warn"])
    assert code == 1


def test_fail_on_unknown_catches_a_summary_report(capsys):
    """A benchmark that cannot be checked has not passed a check."""
    code, _lines, _err = run(capsys, ["audit", fx("EXAMPLE_summary.json"),
                                      "--fail-on", "unknown"])
    assert code == 1


def test_fail_on_any_is_the_loudest_setting(capsys):
    code, _lines, _err = run(capsys, ["audit", fx("EXAMPLE_open_loop.csv"),
                                      "--fail-on", "any"])
    assert code == 1


def test_without_fail_on_a_violation_still_exits_zero(capsys):
    code, lines, _err = run(capsys, ["audit", fx("EXAMPLE_closed_loop.csv")])
    assert code == 0
    assert any("[violation]" in ln for ln in lines)


def test_an_unreadable_input_exits_two(capsys):
    code, _lines, err = run(capsys, ["audit", fx("does_not_exist.csv")])
    assert code == 2
    assert "[error]" in err


def test_a_file_with_no_send_time_exits_two(capsys, tmp_path):
    import os
    p = os.path.join(str(tmp_path), "bad.csv")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("request_id,end_s\n1,2.0\n")
    code, _lines, err = run(capsys, ["audit", p])
    assert code == 2
    assert "send-time" in err


def test_no_subcommand_prints_help_and_exits_two(capsys):
    assert cli.main([]) == 2


# --------------------------------------------------------------------- contents
def test_audit_leads_with_the_source_line(capsys):
    _code, lines, _err = run(capsys, ["audit", fx("EXAMPLE_open_loop.csv")])
    assert lines[0].startswith("source: [info]")
    assert "2000 requests" in lines[0]


def test_latency_reports_both_latencies_when_a_schedule_exists(capsys):
    _code, lines, _err = run(capsys, ["latency", fx("EXAMPLE_open_loop_backlog.csv")])
    joined = "\n".join(lines)
    assert "e2e.q99" in joined
    assert "e2e_from_due.q99" in joined


def test_latency_marks_the_quantiles_it_cannot_bound(capsys):
    _code, lines, _err = run(capsys, ["latency", fx("EXAMPLE_short_200.csv")])
    assert any("[warn]" in ln and "UNBOUNDED" in ln for ln in lines)


def test_latency_on_a_summary_refuses(capsys):
    _code, lines, _err = run(capsys, ["latency", fx("EXAMPLE_summary.json")])
    assert any("[unknown]" in ln for ln in lines)


def test_slo_without_a_target_says_so(capsys):
    _code, lines, _err = run(capsys, ["slo", fx("EXAMPLE_open_loop.csv")])
    assert any("no deadline given" in ln for ln in lines)


def test_slo_reports_the_corrected_attainment_when_it_differs(capsys):
    _code, lines, _err = run(capsys, ["slo", fx("EXAMPLE_open_loop_backlog.csv"),
                                      "--e2e-ms", "5000"])
    assert any("attainment.from_due" in ln for ln in lines)


def test_queueing_prints_the_tradeoff_table(capsys):
    _code, lines, _err = run(capsys, ["queueing", fx("EXAMPLE_open_loop.csv")])
    assert any("rho=0.95" in ln for ln in lines)
    assert any("rho=0.50" in ln for ln in lines)


def test_sweep_names_the_knee(capsys):
    _code, lines, _err = run(capsys, [
        "sweep", fx("EXAMPLE_ladder_40.csv"), fx("EXAMPLE_ladder_70.csv"),
        fx("EXAMPLE_ladder_95.csv"), fx("EXAMPLE_ladder_114.csv"),
        "--e2e-ms", "3000"])
    knee = [ln for ln in lines if ln.startswith("knee:")]
    assert len(knee) == 1
    assert "[violation]" in knee[0]
    assert "throughput peaks at" in knee[0]


def test_compare_blocks_a_closed_versus_open_comparison(capsys):
    _code, lines, _err = run(capsys, ["compare", fx("EXAMPLE_closed_loop.csv"),
                                      fx("EXAMPLE_open_loop.csv")])
    assert any("gate.load_shape" in ln and "[violation]" in ln for ln in lines)
    assert any(ln.startswith("blocked:") for ln in lines)


def test_the_q_flag_changes_which_quantile_is_audited(capsys):
    _code, lines, _err = run(capsys, ["audit", fx("EXAMPLE_short_200.csv"),
                                      "--rule", "SD004", "--q", "0.9"])
    assert any(ln.startswith("q90:") for ln in lines)
    assert all("[violation]" not in ln for ln in lines[1:])
