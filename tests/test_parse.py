"""Reading both shapes of input, and recording which one was read."""
import json
import os

import pytest
from conftest import fx, req, run_of

from servedoctor import parse


def write(tmp_path, name, text):
    p = os.path.join(str(tmp_path), name)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return p


# ------------------------------------------------------------------- basic shape
def test_reads_the_canonical_per_request_csv(open_run):
    assert open_run.evidence == parse.PER_REQUEST
    assert open_run.n == 2000
    assert open_run.source == "per_request_csv"


def test_declaration_lines_are_not_data(open_run):
    # every fixture opens with several '#' lines; none of them became a request
    assert all(r.start_s is not None for r in open_run.requests)
    assert open_run.requests[0].rid == "0"


def test_requests_come_back_sorted_by_send_time(open_run):
    starts = [r.start_s for r in open_run.requests]
    assert starts == sorted(starts)


def test_a_closed_loop_file_has_no_arrival_column(closed_run):
    assert closed_run.has_field("start_s")
    assert not closed_run.has_field("arrival_s")


def test_missing_send_time_column_is_a_hard_error(tmp_path):
    p = write(tmp_path, "bad.csv", "request_id,end_s\n1,2.0\n")
    with pytest.raises(ValueError) as exc:
        parse.load(p)
    assert "send-time" in str(exc.value)


def test_empty_file_is_a_hard_error(tmp_path):
    p = write(tmp_path, "empty.csv", "# only a declaration\n")
    with pytest.raises(ValueError):
        parse.load(p)


# ----------------------------------------------------------------------- aliases
@pytest.mark.parametrize("start_name", ["start_s", "start", "send_s", "t_start"])
def test_send_time_aliases(tmp_path, start_name):
    p = write(tmp_path, "a.csv",
              "id,{},end_s,output_tokens\n1,0.0,1.5,10\n2,1.0,2.0,10\n"
              .format(start_name))
    run = parse.load(p)
    assert run.n == 2
    assert run.requests[0].latency_s() == 1.5


@pytest.mark.parametrize("out_name", ["output_tokens", "completion_tokens",
                                      "generated_tokens", "n_output"])
def test_output_token_aliases(tmp_path, out_name):
    p = write(tmp_path, "a.csv",
              "start_s,end_s,{}\n0.0,1.0,7\n".format(out_name))
    run = parse.load(p)
    assert run.requests[0].output_tokens == 7


def test_duration_columns_reconstruct_absolute_timestamps(tmp_path):
    p = write(tmp_path, "d.csv",
              "start_s,ttft_ms,e2e_ms,output_tokens\n10.0,250,2250,101\n")
    run = parse.load(p)
    r = run.requests[0]
    assert abs(r.first_token_s - 10.25) < 1e-9
    assert abs(r.end_s - 12.25) < 1e-9
    assert set(run.reconstructed) == {"first_token_s", "end_s"}


def test_reconstruction_is_recorded_so_it_can_be_reported(tmp_path):
    p = write(tmp_path, "d.csv", "start_s,latency_ms\n0.0,1000\n")
    run = parse.load(p)
    assert "end_s" in run.reconstructed
    assert "first_token_s" not in run.reconstructed


def test_unclaimed_columns_are_kept_not_dropped(tmp_path):
    p = write(tmp_path, "x.csv",
              "start_s,end_s,gpu_id,tenant\n0.0,1.0,3,acme\n")
    run = parse.load(p)
    assert run.extra_columns == ["gpu_id", "tenant"]


def test_na_cells_are_skipped_rather_than_read_as_zero(tmp_path):
    p = write(tmp_path, "n.csv",
              "start_s,first_token_s,end_s,output_tokens\n"
              "0.0,N/A,1.0,10\n1.0,1.1,2.0,10\n")
    run = parse.load(p)
    assert run.requests[0].first_token_s is None
    assert run.requests[0].ttft_s() is None
    assert run.values(lambda r: r.ttft_s()) == [pytest.approx(0.1)]


