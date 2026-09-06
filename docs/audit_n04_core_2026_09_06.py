"""Codex independent N04 cases; business implementation belongs to ZCode."""
import copy
import hashlib
import importlib
import json
import socket

import pytest
import requests

from tradingagents.agents.report_quality import assess_reports, append_limitation_notice
from tradingagents.run_records import new_run_metadata


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def case(case_id="audit", roles=None, quality="complete", kind="stock"):
    return {
        "case_id": case_id, "ticker": "600519" if kind == "stock" else "000300.SH",
        "trade_date": "2026-01-15", "instrument_type": kind,
        "as_of": "2026-01-15T15:00:00+08:00", "selected_analysts": roles or ["market"],
        "expected_quality_status": quality,
        "allowed_ratings": ["Buy", "Overweight", "Hold", "Underweight", "Sell"],
        "evidence": [
            {"evidence_id": "past", "available_at": "2026-01-15T06:59:59.999999Z", "text": "合成已知证据"},
            {"evidence_id": "equal", "available_at": "2026-01-15T07:00:00Z", "text": "合成边界证据"},
            {"evidence_id": "future", "available_at": "2026-01-15T07:00:00.000001Z", "text": "合成未来证据"},
        ],
    }


def report(c=None):
    c = c or case()
    text = "人工冻结的合成报告；研究信息完整，审计事实甲。" * 20 + "\n|项|值|\n|---|---|\n|一|二|"
    snap = {"selected_analysts": c["selected_analysts"], "deep_think_llm": "audit-model"}
    state = {
        "company_of_interest": c["ticker"], "trade_date": c["trade_date"],
        "instrument_type": c["instrument_type"], "selected_analysts": list(c["selected_analysts"]),
        "market_report": text, "final_trade_decision": "Rating: Buy\n审计事实甲。",
        "run_metadata": new_run_metadata(snap, c["ticker"], c["trade_date"], c["instrument_type"]),
    }
    state["run_metadata"]["run_id"] = "a" * 32
    state["run_metadata"]["created_at"] = "2026-01-15T07:01:00Z"
    state["data_quality"] = assess_reports(state, c["selected_analysts"])
    state["final_trade_decision"] = append_limitation_notice(state["final_trade_decision"], state["data_quality"])
    return state


def suite(cases=None, trials=1):
    return {"schema_version": 1, "suite_id": "codex-audit", "suite_version": "1",
            "title": "独立合成案例", "trials_per_case": trials, "cases": cases or [case()]}


def submission(s, runs=None, label="audit-submission"):
    return {"schema_version": 1, "submission_id": label, "suite_id": s["suite_id"],
            "suite_version": s["suite_version"], "suite_digest": digest(s),
            "runs": runs if runs is not None else [
                {"case_id": s["cases"][0]["case_id"], "trial_id": 1,
                 "status": "completed", "report_path": "report.json"}]}


def checks(grade):
    return {x["check_id"]: x["status"] for x in grade["checks"]}


