"""N04 evaluation data validation, canonical digest, and path safety."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tradingagents.agents.quality_gate import DEFAULT_ANALYSTS, REPORT_FIELDS

GRADER_VERSION = "1"

VALID_INSTRUMENT_TYPES = frozenset({"stock", "index"})
VALID_STATUSES = frozenset({"completed", "failed"})
VALID_QUALITY_STATUSES = frozenset({"complete", "limited", "insufficient"})
VALID_VERDICTS = frozenset({"supported", "contradicted", "unverifiable"})
VALID_RATINGS = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]
INDEX_FORBIDDEN_ANALYSTS = frozenset({"fundamentals", "lockup"})
FIXED_CHECK_IDS = frozenset({
    "identity", "team", "decision_present", "rating",
    "quality_status", "quality_record", "limitation_notice", "run_metadata",
})

_CLAIM_FIELDS = frozenset(REPORT_FIELDS.values()) | {
    "trader_investment_plan", "trader_investment_decision", "final_trade_decision",
}


class EvaluationInputError(ValueError):
    """Raised when suite/submission/report data fails validation."""


# ── Timezone-safe ISO parsing (Python 3.10 compatible) ──


def parse_iso(s: str) -> datetime:
    """Parse ISO datetime; normalize trailing 'Z' to '+00:00' (Py3.10 compat)."""
    if isinstance(s, str) and s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


# ── Canonical digest ──


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvaluationInputError(f"非有限数字: {value}")
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def canonical_digest(value: dict) -> str:
    try:
        payload = json.dumps(
            _json_safe(value), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        )
    except EvaluationInputError:
        raise
    except (TypeError, ValueError) as e:
        raise EvaluationInputError(f"无法生成规范 JSON: {e}") from e
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Shared validators ──


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _require_keys(obj: Any, allowed: frozenset, required: frozenset, ctx: str) -> None:
    if not isinstance(obj, dict):
        raise EvaluationInputError(f"{ctx}: 期望 JSON 对象，得到 {type(obj).__name__}")
    unknown = set(obj) - allowed
    if unknown:
        raise EvaluationInputError(f"{ctx}: 未知字段 {sorted(unknown)}")
    missing = required - set(obj)
    if missing:
        raise EvaluationInputError(f"{ctx}: 缺少必填字段 {sorted(missing)}")


def _valid_date(s: Any) -> bool:
    if not isinstance(s, str):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def _valid_tz_time(s: Any) -> bool:
    if not isinstance(s, str) or not s:
        return False
    try:
        return parse_iso(s).tzinfo is not None
    except (ValueError, TypeError):
        return False


def _shanghai_date(s: str) -> str:
    dt = parse_iso(s)
    sh = dt.astimezone(timezone(timedelta(hours=8)))
    return sh.strftime("%Y-%m-%d")


# ── Suite validation ──

_SUITE_KEYS = frozenset({
    "schema_version", "suite_id", "suite_version", "title",
    "trials_per_case", "cases",
})
_CASE_KEYS = frozenset({
    "case_id", "ticker", "trade_date", "instrument_type", "as_of",
    "selected_analysts", "expected_quality_status", "allowed_ratings", "evidence",
})
_EVIDENCE_KEYS = frozenset({"evidence_id", "available_at", "text"})


def validate_suite(suite: Any) -> dict:
    _require_keys(suite, _SUITE_KEYS, _SUITE_KEYS, "案例集")
    if not _is_int(suite["schema_version"]) or suite["schema_version"] != 1:
        raise EvaluationInputError(f"案例集 schema_version={suite['schema_version']!r}，仅支持整数 1")
    for k in ("suite_id", "suite_version", "title"):
        if not isinstance(suite[k], str) or not suite[k].strip():
            raise EvaluationInputError(f"{k} 必须为非空字符串")

    tpc = suite["trials_per_case"]
    if not _is_int(tpc) or not (1 <= tpc <= 50):
        raise EvaluationInputError(f"trials_per_case={tpc!r}，须为 1–50 整数（拒绝 bool）")

    cases = suite["cases"]
    if not isinstance(cases, list) or not cases:
        raise EvaluationInputError("cases 必须为非空列表")

    seen: set = set()
    for i, case in enumerate(cases):
        ctx = f"案例[{i}]"
        _require_keys(case, _CASE_KEYS, _CASE_KEYS, ctx)
        cid = case["case_id"]
        if not isinstance(cid, str) or not cid.strip():
            raise EvaluationInputError(f"{ctx}: case_id 必须为非空字符串")
        if cid in seen:
            raise EvaluationInputError(f"{ctx}: case_id '{cid}' 重复")
        seen.add(cid)

        if not isinstance(case["ticker"], str) or not case["ticker"].strip():
            raise EvaluationInputError(f"{ctx}: ticker 必须为非空字符串")
        if not _valid_date(case["trade_date"]):
            raise EvaluationInputError(f"{ctx}: trade_date {case['trade_date']!r} 非有效日期")

        itype = case["instrument_type"]
        if itype not in VALID_INSTRUMENT_TYPES:
            raise EvaluationInputError(f"{ctx}: instrument_type {itype!r} 须为 stock/index")

        if not _valid_tz_time(case["as_of"]):
            raise EvaluationInputError(f"{ctx}: as_of {case['as_of']!r} 须为带时区时间")
        if _shanghai_date(case["as_of"]) != case["trade_date"]:
            raise EvaluationInputError(
                f"{ctx}: as_of 上海日期 {_shanghai_date(case['as_of'])} ≠ trade_date {case['trade_date']}"
            )

        analysts = case["selected_analysts"]
        if not isinstance(analysts, list) or not analysts:
            raise EvaluationInputError(f"{ctx}: selected_analysts 须为非空列表")
        if len(set(analysts)) != len(analysts):
            raise EvaluationInputError(f"{ctx}: selected_analysts 不得重复")
        for a in analysts:
            if a not in DEFAULT_ANALYSTS:
                raise EvaluationInputError(f"{ctx}: 未知分析师 {a!r}")
        if itype == "index":
            bad = INDEX_FORBIDDEN_ANALYSTS & set(analysts)
            if bad:
                raise EvaluationInputError(f"{ctx}: 指数案例不允许 {sorted(bad)}")

        eqs = case["expected_quality_status"]
        if eqs not in VALID_QUALITY_STATUSES:
            raise EvaluationInputError(f"{ctx}: expected_quality_status {eqs!r} 须为 complete/limited/insufficient")

        allowed = case["allowed_ratings"]
        if not isinstance(allowed, list) or not allowed:
            raise EvaluationInputError(f"{ctx}: allowed_ratings 须为非空列表")
        for r in allowed:
            if r not in VALID_RATINGS:
                raise EvaluationInputError(f"{ctx}: 非法评级 {r!r}")
        if len(set(allowed)) != len(allowed):
            raise EvaluationInputError(f"{ctx}: allowed_ratings 不得重复")

        evidence = case.get("evidence", [])
        if not isinstance(evidence, list):
            raise EvaluationInputError(f"{ctx}: evidence 须为列表")
        seen_eids: set = set()
        for j, ev in enumerate(evidence):
            ectx = f"{ctx}.evidence[{j}]"
            _require_keys(ev, _EVIDENCE_KEYS, _EVIDENCE_KEYS, ectx)
            eid = ev["evidence_id"]
            if not isinstance(eid, str) or not eid.strip():
                raise EvaluationInputError(f"{ectx}: evidence_id 必须为非空字符串")
            if eid in seen_eids:
                raise EvaluationInputError(f"{ectx}: evidence_id '{eid}' 重复")
            seen_eids.add(eid)
            if not _valid_tz_time(ev["available_at"]):
                raise EvaluationInputError(f"{ectx}: available_at 须为带时区时间")
            if not isinstance(ev["text"], str) or not ev["text"].strip():
                raise EvaluationInputError(f"{ectx}: text 必须为非空字符串")

    return suite


# ── Submission validation ──

_SUBMISSION_KEYS = frozenset({
    "schema_version", "submission_id", "suite_id", "suite_version",
    "suite_digest", "runs",
})
_RUN_KEYS = frozenset({"case_id", "trial_id", "status", "report_path", "observations", "review"})
_OBS_KEYS = frozenset({"latency_ms", "input_tokens", "output_tokens", "cost_amount", "cost_currency"})
_REVIEW_KEYS = frozenset({"report_digest", "reviewer", "reviewed_at", "claims"})
_CLAIM_KEYS = frozenset({"claim_id", "field", "quote", "verdict", "evidence_ids"})


def validate_submission(submission: Any, suite: dict) -> dict:
    _require_keys(submission, _SUBMISSION_KEYS, _SUBMISSION_KEYS, "提交清单")
    if not _is_int(submission["schema_version"]) or submission["schema_version"] != 1:
        raise EvaluationInputError(f"提交清单 schema_version 非整数 1")
    if not isinstance(submission["submission_id"], str) or not submission["submission_id"].strip():
        raise EvaluationInputError("submission_id 必须为非空字符串")
    if submission["suite_id"] != suite["suite_id"]:
        raise EvaluationInputError(
            f"suite_id 不匹配: {submission['suite_id']!r} vs {suite['suite_id']!r}")
    if submission["suite_version"] != suite["suite_version"]:
        raise EvaluationInputError(
            f"suite_version 不匹配: {submission['suite_version']!r} vs {suite['suite_version']!r}")

    sd = submission["suite_digest"]
    if not isinstance(sd, str) or not sd:
        raise EvaluationInputError("suite_digest 必须为非空字符串")
    actual = canonical_digest(suite)
    if sd != actual:
        raise EvaluationInputError(
            f"suite_digest 不符: 提交 {sd[:16]}… vs 实际 {actual[:16]}…"
        )

    runs = submission["runs"]
    if not isinstance(runs, list):
        raise EvaluationInputError("runs 必须为列表")

    known_cases = {c["case_id"] for c in suite["cases"]}
    tpc = suite["trials_per_case"]
    seen: set = set()
    for i, run in enumerate(runs):
        ctx = f"run[{i}]"
        _require_keys(run, _RUN_KEYS, frozenset({"case_id", "trial_id", "status"}), ctx)
        cid = run["case_id"]
        if not isinstance(cid, str):
            raise EvaluationInputError(f"{ctx}: case_id 必须为字符串")
        if cid not in known_cases:
            raise EvaluationInputError(f"{ctx}: 未知案例 {cid!r}")
        tid = run["trial_id"]
        if not _is_int(tid) or not (1 <= tid <= tpc):
            raise EvaluationInputError(f"{ctx}: trial_id={tid!r} 越界（1–{tpc}）")
        key = (cid, tid)
        if key in seen:
            raise EvaluationInputError(f"{ctx}: 重复 (case_id={cid}, trial_id={tid})")
        seen.add(key)

        status = run["status"]
        if status not in VALID_STATUSES:
            raise EvaluationInputError(f"{ctx}: status {status!r} 须为 completed/failed")
        if status == "failed":
            if run.get("report_path") is not None:
                raise EvaluationInputError(f"{ctx}: failed run 不得携带 report_path")
            if run.get("review") is not None:
                raise EvaluationInputError(f"{ctx}: failed run 不得携带 review")
        else:
            rp = run.get("report_path")
            if not isinstance(rp, str) or not rp.strip():
                raise EvaluationInputError(f"{ctx}: completed run 必须提供 report_path")

        _validate_observations(run.get("observations"), ctx)
        _validate_review(run.get("review"), ctx)

    return submission


def _validate_observations(obs: Any, ctx: str) -> None:
    if obs is None:
        return
    _require_keys(obs, _OBS_KEYS, frozenset(), f"{ctx}.observations")
    for key in ("latency_ms", "cost_amount"):
        v = obs.get(key)
        if v is not None:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or not math.isfinite(v):
                raise EvaluationInputError(f"{ctx}.observations.{key}={v!r}，须为有限非负数")
    for key in ("input_tokens", "output_tokens"):
        v = obs.get(key)
        if v is not None and (not _is_int(v) or v < 0):
            raise EvaluationInputError(f"{ctx}.observations.{key}={v!r}，须为非负整数（拒绝 bool）")
    ca, cc = obs.get("cost_amount"), obs.get("cost_currency")
    if (ca is None) != (cc is None):
        raise EvaluationInputError(f"{ctx}.observations: cost_amount 与 cost_currency 必须成对出现")
    if cc is not None:
        if not isinstance(cc, str) or len(cc) != 3 or cc != cc.upper() or not cc.isalpha():
            raise EvaluationInputError(f"{ctx}.observations.cost_currency={cc!r}，须为三位大写字母")


def _validate_review(review: Any, ctx: str) -> None:
    if review is None:
        return
    _require_keys(review, _REVIEW_KEYS, _REVIEW_KEYS, f"{ctx}.review")
    if not isinstance(review["reviewer"], str) or not review["reviewer"].strip():
        raise EvaluationInputError(f"{ctx}.review.reviewer 必须为非空字符串")
    if not _valid_tz_time(review["reviewed_at"]):
        raise EvaluationInputError(f"{ctx}.review.reviewed_at 须为带时区时间")
    if not isinstance(review["report_digest"], str) or len(review["report_digest"]) != 64:
        raise EvaluationInputError(f"{ctx}.review.report_digest 须为 64 位 hex")
    claims = review["claims"]
    if not isinstance(claims, list):
        raise EvaluationInputError(f"{ctx}.review.claims 须为列表")
    seen: set = set()
    for j, claim in enumerate(claims):
        cctx = f"{ctx}.review.claims[{j}]"
        _require_keys(claim, _CLAIM_KEYS, _CLAIM_KEYS, cctx)
        clid = claim["claim_id"]
        if not isinstance(clid, str) or not clid.strip():
            raise EvaluationInputError(f"{cctx}: claim_id 必须为非空字符串")
        if clid in seen:
            raise EvaluationInputError(f"{cctx}: claim_id '{clid}' 重复")
        seen.add(clid)
        if claim["field"] not in _CLAIM_FIELDS:
            raise EvaluationInputError(f"{cctx}: field {claim['field']!r} 不在允许字段集")
        if not isinstance(claim["quote"], str) or not claim["quote"].strip():
            raise EvaluationInputError(f"{cctx}: quote 必须为非空字符串")
        if claim["verdict"] not in VALID_VERDICTS:
            raise EvaluationInputError(f"{cctx}: verdict {claim['verdict']!r} 非法")
        eids = claim["evidence_ids"]
        if not isinstance(eids, list):
            raise EvaluationInputError(f"{cctx}: evidence_ids 须为列表")
        for e in eids:
            if not isinstance(e, str) or not e.strip():
                raise EvaluationInputError(f"{cctx}: evidence_id 必须为非空字符串")
        if len(set(eids)) != len(eids):
            raise EvaluationInputError(f"{cctx}: evidence_ids 不得重复")
        if claim["verdict"] in ("supported", "contradicted") and not eids:
            raise EvaluationInputError(f"{cctx}: {claim['verdict']} 至少引用一条 evidence_id")


# ── Path safety ──


def validate_report_path_format(path_str: Any, root: Path) -> Path:
    """Validate path format and containment BEFORE any file access.

    Returns the resolved Path; raises EvaluationInputError for format violations.
    The caller handles file-not-found / bad-JSON as per-trial invalid separately.
    """
    if not isinstance(path_str, str) or not path_str.strip():
        raise EvaluationInputError(f"report_path 必须为非空字符串")
    p = Path(path_str)
    if p.is_absolute():
        raise EvaluationInputError(f"report_path '{path_str}' 不得为绝对路径")
    if ".." in p.parts:
        raise EvaluationInputError(f"report_path '{path_str}' 不得包含 '..'")
    resolved = (root / p).resolve()
    root_resolved = Path(root).resolve()
    # Path.relative_to, not string startswith (avoids sibling-prefix bug)
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise EvaluationInputError(
            f"report_path '{path_str}' 解析后逃出根目录 ({resolved} 不在 {root_resolved} 内)"
        ) from None
    # Symlink escape check
    if resolved.is_symlink() or any(parent.is_symlink() for parent in resolved.parents if parent != root_resolved and parent.is_absolute()):
        # If the resolved target is outside root, relative_to above would catch it.
        # But check symlink on the file itself resolving outside:
        target = resolved.resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            raise EvaluationInputError(f"report_path '{path_str}' 符号链接逃出根目录") from None
    if resolved.suffix != ".json":
        raise EvaluationInputError(f"report_path '{path_str}' 必须是 .json 文件")
    return resolved


def validate_all_report_paths(submission: dict, root: Path) -> None:
    """Pre-flight: validate all report_path formats; reject entire submission on violations."""
    for i, run in enumerate(submission.get("runs", [])):
        if run.get("status") == "completed":
            validate_report_path_format(run.get("report_path"), root)


# ── Report type validation ──


def validate_report_types(report: Any) -> None:
    """Check known report fields (including nested) have expected types."""
    if not isinstance(report, dict):
        raise EvaluationInputError(f"报告顶层非 JSON 对象: {type(report).__name__}")
    for field in ("market_report", "sentiment_report", "news_report",
                  "fundamentals_report", "policy_report", "hot_money_report",
                  "lockup_report", "final_trade_decision",
                  "trader_investment_plan", "trader_investment_decision",
                  "company_of_interest", "trade_date"):
        v = report.get(field)
        if v is not None and not isinstance(v, str):
            raise EvaluationInputError(f"报告字段 {field} 类型错误: {type(v).__name__}（须为字符串或缺失）")

    # selected_analysts: list of hashable strings
    sel = report.get("selected_analysts")
    if sel is not None:
        if not isinstance(sel, list):
            raise EvaluationInputError(f"selected_analysts 类型错误: {type(sel).__name__}")
        for a in sel:
            if not isinstance(a, str):
                raise EvaluationInputError(f"selected_analysts 含非字符串元素: {type(a).__name__}")

    # data_quality nested validation
    dq = report.get("data_quality")
    if dq is not None:
        if not isinstance(dq, dict):
            raise EvaluationInputError(f"data_quality 类型错误: {type(dq).__name__}")
        dq_sel = dq.get("selected_analysts")
        if dq_sel is not None:
            if not isinstance(dq_sel, list):
                raise EvaluationInputError(f"data_quality.selected_analysts 类型错误")
            for a in dq_sel:
                if not isinstance(a, str):
                    raise EvaluationInputError(f"data_quality.selected_analysts 含非字符串: {type(a).__name__}")
        analysts = dq.get("analysts")
        if analysts is not None:
            if not isinstance(analysts, list):
                raise EvaluationInputError(f"data_quality.analysts 类型错误")
            for a in analysts:
                if not isinstance(a, dict):
                    raise EvaluationInputError(f"data_quality.analysts 含非对象元素")
                role = a.get("role")
                if not isinstance(role, str):
                    raise EvaluationInputError(f"data_quality.analysts.role 非字符串: {type(role).__name__}")

    # run_metadata nested validation
    meta = report.get("run_metadata")
    if meta is not None:
        if not isinstance(meta, dict):
            raise EvaluationInputError(f"run_metadata 类型错误: {type(meta).__name__}")
        snap = meta.get("config_snapshot")
        if snap is not None and not isinstance(snap, dict):
            raise EvaluationInputError(f"config_snapshot 类型错误: {type(snap).__name__}")


# ── Evaluation result validation (for compare) ──


def validate_evaluation_result(result: Any, label: str = "评测结果") -> None:
    """Validate evaluation result using shared summarizers (single source of truth)."""
    from .grader import compute_full_metrics, validate_processed_review_shape

    if not isinstance(result, dict):
        raise EvaluationInputError(f"{label}: 顶层必须是 JSON 对象")
    required = {"schema_version", "grader_version", "suite_id", "suite_version",
                "suite_digest", "submission_id", "trials", "metrics"}
    missing = required - set(result)
    if missing:
        raise EvaluationInputError(f"{label}: 缺少字段 {sorted(missing)}")
    if not _is_int(result["schema_version"]) or result["schema_version"] != 1:
        raise EvaluationInputError(f"{label}: schema_version={result['schema_version']!r}")
    if not isinstance(result["grader_version"], str):
        raise EvaluationInputError(f"{label}: grader_version 须为字符串")
    if not isinstance(result["suite_digest"], str) or not result["suite_digest"]:
        raise EvaluationInputError(f"{label}: suite_digest 须为非空字符串")
    if not isinstance(result["submission_id"], str):
        raise EvaluationInputError(f"{label}: submission_id 须为字符串")

    trials = result["trials"]
    if not isinstance(trials, list):
        raise EvaluationInputError(f"{label}: trials 须为列表")
    m = result["metrics"]
    if not isinstance(m, dict):
        raise EvaluationInputError(f"{label}: metrics 须为对象")

    # Per-trial structure validation
    seen: set = set()
    for i, t in enumerate(trials):
        tctx = f"{label}.trials[{i}]"
        if not isinstance(t, dict):
            raise EvaluationInputError(f"{tctx}: 须为对象")
        for k in ("case_id", "trial_id", "status", "contract_pass", "checks"):
            if k not in t:
                raise EvaluationInputError(f"{tctx}: 缺少 {k}")
        if not isinstance(t["case_id"], str):
            raise EvaluationInputError(f"{tctx}: case_id 须为字符串")
        if not _is_int(t["trial_id"]):
            raise EvaluationInputError(f"{tctx}: trial_id 须为整数")
        key = (t["case_id"], t["trial_id"])
        if key in seen:
            raise EvaluationInputError(f"{tctx}: 重复 trial {key}")
        seen.add(key)
        if not isinstance(t["contract_pass"], bool):
            raise EvaluationInputError(f"{tctx}: contract_pass 须为布尔值")
        status = t["status"]
        if status not in ("completed", "failed", "invalid", "missing"):
            raise EvaluationInputError(f"{tctx}: status={status!r} 非法")

        checks = t.get("checks")
        if not isinstance(checks, list):
            raise EvaluationInputError(f"{tctx}: checks 须为列表")
        check_ids = set()
        for c in checks:
            if not isinstance(c, dict) or "check_id" not in c or "status" not in c:
                raise EvaluationInputError(f"{tctx}: 检查项结构不完整")
            cid = c["check_id"]
            if not isinstance(cid, str):
                raise EvaluationInputError(f"{tctx}: check_id={cid!r} 须为字符串")
            if cid in check_ids:
                raise EvaluationInputError(f"{tctx}: 检查 {cid} 重复")
            check_ids.add(cid)
            if c["status"] not in ("pass", "fail", "unknown", "not_applicable"):
                raise EvaluationInputError(f"{tctx}: 检查 {cid} 状态 {c['status']!r} 非法")
        extra_c = check_ids - FIXED_CHECK_IDS
        if extra_c:
            raise EvaluationInputError(f"{tctx}: 未知检查 {sorted(extra_c)}")

        if status == "completed":
            missing_c = FIXED_CHECK_IDS - check_ids
            if missing_c:
                raise EvaluationInputError(f"{tctx}: 缺少固定检查 {sorted(missing_c)}")
            applicable = [c for c in checks if c.get("status") != "not_applicable"]
            recomputed_pass = all(c.get("status") == "pass" for c in applicable) if applicable else False
            if t["contract_pass"] != recomputed_pass:
                raise EvaluationInputError(
                    f"{tctx}: contract_pass={t['contract_pass']} 与检查重算 {recomputed_pass} 不一致"
                )
            rev = t.get("review")
            if rev is None:
                raise EvaluationInputError(f"{tctx}: completed trial 缺少 review 对象")
        else:
            if t["contract_pass"] is not False:
                raise EvaluationInputError(f"{tctx}: 非 completed trial contract_pass 须为 false")
            if check_ids:
                raise EvaluationInputError(
                    f"{tctx}: 非 completed trial 不应有检查项（得到 {sorted(check_ids)}）"
                )

        # Trial observations: reuse the submission contract
        obs = t.get("observations")
        if obs is not None:
            if not isinstance(obs, dict):
                raise EvaluationInputError(f"{tctx}: observations 须为对象")
            _OBS_K = frozenset({"latency_ms", "input_tokens", "output_tokens", "cost_amount", "cost_currency"})
            unk = set(obs) - _OBS_K
            if unk:
                raise EvaluationInputError(f"{tctx}.observations: 未知字段 {sorted(unk)}")
            for kf in ("latency_ms", "cost_amount"):
                v = obs.get(kf)
                if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0 or not math.isfinite(v)):
                    raise EvaluationInputError(f"{tctx}.observations.{kf}={v!r}")
            for kf in ("input_tokens", "output_tokens"):
                v = obs.get(kf)
                if v is not None and (not _is_int(v) or v < 0):
                    raise EvaluationInputError(f"{tctx}.observations.{kf}={v!r}")
            ca, cc = obs.get("cost_amount"), obs.get("cost_currency")
            if (ca is None) != (cc is None):
                raise EvaluationInputError(f"{tctx}.observations: cost 须成对")
            if cc is not None and (not isinstance(cc, str) or len(cc) != 3 or cc != cc.upper() or not cc.isalpha()):
                raise EvaluationInputError(f"{tctx}.observations.cost_currency={cc!r}")

        # Review shape validation using shared validator
        rev = t.get("review")
        if rev is not None:
            validate_processed_review_shape(rev, f"{tctx}.review")

    # Shared full metrics recomputation and comparison
    exp = m.get("expected")
    if not _is_int(exp) or exp < 0:
        raise EvaluationInputError(f"{label}: metrics.expected={exp!r}")
    provided = m.get("provided")
    if not _is_int(provided) or provided < 0:
        raise EvaluationInputError(f"{label}: metrics.provided={provided!r}")
    if len(trials) != exp:
        raise EvaluationInputError(f"{label}: trials 数 {len(trials)} ≠ expected {exp}")
    actual_provided = sum(1 for t in trials if t.get("status") != "missing")
    if provided != actual_provided:
        raise EvaluationInputError(
            f"{label}: metrics.provided={provided} 与非遗漏 trial 数 {actual_provided} 不一致"
        )

    expected_metrics = compute_full_metrics(trials, exp, provided)

    for field, expected_val in expected_metrics.items():
        if field == "observations":
            continue
        actual_val = m.get(field)
        if expected_val is None:
            if actual_val is not None:
                raise EvaluationInputError(
                    f"{label}: metrics.{field}={actual_val!r} 但应为 null（零分母）"
                )
        elif isinstance(expected_val, float):
            if actual_val is None:
                raise EvaluationInputError(
                    f"{label}: metrics.{field} 为 null 但应非 null（期望 {expected_val}）"
                )
            if not isinstance(actual_val, (int, float)) or isinstance(actual_val, bool):
                raise EvaluationInputError(f"{label}: metrics.{field}={actual_val!r}")
            if not math.isclose(actual_val, expected_val, rel_tol=1e-9, abs_tol=1e-12):
                raise EvaluationInputError(
                    f"{label}: metrics.{field}={actual_val} 与重算 {expected_val} 不一致"
                )
        else:
            if isinstance(expected_val, int) and not _is_int(actual_val):
                raise EvaluationInputError(
                    f"{label}: metrics.{field}={actual_val!r} 须为整数（bool 不等价）"
                )
            if actual_val != expected_val:
                raise EvaluationInputError(
                    f"{label}: metrics.{field}={actual_val!r} 与重算 {expected_val!r} 不一致"
                )

    # Observations summary validation
    expected_obs = expected_metrics.get("observations", {})
    actual_obs = m.get("observations") or {}
    for field in ("latency_ms", "input_tokens", "output_tokens"):
        exp_info = expected_obs.get(field, {})
        act_info = actual_obs.get(field)
        if act_info is None:
            if exp_info.get("observed_count", 0) > 0:
                raise EvaluationInputError(f"{label}: observations.{field} 缺失但有观测值")
            continue
        if not isinstance(act_info, dict):
            raise EvaluationInputError(f"{label}: observations.{field} 须为对象")
        if act_info.get("observed_count") != exp_info.get("observed_count"):
            raise EvaluationInputError(
                f"{label}: observations.{field}.observed_count={act_info.get('observed_count')} 与重算 {exp_info.get('observed_count')} 不一致"
            )
        exp_mean = exp_info.get("mean")
        act_mean = act_info.get("mean")
        if exp_mean is None:
            if act_mean is not None:
                raise EvaluationInputError(f"{label}: observations.{field}.mean 应为 null")
        else:
            if act_mean is None:
                raise EvaluationInputError(
                    f"{label}: observations.{field}.mean 为 null 但有观测值（期望 {exp_mean}）"
                )
            elif not isinstance(act_mean, (int, float)) or isinstance(act_mean, bool):
                raise EvaluationInputError(f"{label}: observations.{field}.mean={act_mean!r} 须为数值")
            elif not math.isclose(act_mean, exp_mean, rel_tol=1e-9, abs_tol=1e-12):
                raise EvaluationInputError(
                    f"{label}: observations.{field}.mean={act_mean} 与重算 {exp_mean} 不一致"
                )
    exp_costs = expected_obs.get("cost_by_currency", {})
    act_costs = actual_obs.get("cost_by_currency")
    if act_costs is None:
        act_costs = {}
    if not isinstance(act_costs, dict):
        raise EvaluationInputError(f"{label}: observations.cost_by_currency 须为对象")
    for cur in set(exp_costs) | set(act_costs):
        exp_c = exp_costs.get(cur)
        act_c = act_costs.get(cur)
        if exp_c is None:
            raise EvaluationInputError(f"{label}: observations.cost_by_currency.{cur} 不应存在")
        elif act_c is None:
            raise EvaluationInputError(f"{label}: observations.cost_by_currency.{cur} 缺失")
        elif not isinstance(act_c, dict):
            raise EvaluationInputError(f"{label}: cost_by_currency.{cur} 须为对象")
        else:
            if not _is_int(act_c.get("observed_count")) or act_c.get("observed_count") != exp_c["observed_count"]:
                raise EvaluationInputError(f"{label}: cost_by_currency.{cur}.observed_count 不一致")
            exp_m = exp_c["mean"]
            act_m = act_c.get("mean")
            if exp_m is None:
                if act_m is not None:
                    raise EvaluationInputError(f"{label}: cost_by_currency.{cur}.mean 应为 null")
            elif act_m is None:
                raise EvaluationInputError(f"{label}: cost_by_currency.{cur}.mean 为 null 但有观测值")
            elif not isinstance(act_m, (int, float)) or isinstance(act_m, bool):
                raise EvaluationInputError(f"{label}: cost_by_currency.{cur}.mean 须为数值")
            elif not math.isclose(act_m, exp_m, rel_tol=1e-9, abs_tol=1e-12):
                raise EvaluationInputError(f"{label}: cost_by_currency.{cur}.mean={act_m} 与重算 {exp_m} 不一致")