def test_failure_flags_in_several_spellings(tmp_path):
    p = write(tmp_path, "f.csv",
              "start_s,end_s,ok\n0.0,1.0,true\n1.0,2.0,0\n2.0,3.0,failed\n")
    run = parse.load(p)
    assert [r.ok for r in run.requests] == [True, False, False]


def test_a_row_without_a_send_time_is_skipped_not_guessed(tmp_path):
    p = write(tmp_path, "s.csv", "start_s,end_s\n0.0,1.0\n,2.0\n2.0,3.0\n")
    run = parse.load(p)
    assert run.n == 2


# ------------------------------------------------------------ derived quantities
def test_the_two_latencies_differ_only_when_the_harness_was_late():
    on_time = req(1, start=5.0, latency=1.0, arrival=5.0)
    late = req(2, start=7.0, latency=1.0, arrival=5.0)
    assert on_time.latency_s() == on_time.corrected_latency_s() == 1.0
    assert late.latency_s() == 1.0
    assert late.corrected_latency_s() == 3.0
    assert late.queue_wait_s() == 2.0


def test_tpot_uses_m_minus_one():
    r = req(1, start=0.0, latency=2.0, ttft=1.0, tokens_out=11)
    # decode took 1.0s and produced 10 tokens after the first
    assert r.decode_s() == pytest.approx(1.0)
    assert r.tpot_s() == pytest.approx(0.1)


def test_tpot_is_undefined_for_a_one_token_answer_rather_than_zero():
    r = req(1, start=0.0, latency=2.0, ttft=1.0, tokens_out=1)
    assert r.tpot_s() is None
    r0 = req(2, start=0.0, latency=2.0, ttft=1.0, tokens_out=0)
    assert r0.tpot_s() is None


def test_span_is_last_completion_minus_first_send():
    run = run_of([req(1, 10.0, 1.0), req(2, 12.0, 3.0)])
    assert run.span_s() == pytest.approx(5.0)


def test_values_skips_none_and_failures():
    run = run_of([req(1, 0.0, 1.0), req(2, 1.0, None), req(3, 2.0, 1.0, ok=False)])
    assert run.values(lambda r: r.latency_s()) == [pytest.approx(1.0)]


# --------------------------------------------------------------- summary reports
def test_reads_a_summary_report(summary_run):
    assert summary_run.evidence == parse.SUMMARY_ONLY
    assert summary_run.n == 0
    assert summary_run.summary.has("completed", "duration_s", "mean_ttft_ms")


def test_summary_keeps_millisecond_names_as_milliseconds(summary_run):
    # converting here would hide the unit from every message downstream
    assert summary_run.summary.get("mean_ttft_ms") > 1.0


def test_summary_records_fields_it_did_not_recognise(summary_run):
    assert "_comment" in summary_run.summary.unknown_fields


def test_summary_aliases(tmp_path):
    p = write(tmp_path, "s.json", json.dumps(
        {"rps": 12.5, "avg_latency_ms": 800.0, "num_workers": 10}))
    run = parse.load(p)
    assert run.summary.get("request_throughput") == 12.5
    assert run.summary.get("mean_e2el_ms") == 800.0
    assert run.declared_concurrency == 10


def test_nested_summary_is_flattened(tmp_path):
    p = write(tmp_path, "s.json", json.dumps(
        {"metrics": {"output_throughput": 900.0}, "completed": 10}))
    run = parse.load(p)
    assert run.summary.get("output_throughput") == 900.0


def test_a_json_with_nothing_recognisable_is_an_error(tmp_path):
    p = write(tmp_path, "s.json", json.dumps({"hello": "world"}))
    with pytest.raises(ValueError) as exc:
        parse.load(p)
    assert "recognised" in str(exc.value)


def test_format_is_sniffed_from_the_extension():
    assert parse.load(fx("EXAMPLE_summary.json")).evidence == parse.SUMMARY_ONLY
    assert parse.load(fx("EXAMPLE_short_200.csv")).evidence == parse.PER_REQUEST


def test_an_unknown_explicit_format_is_refused():
    with pytest.raises(ValueError):
        parse.load(fx("EXAMPLE_short_200.csv"), fmt="parquet")
