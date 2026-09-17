"""Two units called "token", and the TPOT denominator."""
import pytest
from conftest import req, run_of

from servedoctor import tokens as T


# ------------------------------------------------------------- the TPOT denominator
@pytest.mark.parametrize("m,err", [(2, 1.0), (4, 1.0 / 3.0), (11, 0.1),
                                   (101, 0.01), (1001, 0.001)])
def test_the_denominator_error_is_exactly_m_over_m_minus_one(m, err):
    assert T.denominator_error(m) == pytest.approx(err)


def test_the_denominator_error_is_undefined_below_two_tokens():
    assert T.denominator_error(1) is None
    assert T.denominator_error(0) is None
    assert T.denominator_error(None) is None


def test_a_short_answer_run_shows_a_third(short_answers_run):
    v = T.analyse(short_answers_run)
    assert v.median_output == 4
    assert v.tpot_error == pytest.approx(1.0 / 3.0, rel=1e-3)


def test_a_long_answer_run_shows_a_rounding_difference(open_run):
    v = T.analyse(open_run)
    assert v.median_output > 100
    assert v.tpot_error < 0.01


def test_one_token_answers_are_excluded_not_counted_as_zero(short_answers_run):
    v = T.analyse(short_answers_run)
    assert v.n_single > 0
    assert "undefined" in v.tpot_note
    assert v.tpot_median > 0


def test_tpot_median_uses_only_requests_that_have_one():
    reqs = [req(0, 0.0, 2.0, ttft=1.0, tokens_out=11),
            req(1, 1.0, 2.0, ttft=1.0, tokens_out=1)]
    v = T.analyse(run_of(reqs))
    assert v.tpot_median == pytest.approx(0.1)
    assert v.n_single == 1


# --------------------------------------------------------------- token composition
def test_the_two_rates_and_their_ratio(short_answers_run):
    v = T.analyse(short_answers_run)
    assert v.total_tps > 100 * v.output_tps
    assert v.prefill_share > 0.99


def test_a_balanced_run_is_not_prefill_dominated(open_run):
    v = T.analyse(open_run)
    assert 0.7 < v.prefill_share < 0.9
    assert v.total_tps / v.output_tps == pytest.approx(5.0, abs=0.2)


def test_without_a_prompt_column_the_mix_is_unrecorded():
    reqs = [req(i, float(i), 1.0, tokens_in=None, tokens_out=10) for i in range(20)]
    v = T.analyse(run_of(reqs))
    assert v.prefill_share is None
    assert v.total_tps is None
    assert "not comparable" in v.rate_note


def test_the_two_notes_are_separate_so_two_rules_can_use_them(open_run):
    v = T.analyse(open_run)
    assert "tok/s" in v.rate_note
    assert "tok/s" not in v.tpot_note
    assert "TPOT" in v.tpot_note


def test_no_output_tokens_at_all_returns_nothing():
    reqs = [req(i, float(i), 1.0, tokens_out=None) for i in range(20)]
    assert T.analyse(run_of(reqs)) is None


# ------------------------------------------------------------------- summary view
def test_summary_view_reports_both_rates_when_both_are_present(summary_run):
    s = T.summary_view(summary_run)
    assert "decode" in s and "total" in s


def test_summary_view_reconstructs_end_to_end_to_reveal_the_denominator(summary_run):
    s = T.summary_view(summary_run)
    assert "reconstructing mean end-to-end" in s
    # the fixture was built with the m-1 denominator, so that reconstruction wins
    assert "(-0.1%)" in s or "(+0.0%)" in s or "(-0.0%)" in s


def test_summary_view_says_when_only_a_total_rate_exists(tmp_path):
    import json
    import os

    from servedoctor import parse
    p = os.path.join(str(tmp_path), "s.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"total_token_throughput": 5000.0, "completed": 10}, fh)
    s = T.summary_view(parse.load(p))
    assert "not recoverable" in s


def test_summary_view_on_a_per_request_run_is_none(open_run):
    assert T.summary_view(open_run) is None