@pytest.fixture
def api(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Remote I/O is forbidden in independent N04 evaluation")
    monkeypatch.setattr(requests.Session, "request", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    return importlib.import_module("tradingagents.evaluation")


def test_complete_report_passes_without_mutation_and_digest_is_standard(api):
    c = case()
    r = report(c)
    before = copy.deepcopy((c, r))
    g = api.grade_report(c, r)
    assert g["contract_pass"] is True
    assert checks(g)["limitation_notice"] == "not_applicable"
    assert all(v in ("pass", "not_applicable") for v in checks(g).values())
    assert (c, r) == before
    assert api.canonical_digest(r) == digest(r)
    assert api.canonical_digest(dict(reversed(list(r.items())))) == digest(r)
    assert g == api.grade_report(c, r)


def test_frozen_suite_content_change_cannot_reuse_submission(api):
    s = suite()
    sub = submission(s)
    s["cases"][0]["evidence"][0]["text"] += "改变证据"
    with pytest.raises(api.EvaluationInputError):
        api.evaluate_suite(s, sub, report_loader=lambda _: report())


@pytest.mark.parametrize("broken", [float("nan"), float("inf"), -float("inf")])
def test_digest_rejects_nonfinite_json(api, broken):
    with pytest.raises(ValueError):
        api.canonical_digest({"value": broken})


@pytest.mark.parametrize("kind", ["duplicate_case", "bool_trials", "bad_date", "naive_time",
                                 "wrong_local_day", "unknown_role", "duplicate_role", "index_fundamentals",
                                 "unknown_field", "duplicate_evidence", "bad_rating"])
def test_suite_schema_rejects_bad_frozen_cases_before_loader(api, kind):
    s = suite()
    c = s["cases"][0]
    if kind == "duplicate_case":
        s["cases"].append(copy.deepcopy(c))
    elif kind == "bool_trials":
        s["trials_per_case"] = True
    elif kind == "bad_date":
        c["trade_date"] = "2026-02-30"
    elif kind == "naive_time":
        c["as_of"] = "2026-01-15T15:00:00"
    elif kind == "wrong_local_day":
        c["as_of"] = "2026-01-15T23:00:00Z"
    elif kind == "unknown_role":
        c["selected_analysts"] = ["made_up"]
    elif kind == "duplicate_role":
        c["selected_analysts"] = ["market", "market"]
    elif kind == "index_fundamentals":
        c["instrument_type"] = "index"
        c["selected_analysts"] = ["fundamentals"]
    elif kind == "unknown_field":
        c["typo_expected_grade"] = "A"
    elif kind == "duplicate_evidence":
        c["evidence"].append(copy.deepcopy(c["evidence"][0]))
    else:
        c["allowed_ratings"] = ["Strong Buy"]
    calls = []
    with pytest.raises(api.EvaluationInputError):
        api.evaluate_suite(s, submission(s), report_loader=lambda p: calls.append(p))
    assert calls == []


@pytest.mark.parametrize("kind", ["duplicate_trial", "unknown_case", "bool_trial", "out_of_range",
                                 "negative_tokens", "bool_tokens", "infinite_cost", "unpaired_currency"])
def test_submission_schema_rejects_invalid_slots_and_observations(api, kind):
    s = suite()
    sub = submission(s)
    row = sub["runs"][0]
    if kind == "duplicate_trial":
        sub["runs"].append(copy.deepcopy(row))
    elif kind == "unknown_case":
        row["case_id"] = "unknown"
    elif kind == "bool_trial":
        row["trial_id"] = True
    elif kind == "out_of_range":
        row["trial_id"] = 2
    else:
        row["observations"] = {
            "negative_tokens": {"input_tokens": -1},
            "bool_tokens": {"output_tokens": True},
            "infinite_cost": {"cost_amount": float("inf"), "cost_currency": "USD"},
            "unpaired_currency": {"cost_currency": "USD"},
        }[kind]
    with pytest.raises(api.EvaluationInputError):
        api.evaluate_suite(s, sub, report_loader=lambda _: report())


@pytest.mark.parametrize("mutation, failed_check", [
    ("fake_quality", "quality_record"), ("stale_counts", "quality_record"),
    ("bad_meta_hash", "run_metadata"), ("meta_other_type", "identity"),
    ("wrong_team", "team"), ("empty_decision", "decision_present"),
    ("missing_rating", "rating"), ("wrong_date", "identity"),
])
def test_report_claims_are_checked_against_actual_content(api, mutation, failed_check):
    c = case()
    r = report(c)
    if mutation == "fake_quality":
        r["data_quality"]["analysts"][0]["grade"] = "B"
    elif mutation == "stale_counts":
        r["data_quality"]["fail_count"] = 1
    elif mutation == "bad_meta_hash":
        r["run_metadata"]["config_fingerprint"] = "f" * 64
    elif mutation == "meta_other_type":
        r["run_metadata"]["instrument_type"] = "index"
    elif mutation == "wrong_team":
        r["selected_analysts"] = ["news"]
    elif mutation == "empty_decision":
        r["final_trade_decision"] = ""
    elif mutation == "missing_rating":
        r["final_trade_decision"] = "研究结论：审计事实甲。"
    else:
        r["trade_date"] = "2026-01-14"
    g = api.grade_report(c, r)
    assert g["contract_pass"] is False
    assert checks(g)[failed_check] == "fail"


def test_legacy_missing_records_stay_unknown_and_never_pass(api):
    c = case()
    r = report(c)
    for k in ("data_quality", "run_metadata", "selected_analysts", "instrument_type"):
        r.pop(k)
    g = api.grade_report(c, r)
    ck = checks(g)
    assert ck["quality_record"] == ck["run_metadata"] == ck["team"] == "unknown"
    assert not g["contract_pass"]


def test_restricted_report_requires_current_complete_notice(api):
    c = case(roles=["market", "news"], quality="limited")
    r = report(c)
    assert api.grade_report(c, r)["contract_pass"]
    r["final_trade_decision"] = "Rating: Buy\n⚠️ 数据质量限制（报告完整性检查）"
    g = api.grade_report(c, r)
    assert checks(g)["limitation_notice"] == "fail"
    assert not g["contract_pass"]


def test_failed_invalid_and_missing_trials_stay_in_denominators(api):
    s = suite(trials=4)
    rows = [{"case_id": "audit", "trial_id": n, "status": "completed", "report_path": str(n)}
            for n in (1, 2)]
    rows.append({"case_id": "audit", "trial_id": 3, "status": "failed"})
    def loader(p):
        if p == "2":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
        return report()
    result = api.evaluate_suite(s, submission(s, rows), report_loader=loader)
    m = result["metrics"]
    assert {k: m[k] for k in ("expected", "provided", "completed", "failed", "invalid", "missing")} == {
        "expected": 4, "provided": 3, "completed": 1, "failed": 1, "invalid": 1, "missing": 1}
    assert m["completion_rate"] == pytest.approx(0.25)
    assert m["failure_rate"] == pytest.approx(0.75)
    assert m["contract_pass_rate"] == pytest.approx(0.25)
    assert len(result["trials"]) == 4


@pytest.mark.parametrize("bad_report", [[], {"market_report": {"text": "not a report string"}},
                                        {"final_trade_decision": 123}])
def test_wrong_report_types_are_invalid_slots_not_suite_crashes(api, bad_report):
    s = suite(trials=2)
    rows = [{"case_id": "audit", "trial_id": n, "status": "completed", "report_path": str(n)}
            for n in (1, 2)]
    result = api.evaluate_suite(s, submission(s, rows), report_loader=lambda p: bad_report if p == "1" else report())
    assert result["metrics"]["invalid"] == 1
    assert result["metrics"]["completed"] == 1


def test_keyboard_interrupt_is_not_swallowed(api):
    s = suite()
    def interrupt(_):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        api.evaluate_suite(s, submission(s), report_loader=interrupt)


def test_repeat_evaluation_is_identical_and_preserves_inputs(api):
    s = suite()
    sub = submission(s)
    r = report()
    before = copy.deepcopy((s, sub, r))
    first = api.evaluate_suite(s, sub, report_loader=lambda _: r)
    second = api.evaluate_suite(s, sub, report_loader=lambda _: r)
    assert first == second
    assert (s, sub, r) == before
    assert "人工冻结的合成报告" not in json.dumps(first, ensure_ascii=False)


def test_missing_stock_type_is_unknown_instead_of_inferred_from_case(api):
    c = case()
    r = report(c)
    r.pop("run_metadata")
    r.pop("instrument_type")
    assert checks(api.grade_report(c, r))["identity"] == "unknown"


def test_unknown_quality_record_remains_unknown(api):
    r = report()
    r["data_quality"]["status"] = "unknown"
    assert checks(api.grade_report(case(), r))["quality_record"] == "unknown"


@pytest.mark.parametrize("kind", ["missing_creation", "missing_meta_type", "extra_quality_role", "duplicate_quality_role"])
def test_incomplete_metadata_and_nonbijective_quality_cards_cannot_pass(api, kind):
    r = report()
    if kind == "missing_creation":
        r["run_metadata"].pop("created_at")
    elif kind == "missing_meta_type":
        r["run_metadata"].pop("instrument_type")
    elif kind == "extra_quality_role":
        extra = copy.deepcopy(r["data_quality"]["analysts"][0])
        extra["role"] = "news"
        r["data_quality"]["analysts"].append(extra)
    else:
        r["data_quality"]["analysts"].append(copy.deepcopy(r["data_quality"]["analysts"][0]))
    try:
        result = api.grade_report(case(), r)
    except api.EvaluationInputError:
        return
    assert result["contract_pass"] is False


@pytest.mark.parametrize("kind", ["bool_suite_schema", "bool_submission_schema", "unhashable_case_id", "null_digest"])
def test_invalid_schema_types_raise_clear_input_errors(api, kind):
    s = suite()
    if kind == "bool_suite_schema":
        s["schema_version"] = True
    sub = submission(s)
    if kind == "bool_submission_schema":
        sub["schema_version"] = True
    elif kind == "unhashable_case_id":
        sub["runs"][0]["case_id"] = []
    elif kind == "null_digest":
        sub["suite_digest"] = None
    with pytest.raises(api.EvaluationInputError):
        api.evaluate_suite(s, sub, report_loader=lambda _: report())


@pytest.mark.parametrize("kind", ["top_team", "quality_team", "quality_analysts", "meta_snapshot"])
def test_nested_bad_report_types_cannot_abort_other_trials(api, kind):
    bad = report()
    if kind == "top_team":
        bad["selected_analysts"] = [{}]
    elif kind == "quality_team":
        bad["data_quality"]["selected_analysts"] = [{}]
    elif kind == "quality_analysts":
        bad["data_quality"]["analysts"] = [{"role": []}]
    else:
        bad["run_metadata"]["config_snapshot"] = ["bad"]
    s = suite(trials=2)
    rows = [{"case_id": "audit", "trial_id": n, "status": "completed", "report_path": str(n)} for n in (1, 2)]
    result = api.evaluate_suite(s, submission(s, rows), report_loader=lambda p: bad if p == "1" else report())
    assert result["metrics"]["invalid"] == 1
    assert result["metrics"]["completed"] == 1


def test_invalid_creation_time_does_not_pass_run_metadata_check(api):
    r = report()
    r["run_metadata"]["created_at"] = "not a date"
    assert checks(api.grade_report(case(), r))["run_metadata"] == "fail"


@pytest.mark.parametrize("kind", ["null_snapshot", "bool_schema"])
def test_invalid_metadata_shape_cannot_be_repaired_by_truthy_defaults(api, kind):
    r = report()
    if kind == "null_snapshot":
        r["run_metadata"]["config_snapshot"] = None
        r["run_metadata"]["config_fingerprint"] = digest({})
    else:
        r["run_metadata"]["schema_version"] = True
    try:
        g = api.grade_report(case(), r)
    except api.EvaluationInputError:
        return
    assert checks(g)["run_metadata"] == "fail"
    assert not g["contract_pass"]
