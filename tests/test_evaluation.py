"""N04 evaluation regressions: schema, grading, comparison, CLI.

All offline; no network/model/market calls. Uses temporary dirs.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.quality_gate import REPORT_FIELDS, create_quality_gate
from tradingagents.agents.report_quality import append_limitation_notice
from tradingagents.evaluation import (
    EvaluationInputError,
    canonical_digest,
    compare_evaluations,
    evaluate_suite,
    grade_report,
    render_comparison_markdown,
    render_evaluation_markdown,
)
from tradingagents.evaluation.schema import validate_suite
from tradingagents.graph.propagation import Propagator
from tradingagents.run_records import new_run_metadata
from tradingagents.default_config import DEFAULT_CONFIG

GOOD = "离线研究报告内容 " * 60 + "\n| 指标 | 结论 |\n|---|---|\n| 综合 | 通过 |"


def _case(**kw):
    base = {
        "case_id": "c1", "ticker": "600519", "trade_date": "2026-01-15",
        "instrument_type": "stock", "as_of": "2026-01-15T15:00:00+08:00",
        "selected_analysts": ["market"], "expected_quality_status": "complete",
        "allowed_ratings": ["Buy", "Overweight", "Hold", "Underweight", "Sell"],
        "evidence": [{"evidence_id": "e1", "available_at": "2026-01-15T10:00:00+08:00", "text": "合成证据"}],
    }
    base.update(kw)
    return base


def _suite(**kw):
    base = {
        "schema_version": 1, "suite_id": "test", "suite_version": "1",
        "title": "测试", "trials_per_case": 1,
        "cases": [_case()],
    }
    base.update(kw)
    return base


def _report(ticker="600519", date="2026-01-15", roles=("market",),
            decision="Rating: Buy\n分析完整。", metadata=True, itype="stock",
            append_notice=True):
    state = Propagator().create_initial_state(ticker, date, selected_analysts=list(roles))
    for r in roles:
        state[REPORT_FIELDS[r]] = GOOD
    llm = Mock()
    llm.invoke.return_value = AIMessage(content="rv")
    state.update(create_quality_gate(llm)(state))
    state["final_trade_decision"] = decision
    state["trader_investment_plan"] = "分批建仓"
    state["trader_investment_decision"] = "分批建仓"
    if append_notice:
        state["final_trade_decision"] = append_limitation_notice(
            state["final_trade_decision"], state["data_quality"])
    if metadata:
        state["run_metadata"] = new_run_metadata(
            DEFAULT_CONFIG, ticker, date, instrument_type=itype, selected_analysts=list(roles))
    return state


def _submission(suite, runs):
    return {
        "schema_version": 1, "submission_id": "test-sub",
        "suite_id": suite["suite_id"], "suite_version": suite["suite_version"],
        "suite_digest": canonical_digest(suite),
        "runs": runs,
    }


def _loader_from_dict(reports: dict):
    return lambda path: reports[path]


# ===========================================================================
# Schema validation
# ===========================================================================


class TestSuiteValidation:
    def test_valid_suite_passes(self):
        validate_suite(_suite())

    def test_unknown_field_rejected(self):
        suite = _suite()
        suite["unknown_top"] = "x"
        with pytest.raises(EvaluationInputError, match="未知字段"):
            validate_suite(suite)

    def test_duplicate_case_id_rejected(self):
        suite = _suite()
        suite["cases"].append(_case())
        with pytest.raises(EvaluationInputError, match="重复"):
            validate_suite(suite)

    def test_trials_per_case_bool_rejected(self):
        suite = _suite(trials_per_case=True)
        with pytest.raises(EvaluationInputError, match="trials_per_case"):
            validate_suite(suite)

    def test_as_of_date_mismatch_rejected(self):
        suite = _suite()
        suite["cases"][0]["as_of"] = "2026-01-16T15:00:00+08:00"
        with pytest.raises(EvaluationInputError, match="上海日期"):
            validate_suite(suite)

    def test_index_forbids_fundamentals(self):
        c = _case(instrument_type="index", selected_analysts=["market", "fundamentals"])
        with pytest.raises(EvaluationInputError, match="fundamentals"):
            validate_suite(_suite(cases=[c]))

    def test_evidence_id_duplicate_rejected(self):
        c = _case(evidence=[
            {"evidence_id": "e1", "available_at": "2026-01-15T10:00:00+08:00", "text": "A"},
            {"evidence_id": "e1", "available_at": "2026-01-15T11:00:00+08:00", "text": "B"},
        ])
        with pytest.raises(EvaluationInputError, match="重复"):
            validate_suite(_suite(cases=[c]))


class TestSubmissionValidation:
    def test_digest_mismatch_rejected(self):
        suite = _suite()
        sub = _submission(suite, [])
        sub["suite_digest"] = "0" * 64
        with pytest.raises(EvaluationInputError, match="suite_digest"):
            from tradingagents.evaluation.schema import validate_submission
            validate_submission(sub, suite)

    def test_duplicate_trial_rejected(self):
        suite = _suite()
        run = {"case_id": "c1", "trial_id": 1, "status": "completed", "report_path": "r.json"}
        sub = _submission(suite, [run, dict(run)])
        with pytest.raises(EvaluationInputError, match="重复"):
            from tradingagents.evaluation.schema import validate_submission
            validate_submission(sub, suite)

    def test_failed_with_report_path_rejected(self):
        suite = _suite()
        sub = _submission(suite, [
            {"case_id": "c1", "trial_id": 1, "status": "failed", "report_path": "r.json"},
        ])
        with pytest.raises(EvaluationInputError, match="failed.*report_path"):
            from tradingagents.evaluation.schema import validate_submission
            validate_submission(sub, suite)

    def test_cost_without_currency_rejected(self):
        suite = _suite()
        sub = _submission(suite, [
            {"case_id": "c1", "trial_id": 1, "status": "completed", "report_path": "r.json",
             "observations": {"cost_amount": 1.0}},
        ])
        with pytest.raises(EvaluationInputError, match="成对"):
            from tradingagents.evaluation.schema import validate_submission
            validate_submission(sub, suite)


# ===========================================================================
# Grading
# ===========================================================================


class TestGradeReport:
    def test_passing_report_all_checks_pass(self):
        case = _case()
        report = _report()
        result = grade_report(case, report)
        assert result["contract_pass"]
        assert all(c["status"] == "pass" or c["status"] == "not_applicable"
                   for c in result["checks"])

    def test_legacy_report_unknown_checks(self):
        case = _case()
        report = _report(metadata=False)
        report.pop("data_quality", None)
        result = grade_report(case, report)
        checks = {c["check_id"]: c["status"] for c in result["checks"]}
        assert checks["quality_record"] == "unknown"
        assert checks["run_metadata"] == "unknown"
        assert not result["contract_pass"]  # unknown 不当通过

    def test_no_rating_fails(self):
        case = _case()
        report = _report(decision="无明确评级")
        result = grade_report(case, report)
        checks = {c["check_id"]: c["status"] for c in result["checks"]}
        assert checks["rating"] == "fail"
        assert not result["contract_pass"]

    def test_wrong_ticker_fails_identity(self):
        case = _case(ticker="000858")
        report = _report()  # 600519
        result = grade_report(case, report)
        checks = {c["check_id"]: c["status"] for c in result["checks"]}
        assert checks["identity"] == "fail"


class TestEvaluateSuite:
    def test_missing_trial_enters_denominator(self):
        suite = _suite(trials_per_case=2)
        sub = _submission(suite, [
            {"case_id": "c1", "trial_id": 1, "status": "completed", "report_path": "r.json"},
            # trial 2 missing
        ])
        reports = {"r.json": _report()}
        result = evaluate_suite(suite, sub, report_loader=_loader_from_dict(reports))
        m = result["metrics"]
        assert m["expected"] == 2
        assert m["missing"] == 1
        assert m["completion_rate"] == 0.5
        assert m["failure_rate"] == 0.5

    def test_failed_trial_counts_in_failure(self):
        suite = _suite()
        sub = _submission(suite, [
            {"case_id": "c1", "trial_id": 1, "status": "failed"},
        ])
        result = evaluate_suite(suite, sub, report_loader=lambda p: {})
        assert result["metrics"]["failed"] == 1
        assert result["metrics"]["failure_rate"] == 1.0

    def test_inputs_not_mutated(self):
        suite = _suite()
        run = {"case_id": "c1", "trial_id": 1, "status": "completed", "report_path": "r.json"}
        sub = _submission(suite, [run])
        report = _report()
        import copy
        suite_before = copy.deepcopy(suite)
        sub_before = copy.deepcopy(sub)
        report_before = copy.deepcopy(report)
        evaluate_suite(suite, sub, report_loader=lambda p: report)
        assert suite == suite_before
        assert sub == sub_before
        assert report == report_before

    def test_future_evidence_excluded_from_supported(self):
        case = _case(evidence=[
            {"evidence_id": "ok", "available_at": "2026-01-15T10:00:00+08:00", "text": "A"},
            {"evidence_id": "future", "available_at": "2026-06-01T00:00:00+08:00", "text": "B"},
        ])
        suite = _suite(cases=[case])
        report = _report()
        rdig = canonical_digest(report)
        sub = _submission(suite, [{
            "case_id": "c1", "trial_id": 1, "status": "completed",
            "report_path": "r.json",
            "review": {
                "report_digest": rdig, "reviewer": "审核者",
                "reviewed_at": "2026-01-16T09:00:00+08:00",
                "claims": [
                    {"claim_id": "c1", "field": "market_report", "quote": "离线研究报告内容",
                     "verdict": "supported", "evidence_ids": ["ok"]},
                    {"claim_id": "c2", "field": "market_report", "quote": "离线研究报告内容",
                     "verdict": "supported", "evidence_ids": ["future"]},
                ],
            },
        }])
        result = evaluate_suite(suite, sub, report_loader={"r.json": report}.get)
        m = result["metrics"]
        # 2 claims, 1 supported with future evidence excluded → rate = 0.5
        assert m["factual_support_rate"] == 0.5
        assert m["temporal_violation_rate"] == 0.5  # 1 future / 2 evidence-backed

    def test_mixed_currency_not_summed(self):
        suite = _suite(trials_per_case=2)
        sub = _submission(suite, [
            {"case_id": "c1", "trial_id": 1, "status": "completed", "report_path": "r.json",
             "observations": {"cost_amount": 1.0, "cost_currency": "USD"}},
            {"case_id": "c1", "trial_id": 2, "status": "completed", "report_path": "r.json",
             "observations": {"cost_amount": 7.0, "cost_currency": "CNY"}},
        ])
        result = evaluate_suite(suite, sub, report_loader={"r.json": _report()}.get)
        costs = result["metrics"]["observations"]["cost_by_currency"]
        assert "USD" in costs and "CNY" in costs
        assert costs["USD"]["mean"] == 1.0
        assert costs["CNY"]["mean"] == 7.0


# ===========================================================================
# Comparison
# ===========================================================================


class TestCompare:
    def test_incomparable_digest_rejected(self):
        from tests.conftest import _make_eval_result
        trial = [{"case_id": "c1", "trial_id": 1, "pass": True}]
        b = _make_eval_result("base", trial)
        c = _make_eval_result("cand", trial)
        c["suite_digest"] = "different"
        with pytest.raises(ValueError, match="suite_digest"):
            compare_evaluations(b, c)

    def test_regression_detected(self):
        from tests.conftest import _make_eval_result
        b = _make_eval_result("base", [{"case_id": "c1", "trial_id": 1, "pass": True}])
        c = _make_eval_result("cand", [{"case_id": "c1", "trial_id": 1, "pass": False}])
        result = compare_evaluations(b, c)
        assert result["summary"]["degraded"] == 1
        assert result["degraded_checks"]
        md = render_comparison_markdown(result)
        assert "退化" in md


# ===========================================================================
# CLI subprocess
# ===========================================================================


class TestCLI:
    @pytest.fixture
    def demo_dir(self, tmp_path):
        """Copy examples and run CLI end-to-end."""
        src = Path("examples/evaluation")
        dst = tmp_path / "eval"
        dst.mkdir()
        for f in src.rglob("*.json"):
            rel = f.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.read_text(encoding="utf-8"))
        return dst

    def test_run_success(self, demo_dir):
        out = demo_dir / "out"
        result = subprocess.run(
            [sys.executable, "-m", "tradingagents.evaluation", "run",
             "--suite", str(demo_dir / "suite.json"),
             "--submission", str(demo_dir / "baseline.json"),
             "--output-dir", str(out)],
            capture_output=True, text=True, cwd=".", timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert (out / "evaluation.json").exists()
        assert (out / "evaluation.md").exists()

    def test_run_output_dir_exists_rejected(self, demo_dir, tmp_path):
        existing = tmp_path / "exists"
        existing.mkdir()
        result = subprocess.run(
            [sys.executable, "-m", "tradingagents.evaluation", "run",
             "--suite", str(demo_dir / "suite.json"),
             "--submission", str(demo_dir / "baseline.json"),
             "--output-dir", str(existing)],
            capture_output=True, text=True, cwd=".", timeout=30,
        )
        assert result.returncode == 2
        assert "已存在" in result.stderr

    def test_compare_fail_on_regression_exit_1(self, demo_dir):
        base_out = demo_dir / "base"
        cand_out = demo_dir / "cand"
        diff_out = demo_dir / "diff"
        for sub, out in [("baseline.json", base_out), ("candidate.json", cand_out)]:
            subprocess.run(
                [sys.executable, "-m", "tradingagents.evaluation", "run",
                 "--suite", str(demo_dir / "suite.json"),
                 "--submission", str(demo_dir / sub),
                 "--output-dir", str(out)],
                capture_output=True, cwd=".", timeout=30,
            )
        result = subprocess.run(
            [sys.executable, "-m", "tradingagents.evaluation", "compare",
             "--baseline", str(base_out / "evaluation.json"),
             "--candidate", str(cand_out / "evaluation.json"),
             "--output-dir", str(diff_out),
             "--fail-on-regression"],
            capture_output=True, text=True, cwd=".", timeout=30,
        )
        assert result.returncode == 1
        assert (diff_out / "comparison.json").exists()
        assert "退化" in result.stderr

    def test_digest_subcommand(self, demo_dir):
        result = subprocess.run(
            [sys.executable, "-m", "tradingagents.evaluation", "digest",
             str(demo_dir / "suite.json")],
            capture_output=True, text=True, cwd=".", timeout=30,
        )
        assert result.returncode == 0
        assert len(result.stdout.strip()) == 64

    def test_source_files_unchanged(self, demo_dir):
        """CLI 不修改源案例/报告/提交文件。"""
        suite_before = (demo_dir / "suite.json").read_bytes()
        baseline_before = (demo_dir / "baseline.json").read_bytes()
        report_before = (demo_dir / "reports" / "sc_t1.json").read_bytes()
        out = demo_dir / "out2"
        subprocess.run(
            [sys.executable, "-m", "tradingagents.evaluation", "run",
             "--suite", str(demo_dir / "suite.json"),
             "--submission", str(demo_dir / "baseline.json"),
             "--output-dir", str(out)],
            capture_output=True, cwd=".", timeout=30,
        )
        assert (demo_dir / "suite.json").read_bytes() == suite_before
        assert (demo_dir / "baseline.json").read_bytes() == baseline_before
        assert (demo_dir / "reports" / "sc_t1.json").read_bytes() == report_before

    def test_no_traceback_on_input_error(self, demo_dir):
        """输入错误不产生 traceback 堆栈。"""
        result = subprocess.run(
            [sys.executable, "-m", "tradingagents.evaluation", "run",
             "--suite", "/nonexistent.json",
             "--submission", str(demo_dir / "baseline.json"),
             "--output-dir", str(demo_dir / "err")],
            capture_output=True, text=True, cwd=".", timeout=30,
        )
        assert result.returncode == 2
        assert "Traceback" not in result.stderr


class TestSharedConsistencyRegressions:
    """Adjacent regressions for the shared summarizer consistency branches."""

    def _eval_with_review(self, verdicts=(("supported", ["e1"]),)):
        from tests.conftest import _make_eval_result
        from tradingagents.evaluation import canonical_digest
        from tradingagents.evaluation import evaluate_suite

        suite = _suite()
        report = _report()
        rdig = canonical_digest(report)
        claims = [
            {"claim_id": f"c{i}", "field": "market_report",
             "quote": "离线研究报告内容", "verdict": v, "evidence_ids": eids}
            for i, (v, eids) in enumerate(verdicts)
        ]
        sub = _submission(suite, [{
            "case_id": "c1", "trial_id": 1, "status": "completed",
            "report_path": "r.json",
            "review": {"report_digest": rdig, "reviewer": "审核者",
                       "reviewed_at": "2026-01-16T09:00:00+08:00", "claims": claims},
        }])
        return evaluate_suite(suite, sub, report_loader={"r.json": report}.get)

    def test_null_future_flag_rejected(self):
        r = self._eval_with_review()
        r["trials"][0]["review"]["claims"][0]["has_future_evidence"] = None
        from tradingagents.evaluation import compare_evaluations
        with pytest.raises(ValueError, match="has_future_evidence"):
            compare_evaluations(r, r)

    def test_int_evidence_id_rejected(self):
        r = self._eval_with_review()
        r["trials"][0]["review"]["claims"][0]["evidence_ids"] = [1]
        from tradingagents.evaluation import compare_evaluations
        with pytest.raises(ValueError, match="非字符串"):
            compare_evaluations(r, r)

    def test_invented_reviewed_count_rejected(self):
        """reviewed_claims_count=999 须被拒绝（非 bool 整数且等于 len(claims)）。"""
        r = self._eval_with_review()
        r["trials"][0]["review"]["reviewed_claims_count"] = 999
        from tradingagents.evaluation import compare_evaluations
        with pytest.raises(ValueError, match="reviewed_claims_count"):
            compare_evaluations(r, r)

    def test_reviewed_claims_count_bool_rejected(self):
        r = self._eval_with_review()
        r["trials"][0]["review"]["reviewed_claims_count"] = True
        from tradingagents.evaluation import compare_evaluations
        with pytest.raises(ValueError, match="reviewed_claims_count"):
            compare_evaluations(r, r)

    def test_reviewed_claims_count_non_null_on_unreviewed_rejected(self):
        """invalid/unreviewed 的 reviewed_claims_count 若存在须为 null。"""
        r = self._eval_with_review()
        r["trials"][0]["review"]["review_status"] = "unreviewed"
        r["trials"][0]["review"]["reviewed_claims_count"] = 1
        from tradingagents.evaluation import compare_evaluations
        with pytest.raises(ValueError):
            compare_evaluations(r, r)

    def test_list_check_id_rejected_not_crash(self):
        r = self._eval_with_review()
        r["trials"][0]["checks"][0]["check_id"] = []
        from tradingagents.evaluation import compare_evaluations
        with pytest.raises(ValueError, match="check_id.*字符串"):
            compare_evaluations(r, r)

    def test_bool_metrics_completed_rejected(self):
        r = self._eval_with_review()
        r["metrics"]["completed"] = True
        from tradingagents.evaluation import compare_evaluations
        with pytest.raises(ValueError, match="completed"):
            compare_evaluations(r, r)

    def test_null_mean_with_observations_rejected(self):
        from tests.conftest import _make_eval_result
        from tradingagents.evaluation import compare_evaluations

        trial = [{"case_id": "c1", "trial_id": 1, "pass": True}]
        r = _make_eval_result("base", trial)
        # Add latency observation
        r["trials"][0]["observations"] = {"latency_ms": 5}
        # Override metrics observations to have count=1 but mean=None
        r["metrics"]["observations"]["latency_ms"] = {"observed_count": 1, "mean": None}
        with pytest.raises(ValueError, match="mean.*null 但有观测值"):
            compare_evaluations(r, r)
