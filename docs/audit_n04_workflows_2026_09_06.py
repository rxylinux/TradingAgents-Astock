"""Independent evidence, paired comparison and real offline CLI workflows."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from docs.audit_n04_core_2026_09_06 import api as _api_fixture
from docs.audit_n04_core_2026_09_06 import case, checks, digest, report, submission, suite

api = _api_fixture


def review(r, verdicts):
    return {"report_digest": digest(r), "reviewer": "独立人工审计",
            "reviewed_at": "2026-01-16T09:00:00+08:00", "claims": [
                {"claim_id": f"claim_{n}", "field": "market_report", "quote": "审计事实甲",
                 "verdict": verdict, "evidence_ids": eids}
                for n, (verdict, eids) in enumerate(verdicts)]}


def evaluated(api, c=None, r=None, rev=None, trials=1):
    c = c or case()
    r = r or report(c)
    s = suite([c], trials=trials)
    sub = submission(s)
    if rev is not None:
        sub["runs"][0]["review"] = rev
    return api.evaluate_suite(s, sub, report_loader=lambda _: r)


def test_future_microsecond_and_timezone_offset_are_respected(api):
    r = report()
    rev = review(r, [("supported", ["past"]), ("supported", ["equal"]),
                     ("supported", ["future"]), ("unverifiable", [])])
    result = evaluated(api, r=r, rev=rev)
    m = result["metrics"]
    assert m["reviewed_claims"] == 4
    assert m["evidence_backed_claims"] == 3
    assert m["factual_support_rate"] == pytest.approx(0.5)
    assert m["temporal_violation_rate"] == pytest.approx(1 / 3)
    rr = result["trials"][0]["review"]
    assert rr["supported"] == 2 and rr["future_evidence"] == 1
    assert rr["unverifiable"] == 1


@pytest.mark.parametrize("kind", ["wrong_digest", "wrong_quote", "unknown_evidence"])
def test_stale_or_unbound_reviews_are_unknown_not_zero_or_full_score(api, kind):
    r = report()
    rev = review(r, [("supported", ["past"])])
    if kind == "wrong_digest":
        rev["report_digest"] = "f" * 64
    elif kind == "wrong_quote":
        rev["claims"][0]["quote"] = "这句并没有出现在报告正文"
    else:
        rev["claims"][0]["evidence_ids"] = ["missing-evidence"]
    result = evaluated(api, r=r, rev=rev)
    assert result["trials"][0]["review"]["review_status"] == "invalid"
    assert result["metrics"]["invalid_reviews"] == 1
    assert result["metrics"]["factual_support_rate"] is None
    assert result["metrics"]["temporal_violation_rate"] is None


def test_fact_summary_uses_claim_denominators_not_mean_of_trial_rates(api):
    c = case()
    r = report(c)
    s = suite([c], trials=2)
    rows = [
        {"case_id": "audit", "trial_id": 1, "status": "completed", "report_path": "r.json",
         "review": review(r, [("supported", ["past"])])},
        {"case_id": "audit", "trial_id": 2, "status": "completed", "report_path": "r.json",
         "review": review(r, [("unverifiable", [])] * 9)},
    ]
    result = api.evaluate_suite(s, submission(s, rows), report_loader=lambda _: r)
    assert result["metrics"]["reviewed_claims"] == 10
    assert result["metrics"]["factual_support_rate"] == pytest.approx(0.1)
    rows[0]["review"] = review(r, [("supported", ["future"])])
    rows[1]["review"] = review(r, [("supported", ["past"])] * 9)
    result = api.evaluate_suite(s, submission(s, rows), report_loader=lambda _: r)
    assert result["metrics"]["evidence_backed_claims"] == 10
    assert result["metrics"]["temporal_violation_rate"] == pytest.approx(0.1)


def test_observations_preserve_zero_missing_and_currency_groups(api):
    s = suite(trials=4)
    rows = [{"case_id": "audit", "trial_id": n, "status": "failed"} for n in (1, 2, 3)]
    rows[0]["observations"] = {"latency_ms": 0, "input_tokens": 0, "cost_amount": 0, "cost_currency": "USD"}
    rows[1]["observations"] = {"latency_ms": 100, "cost_amount": 2, "cost_currency": "USD"}
    rows[2]["observations"] = {"cost_amount": 7, "cost_currency": "CNY"}
    result = api.evaluate_suite(s, submission(s, rows), report_loader=lambda _: pytest.fail("failed run loaded"))
    o = result["metrics"]["observations"]
    assert o["latency_ms"] == {"observed_count": 2, "mean": 50}
    assert o["input_tokens"] == {"observed_count": 1, "mean": 0}
    assert o["output_tokens"] == {"observed_count": 0, "mean": None}
    assert o["cost_by_currency"]["USD"] == {"observed_count": 2, "mean": 1}
    assert o["cost_by_currency"]["CNY"] == {"observed_count": 1, "mean": 7}


def test_regression_in_already_failing_trial_is_still_reported(api):
    c = case()
    c["allowed_ratings"] = ["Buy"]
    r = report(c)
    r["final_trade_decision"] = "Rating: Sell"
    baseline = evaluated(api, c=c, r=r)
    r.pop("run_metadata")
    candidate = evaluated(api, c=c, r=r)
    compared = api.compare_evaluations(baseline, candidate)
    assert any(x["check_id"] == "run_metadata" and x["baseline_status"] == "pass"
               and x["candidate_status"] == "unknown" for x in compared["degraded_checks"])


def test_empty_review_rate_is_not_zero_in_pairwise_improvement(api):
    r = report()
    b = evaluated(api, r=r, rev=review(r, []))
    c = evaluated(api, r=r, rev=review(r, [("supported", ["past"])]))
    compared = api.compare_evaluations(b, c)
    assert compared["paired_review"]["support_improved"] == 0
    assert compared["paired_review"]["support_degraded"] == 0


def test_nonoverlapping_reviews_do_not_produce_paired_quality_claims(api):
    s = suite(trials=2)
    r = report()
    rows = [{"case_id": "audit", "trial_id": n, "status": "completed", "report_path": "r.json"}
            for n in (1, 2)]
    rows[0]["review"] = review(r, [("supported", ["past"])])
    b = api.evaluate_suite(s, submission(s, rows), report_loader=lambda _: r)
    rows[0].pop("review")
    rows[1]["review"] = review(r, [("unverifiable", [])])
    c = api.evaluate_suite(s, submission(s, rows), report_loader=lambda _: r)
    compared = api.compare_evaluations(b, c)
    assert compared["paired_review"]["covered_trials"] == 0
    assert compared["paired_review"]["support_degraded"] == 0


@pytest.mark.parametrize("kind", ["empty", "duplicate_trial", "metrics_lie", "nonbool_pass", "missing_check",
                                 "changed_suite", "changed_grader"])
def test_comparison_rejects_unusable_or_inconsistent_evaluation_files(api, kind):
    b = evaluated(api)
    c = copy.deepcopy(b)
    if kind == "empty":
        b = c = {}
    elif kind == "duplicate_trial":
        c["trials"].append(copy.deepcopy(c["trials"][0]))
    elif kind == "metrics_lie":
        c["metrics"]["expected"] = 999
    elif kind == "nonbool_pass":
        c["trials"][0]["contract_pass"] = "false"
    elif kind == "missing_check":
        c["trials"][0]["checks"].pop()
    elif kind == "changed_suite":
        c["suite_digest"] = "f" * 64
    else:
        c["grader_version"] = "2"
    with pytest.raises(ValueError):
        api.compare_evaluations(b, c)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli(tmp_path):
    guard = tmp_path / "network-denied"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def denied(*args, **kwargs):\n    raise AssertionError('N04 CLI remote I/O forbidden')\n"
        "socket.socket.connect = denied\nsocket.create_connection = denied\nsocket.getaddrinfo = denied\n"
    )
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")}
    env["PYTHONPATH"] = os.pathsep.join((str(guard), str(ROOT)))
    def run(*args):
        return subprocess.run([sys.executable, "-m", "tradingagents.evaluation", *map(str, args)],
                              cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25)
    return run


def input_files(tmp_path):
    folder = tmp_path / "input"
    folder.mkdir()
    s = suite()
    sub = submission(s)
    for name, value in (("suite.json", s), ("submission.json", sub), ("report.json", report())):
        (folder / name).write_text(json.dumps(value, ensure_ascii=False))
    return folder


def test_real_cli_roundtrip_digest_pairing_and_immutable_inputs(cli, tmp_path):
    folder = input_files(tmp_path)
    before = {p: p.read_bytes() for p in folder.iterdir()}
    one = tmp_path / "one"
    two = tmp_path / "two"
    for out in (one, two):
        proc = cli("run", "--suite", folder / "suite.json", "--submission", folder / "submission.json", "--output-dir", out)
        assert proc.returncode == 0, proc.stderr
        assert json.loads((out / "evaluation.json").read_text())["metrics"]["contract_passed"] == 1
        assert (out / "evaluation.md").read_text().strip()
    assert (one / "evaluation.json").read_bytes() == (two / "evaluation.json").read_bytes()
    proc = cli("digest", folder / "suite.json")
    assert proc.returncode == 0 and proc.stdout.strip() == digest(json.loads((folder / "suite.json").read_text()))
    proc = cli("compare", "--baseline", one / "evaluation.json", "--candidate", two / "evaluation.json",
               "--output-dir", tmp_path / "comparison", "--fail-on-regression")
    assert proc.returncode == 0, proc.stderr
    assert {p: p.read_bytes() for p in folder.iterdir()} == before


@pytest.mark.parametrize("bad_bytes", [b"{not json", b"\xff\xfe"])
def test_real_cli_bad_report_is_isolated_and_still_writes_evaluation(cli, tmp_path, bad_bytes):
    folder = input_files(tmp_path)
    (folder / "report.json").write_bytes(bad_bytes)
    proc = cli("run", "--suite", folder / "suite.json", "--submission", folder / "submission.json",
               "--output-dir", tmp_path / "out")
    assert proc.returncode == 0, proc.stderr
    m = json.loads((tmp_path / "out/evaluation.json").read_text())["metrics"]
    assert m["invalid"] == 1 and m["contract_passed"] == 0
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize("kind", ["dotdot_inside", "absolute", "sibling_symlink"])
def test_cli_rejects_path_escape_before_scoring(cli, tmp_path, kind):
    folder = input_files(tmp_path)
    sub = json.loads((folder / "submission.json").read_text())
    if kind == "dotdot_inside":
        (folder / "inside").mkdir()
        sub["runs"][0]["report_path"] = "inside/../report.json"
    elif kind == "absolute":
        sub["runs"][0]["report_path"] = str(folder / "report.json")
    else:
        sibling = tmp_path / "input-secrets"
        sibling.mkdir()
        secret = sibling / "secret.json"
        secret.write_text(json.dumps(report()))
        (folder / "escape.json").symlink_to(secret)
        sub["runs"][0]["report_path"] = "escape.json"
    (folder / "submission.json").write_text(json.dumps(sub))
    proc = cli("run", "--suite", folder / "suite.json", "--submission", folder / "submission.json",
               "--output-dir", tmp_path / "out")
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert not (tmp_path / "out").exists()
    assert "Traceback" not in proc.stderr


def test_cli_refuses_existing_output_without_changing_any_file(cli, tmp_path):
    folder = input_files(tmp_path)
    before = {p: p.read_bytes() for p in folder.iterdir()}
    proc = cli("run", "--suite", folder / "suite.json", "--submission", folder / "submission.json",
               "--output-dir", folder)
    assert proc.returncode == 2
    assert {p: p.read_bytes() for p in folder.iterdir()} == before


def test_cli_flags_regression_even_when_both_trials_already_fail(cli, tmp_path, api):
    c = case()
    c["allowed_ratings"] = ["Buy"]
    r = report(c)
    r["final_trade_decision"] = "Rating: Sell"
    b = evaluated(api, c=c, r=r)
    r.pop("run_metadata")
    cand = evaluated(api, c=c, r=r)
    (tmp_path / "b.json").write_text(json.dumps(b))
    (tmp_path / "c.json").write_text(json.dumps(cand))
    proc = cli("compare", "--baseline", tmp_path / "b.json", "--candidate", tmp_path / "c.json",
               "--output-dir", tmp_path / "diff", "--fail-on-regression")
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert (tmp_path / "diff/comparison.json").exists()
    assert "run_metadata" in (tmp_path / "diff/comparison.md").read_text()


def test_public_grader_honours_review_parameter(api):
    r = report()
    g = api.grade_report(case(), r, review(r, [("supported", ["future"])]))
    assert g["review"]["review_status"] == "reviewed"
    assert g["review"]["factual_support_rate"] == 0


def test_output_creation_race_never_removes_another_process_directory(api, monkeypatch, tmp_path):
    from tradingagents.evaluation.cli import _write_output
    target = tmp_path / "racing-output"
    mkdir = Path.mkdir
    def competing_mkdir(p, *args, **kwargs):
        if p == target:
            mkdir(p)
            raise FileExistsError("another writer created this directory")
        return mkdir(p, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", competing_mkdir)
    with pytest.raises(SystemExit) as exit_info:
        _write_output(target, {"evaluation.json": "{}"})
    assert exit_info.value.code == 2
    assert target.is_dir(), "Failed mkdir removed a directory owned by another writer"


def test_partial_write_failure_cleans_own_output_and_reports_original_error(api, monkeypatch, tmp_path):
    from tradingagents.evaluation.cli import _write_output
    target = tmp_path / "partial-output"
    write = Path.write_text
    def failing_write(p, data, *args, **kwargs):
        if p.name == "evaluation.md":
            write(p, "half-written", *args, **kwargs)
            raise OSError("audit simulated full disk")
        return write(p, data, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", failing_write)
    with pytest.raises(SystemExit) as exit_info:
        _write_output(target, {"evaluation.json": "{}", "evaluation.md": "report"})
    assert exit_info.value.code == 2
    assert not target.exists(), "A half-written evaluation remained visible"


def test_comparison_accepts_its_own_failed_invalid_and_missing_trial_outputs(api):
    s = suite(trials=4)
    rows = [
        {"case_id": "audit", "trial_id": 1, "status": "completed", "report_path": "good.json"},
        {"case_id": "audit", "trial_id": 2, "status": "failed"},
        {"case_id": "audit", "trial_id": 3, "status": "completed", "report_path": "bad.json"},
    ]
    result = api.evaluate_suite(s, submission(s, rows), report_loader=lambda p: report() if p == "good.json" else [])
    compared = api.compare_evaluations(result, copy.deepcopy(result))
    assert compared["summary"]["total_pairs"] == 4
    assert compared["degraded_checks"] == []


@pytest.mark.parametrize("kind", ["false_contract", "duplicate_check", "fake_rate", "wrong_status_counts", "nonfinite_observation"])
def test_compare_import_checks_semantics_and_measurements_not_only_shapes(api, kind):
    b = evaluated(api)
    c = copy.deepcopy(b)
    if kind == "false_contract":
        c["trials"][0]["checks"][0]["status"] = "fail"
    elif kind == "duplicate_check":
        c["trials"][0]["checks"].append(copy.deepcopy(c["trials"][0]["checks"][0]))
    elif kind == "fake_rate":
        c["metrics"]["contract_pass_rate"] = 0.1
    elif kind == "wrong_status_counts":
        c["metrics"].update(completed=0, failed=1, completion_rate=0, failure_rate=1)
    else:
        c["trials"][0]["observations"] = {"latency_ms": float("nan")}
    with pytest.raises(ValueError):
        api.compare_evaluations(b, c)


def test_failed_output_cleanup_preserves_unrelated_file_created_in_directory(api, monkeypatch, tmp_path):
    from tradingagents.evaluation.cli import _write_output
    target = tmp_path / "mixed-output"
    write = Path.write_text
    def failing_write(p, data, *args, **kwargs):
        if p.name == "evaluation.md":
            write(target / "another-writer.txt", "keep-me")
            write(p, "partial", *args, **kwargs)
            raise OSError("audit interrupted write")
        return write(p, data, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", failing_write)
    with pytest.raises(SystemExit) as exit_info:
        _write_output(target, {"evaluation.json": "{}", "evaluation.md": "report"})
    assert exit_info.value.code == 2
    assert (target / "another-writer.txt").read_text() == "keep-me"
    assert not (target / "evaluation.json").exists()
    assert not (target / "evaluation.md").exists()


def test_missing_candidate_trial_lists_lost_checks_and_trial_identity(api):
    s = suite()
    b = api.evaluate_suite(s, submission(s), report_loader=lambda _: report())
    c = api.evaluate_suite(s, submission(s, []), report_loader=lambda _: pytest.fail("missing run loaded"))
    comp = api.compare_evaluations(b, c)
    assert comp["summary"]["degraded"] == 1
    assert any(d["case_id"] == "audit" and d["trial_id"] == 1 and d["check_id"] == "decision_present"
               and d["candidate_status"] == "absent" for d in comp["degraded_checks"])
    assert "audit" in api.render_comparison_markdown(comp)


@pytest.mark.parametrize("kind", ["provided", "null_rate", "negative_cost", "review_type", "bool_schema"])
def test_compare_reuses_input_contracts_for_all_consumed_fields(api, kind):
    b = evaluated(api)
    c = copy.deepcopy(b)
    if kind == "provided":
        c["metrics"]["provided"] = 999
    elif kind == "null_rate":
        c["metrics"]["contract_pass_rate"] = None
    elif kind == "negative_cost":
        c["trials"][0]["observations"] = {"cost_amount": -3, "cost_currency": "USD"}
    elif kind == "review_type":
        c["trials"][0]["review"] = []
    else:
        c["schema_version"] = True
    with pytest.raises(ValueError):
        api.compare_evaluations(b, c)


def test_processed_review_rate_must_match_its_annotated_claims(api):
    r = report()
    b = evaluated(api, r=r, rev=review(r, [("unverifiable", [])]))
    c = copy.deepcopy(b)
    c["trials"][0]["review"]["factual_support_rate"] = 1
    with pytest.raises(ValueError):
        api.compare_evaluations(b, c)


@pytest.mark.parametrize("kind", [
    "null_support", "null_temporal", "count", "claims_null", "bad_claim",
    "global_support", "global_cost", "unknown_checks", "no_evidence_rate", "review_null",
])
def test_imported_review_and_aggregate_cannot_disagree_with_trial_detail(api, kind):
    r = report()
    verdicts = [("supported", ["past"])]
    if kind == "no_evidence_rate":
        verdicts = [("unverifiable", [])]
    b = evaluated(api, r=r, rev=review(r, verdicts))
    c = copy.deepcopy(b)
    rev = c["trials"][0]["review"]
    if kind == "null_support":
        rev["factual_support_rate"] = None
    elif kind == "null_temporal":
        rev["temporal_violation_rate"] = None
    elif kind == "count":
        rev["supported"] = 0
    elif kind == "claims_null":
        rev["claims"] = None
    elif kind == "bad_claim":
        rev["claims"][0]["verdict"] = "invented"
        rev["factual_support_rate"] = 0
    elif kind == "global_support":
        c["metrics"]["factual_support_rate"] = 0
    elif kind == "global_cost":
        c["metrics"]["observations"]["cost_by_currency"] = {
            "USD": {"observed_count": 1, "mean": -42},
        }
    elif kind == "unknown_checks":
        c["metrics"]["unknown_checks"] = 999
    elif kind == "no_evidence_rate":
        rev["temporal_violation_rate"] = 1
    else:
        c["trials"][0]["review"] = None
    with pytest.raises(ValueError):
        api.compare_evaluations(b, c)
