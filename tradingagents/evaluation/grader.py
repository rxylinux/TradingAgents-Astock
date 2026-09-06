"""N04 deterministic report grading and suite evaluation."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from tradingagents.agents.quality_gate import REPORT_FIELDS
from tradingagents.agents.report_quality import (
    STATUS_LIMITED,
    STATUS_INSUFFICIENT,
    assess_reports,
    is_structured,
    render_limitation_notice,
)
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.run_records import is_run_metadata

from .schema import (
    GRADER_VERSION,
    EvaluationInputError,
    canonical_digest,
    parse_iso,
    validate_report_types,
    validate_submission,
    validate_suite,
)


def _safe_str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _check(cid: str, status: str, detail: str) -> dict:
    return {"check_id": cid, "status": status, "detail": detail}


def grade_report(case: dict, report: dict, review: Optional[dict] = None) -> dict:
    """Grade one report against one case; optionally process a review.

    Pure function: inputs are never mutated.
    """
    validate_report_types(report)
    checks: List[dict] = []
    ticker = _safe_str(report.get("company_of_interest") or report.get("ticker")).strip().upper()

    # 1. identity
    if ticker != case["ticker"].strip().upper():
        checks.append(_check("identity", "fail", f"标的 {ticker} ≠ 案例 {case['ticker']}"))
    elif _safe_str(report.get("trade_date")).strip() != case["trade_date"]:
        checks.append(_check("identity", "fail", f"日期 {report.get('trade_date')} ≠ 案例 {case['trade_date']}"))
    else:
        meta_type = None
        meta = report.get("run_metadata")
        if is_run_metadata(meta):
            meta_type = meta.get("instrument_type")
        top_type = report.get("instrument_type")
        known = [t for t in (meta_type, top_type) if t]
        if known and any(t != case["instrument_type"] for t in known):
            checks.append(_check("identity", "fail", f"类型冲突 {known} ≠ 案例 {case['instrument_type']}"))
        elif not known:
            # Missing type is unknown — never inferred from the case (Codex R7)
            checks.append(_check("identity", "unknown", "标的/日期一致但类型未声明"))
        else:
            checks.append(_check("identity", "pass", "标的/日期/类型一致"))

    # 2. team
    expected_team = set(case["selected_analysts"])
    sources = []
    top = report.get("selected_analysts")
    if isinstance(top, list) and top:
        sources.append(("顶层", set(top)))
    dq = report.get("data_quality")
    if is_structured(dq) and dq.get("selected_analysts"):
        sources.append(("质量卡", set(dq["selected_analysts"])))
    meta = report.get("run_metadata")
    if is_run_metadata(meta):
        snap = meta.get("config_snapshot") or {}
        st = snap.get("selected_analysts")
        if isinstance(st, list) and st:
            sources.append(("run_metadata", set(st)))
    if not sources:
        checks.append(_check("team", "unknown", "所有来源均未记录启用集合"))
    else:
        conflicts = [(n, s) for n, s in sources if s != expected_team]
        if conflicts:
            checks.append(_check("team", "fail",
                f"冲突: {[(n, sorted(s)) for n, s in conflicts]} ≠ 案例 {sorted(expected_team)}"))
        else:
            checks.append(_check("team", "pass", f"启用集合一致（{len(sources)} 个来源匹配）"))

    # 3. decision_present
    decision = _safe_str(report.get("final_trade_decision"))
    if decision.strip():
        checks.append(_check("decision_present", "pass", f"决策文本 {len(decision)} 字符"))
    else:
        checks.append(_check("decision_present", "fail", "最终决策为空或非字符串"))

    # 4. rating
    rating = parse_rating(decision, default="")
    if not rating:
        checks.append(_check("rating", "fail", "缺失评级（不默认 Hold）"))
    elif rating not in case["allowed_ratings"]:
        checks.append(_check("rating", "fail", f"评级 '{rating}' 不在允许集合"))
    else:
        checks.append(_check("rating", "pass", f"评级 '{rating}' 在允许集合内"))

    # 5. quality_status (recompute, never trust self-reported)
    recomputed = assess_reports(report, case["selected_analysts"])
    if recomputed["status"] == case["expected_quality_status"]:
        checks.append(_check("quality_status", "pass",
            f"重算 {recomputed['status']} = 预期"))
    else:
        checks.append(_check("quality_status", "fail",
            f"重算 {recomputed['status']} ≠ 预期 {case['expected_quality_status']}"))

    # 6. quality_record
    saved_dq = report.get("data_quality")
    if not is_structured(saved_dq):
        checks.append(_check("quality_record", "unknown", "报告未保存质量卡或格式无效"))
    elif saved_dq.get("status") == "unknown":
        checks.append(_check("quality_record", "unknown", "保存的质量卡状态为 unknown"))
    else:
        mismatch = _diff_quality_card(saved_dq, recomputed)
        if mismatch:
            checks.append(_check("quality_record", "fail", "; ".join(mismatch[:5])))
        else:
            checks.append(_check("quality_record", "pass", "保存的质量结构与重算一致"))

    # 7. limitation_notice
    if recomputed["status"] in (STATUS_LIMITED, STATUS_INSUFFICIENT):
        notice = render_limitation_notice(recomputed)
        if notice and notice in decision:
            checks.append(_check("limitation_notice", "pass", "受限提示完整存在"))
        else:
            checks.append(_check("limitation_notice", "fail", "缺少当前完整确定性提示"))
    else:
        checks.append(_check("limitation_notice", "not_applicable", f"状态 {recomputed['status']}，无需提示"))

    # 8. run_metadata
    meta = report.get("run_metadata")
    if not is_run_metadata(meta):
        checks.append(_check("run_metadata", "unknown", "旧报告缺有效运行档案"))
    else:
        issues = _check_run_metadata(meta, case)
        if issues:
            checks.append(_check("run_metadata", "fail", "; ".join(issues[:5])))
        else:
            checks.append(_check("run_metadata", "pass", f"run_id={meta['run_id'][:8]}"))

    applicable = [c for c in checks if c["status"] != "not_applicable"]
    contract_pass = all(c["status"] == "pass" for c in applicable)

    result: Dict[str, Any] = {
        "checks": checks,
        "contract_pass": contract_pass,
        "recomputed_quality": {
            "status": recomputed["status"],
            "active_count": recomputed["active_count"],
            "fail_count": recomputed["fail_count"],
        },
    }
    if is_run_metadata(meta):
        result["run_id"] = meta["run_id"]
        result["config_fingerprint"] = meta.get("config_fingerprint")

    # Process review if provided (Codex: grade_report must not ignore review)
    if review is not None:
        result["review"] = _process_review(case, report, review)

    return result


def _check_run_metadata(meta: dict, case: dict) -> List[str]:
    """Validate N02 run_metadata completeness and consistency with the case.

    Damaged structures (wrong types on required fields) are explicit failures,
    not repaired by defaults — a null snapshot with an empty-dict digest is
    not a valid archive.
    """
    issues = []
    # Type checks first: schema_version must be int 1 (not bool True == 1)
    sv = meta.get("schema_version")
    if isinstance(sv, bool) or not isinstance(sv, int) or sv != 1:
        issues.append(f"schema_version={sv!r} 须为整数 1（bool 不等价）")
    # config_snapshot must be a dict (null/list/string are all damaged)
    snap = meta.get("config_snapshot")
    if snap is not None and not isinstance(snap, dict):
        issues.append(f"config_snapshot 类型 {type(snap).__name__}，须为 dict")
    if issues:
        return issues

    # Required N02 fields must be present
    for field in ("schema_version", "run_id", "ticker", "trade_date",
                  "instrument_type", "created_at", "config_snapshot",
                  "config_fingerprint"):
        if field not in meta:
            issues.append(f"缺少 {field}")
    if not meta.get("created_at"):
        issues.append("缺少 created_at")
    else:
        # created_at must be a valid tz-aware ISO datetime
        ca = meta["created_at"]
        if not isinstance(ca, str):
            issues.append(f"created_at 非字符串: {type(ca).__name__}")
        else:
            from .schema import _valid_tz_time
            if not _valid_tz_time(ca):
                issues.append(f"created_at 无效时间: {ca!r}")
    if not meta.get("instrument_type"):
        issues.append("缺少 instrument_type")
    if issues:
        return issues

    if str(meta.get("ticker", "")).upper() != case["ticker"].upper():
        issues.append(f"ticker {meta.get('ticker')} ≠ {case['ticker']}")
    if str(meta.get("trade_date")) != case["trade_date"]:
        issues.append(f"date {meta.get('trade_date')} ≠ {case['trade_date']}")
    if meta.get("instrument_type") != case["instrument_type"]:
        issues.append(f"type {meta.get('instrument_type')} ≠ {case['instrument_type']}")

    snap = meta.get("config_snapshot")
    if snap is None:
        issues.append("config_snapshot 为 null（不是合法档案，不修复为空 dict）")
    elif not isinstance(snap, dict):
        issues.append(f"config_snapshot 非 dict: {type(snap).__name__}")
    else:
        actual_fp = canonical_digest(snap)
        if meta.get("config_fingerprint") != actual_fp:
            issues.append("指纹与快照不匹配")
    return issues


def _diff_quality_card(saved: dict, recomputed: dict) -> List[str]:
    issues = []
    if saved.get("status") != recomputed["status"]:
        issues.append(f"status {saved.get('status')} ≠ {recomputed['status']}")
    saved_set = set(saved.get("selected_analysts") or [])
    if saved_set != set(recomputed.get("selected_analysts") or []):
        issues.append(f"集合不匹配")
    if saved.get("active_count") != recomputed["active_count"]:
        issues.append(f"active_count {saved.get('active_count')} ≠ {recomputed['active_count']}")
    if saved.get("fail_count") != recomputed["fail_count"]:
        issues.append(f"fail_count {saved.get('fail_count')} ≠ {recomputed['fail_count']}")

    # Bijective role mapping check (extra/duplicate roles → fail)
    saved_analysts = saved.get("analysts") or []
    if not isinstance(saved_analysts, list):
        issues.append("analysts 非 list")
    else:
        saved_roles = [a.get("role") for a in saved_analysts if isinstance(a, dict)]
        if len(saved_roles) != len(set(saved_roles)):
            issues.append("analysts 存在重复角色")
        expected_roles = {a["role"] for a in recomputed.get("analysts") or []}
        if set(saved_roles) != expected_roles:
            issues.append(f"analysts 角色集不匹配: {sorted(set(saved_roles))} vs {sorted(expected_roles)}")
        saved_by_role = {a.get("role"): a for a in saved_analysts if isinstance(a, dict)}
        for ra in recomputed.get("analysts") or []:
            sa = saved_by_role.get(ra["role"])
            if sa is None:
                continue
            if sa.get("grade") != ra["grade"]:
                issues.append(f"{ra['role']}.grade {sa.get('grade')} ≠ {ra['grade']}")
            if sa.get("detail") != ra["detail"]:
                issues.append(f"{ra['role']}.detail 不匹配")
            if sa.get("chars") != ra["chars"]:
                issues.append(f"{ra['role']}.chars {sa.get('chars')} ≠ {ra['chars']}")

    saved_lim = sorted(saved.get("limitations") or [])
    if saved_lim != sorted(recomputed.get("limitations") or []):
        issues.append("limitations 不匹配")
    return issues


# ── Shared claims summarizer (used by evaluate AND compare import validation) ──


def summarize_review_claims(review: dict) -> dict:
    """Summarize a processed review dict from its claims.

    Returns the expected counts and rates. Used by both the evaluation engine
    and the compare-side import validation so there is exactly one formula.

    Assumes review is a dict with review_status='reviewed' and a valid claims
    list; for other statuses, all counts/rates are None.
    """
    status = review.get("review_status")
    if status != "reviewed":
        return {
            "supported": None, "contradicted": None, "unverifiable": None,
            "future_evidence": None, "evidence_backed_claims": None,
            "reviewed_claims_count": None,
            "factual_support_rate": None, "temporal_violation_rate": None,
        }
    claims = review.get("claims")
    if not isinstance(claims, list):
        claims = []
    if not claims:
        return {
            "supported": 0, "contradicted": 0, "unverifiable": 0,
            "future_evidence": 0, "evidence_backed_claims": 0,
            "reviewed_claims_count": 0,
            "factual_support_rate": None, "temporal_violation_rate": None,
        }
    total = len(claims)
    supported = sum(1 for c in claims if isinstance(c, dict) and c.get("verdict") == "supported" and not c.get("has_future_evidence", False))
    contradicted = sum(1 for c in claims if isinstance(c, dict) and c.get("verdict") == "contradicted")
    unverifiable = sum(1 for c in claims if isinstance(c, dict) and c.get("verdict") == "unverifiable")
    future = sum(1 for c in claims if isinstance(c, dict) and c.get("has_future_evidence", False))
    evidence_backed = sum(1 for c in claims if isinstance(c, dict) and c.get("evidence_ids"))
    return {
        "supported": supported, "contradicted": contradicted,
        "unverifiable": unverifiable, "future_evidence": future,
        "evidence_backed_claims": evidence_backed,
        "reviewed_claims_count": total,
        "factual_support_rate": supported / total if total > 0 else None,
        "temporal_violation_rate": future / evidence_backed if evidence_backed > 0 else None,
    }


def validate_processed_review_shape(review: Any, label: str) -> None:
    """Validate the shape of a processed review dict.

    Strictly checks each claim's required fields and types BEFORE any set
    membership or counting. Then recomputes counts/rates and requires exact
    match (null when zero denominator, non-null otherwise).
    """
    from .schema import EvaluationInputError, VALID_VERDICTS, _is_int

    if not isinstance(review, dict):
        raise EvaluationInputError(f"{label}: review 须为对象（得到 {type(review).__name__}）")
    status = review.get("review_status")
    if not isinstance(status, str) or status not in ("reviewed", "invalid", "unreviewed"):
        raise EvaluationInputError(f"{label}: review_status={status!r} 非法")

    claims = review.get("claims")
    if status == "reviewed":
        if not isinstance(claims, list):
            raise EvaluationInputError(f"{label}: reviewed 的 claims 须为列表（得到 {type(claims).__name__}）")
        seen_cids = set()
        for j, c in enumerate(claims):
            cctx = f"{label}.claims[{j}]"
            if not isinstance(c, dict):
                raise EvaluationInputError(f"{cctx}: 须为对象")
            # Required fields must exist and have correct types
            for req in ("claim_id", "field", "verdict", "evidence_ids", "has_future_evidence"):
                if req not in c:
                    raise EvaluationInputError(f"{cctx}: 缺少必填字段 {req}")
            cid_val = c["claim_id"]
            if not isinstance(cid_val, str) or not cid_val:
                raise EvaluationInputError(f"{cctx}: claim_id 须为非空字符串")
            if cid_val in seen_cids:
                raise EvaluationInputError(f"{cctx}: claim_id '{cid_val}' 重复")
            seen_cids.add(cid_val)
            field_val = c["field"]
            if not isinstance(field_val, str) or not field_val:
                raise EvaluationInputError(f"{cctx}: field 须为非空字符串")
            verdict = c["verdict"]
            if not isinstance(verdict, str) or verdict not in VALID_VERDICTS:
                raise EvaluationInputError(f"{cctx}: verdict={verdict!r} 非法")
            eids = c["evidence_ids"]
            if not isinstance(eids, list):
                raise EvaluationInputError(f"{cctx}: evidence_ids 须为列表")
            for e in eids:
                if not isinstance(e, str):
                    raise EvaluationInputError(f"{cctx}: evidence_ids 含非字符串元素 {type(e).__name__}")
            hfe = c["has_future_evidence"]
            if not isinstance(hfe, bool):
                raise EvaluationInputError(f"{cctx}: has_future_evidence={hfe!r} 须为布尔（不接受 null/int）")

        # Recompute and match counts/rates
        expected = summarize_review_claims(review)
        for count_field in ("supported", "contradicted", "unverifiable",
                            "future_evidence", "evidence_backed_claims"):
            actual = review.get(count_field)
            exp_val = expected[count_field]
            if not _is_int(actual) or actual != exp_val:
                raise EvaluationInputError(
                    f"{label}: {count_field}={actual!r} 与声明重算 {exp_val!r} 不一致（或非整数/bool）"
                )
        # Optional field: if present, must be exact non-bool int == len(claims)
        rcc = review.get("reviewed_claims_count")
        if rcc is not None:
            if not _is_int(rcc) or rcc != len(claims):
                raise EvaluationInputError(
                    f"{label}: reviewed_claims_count={rcc!r} 须为非 bool 整数且等于 len(claims)={len(claims)}"
                )
        for rate_field in ("factual_support_rate", "temporal_violation_rate"):
            actual = review.get(rate_field)
            exp_val = expected[rate_field]
            if exp_val is None:
                if actual is not None:
                    raise EvaluationInputError(
                        f"{label}: {rate_field}={actual} 但零分母应为 null"
                    )
            else:
                if actual is None:
                    raise EvaluationInputError(
                        f"{label}: {rate_field} 为 null 但分母非零（期望 {exp_val}）"
                    )
                if not isinstance(actual, (int, float)) or isinstance(actual, bool):
                    raise EvaluationInputError(f"{label}: {rate_field}={actual!r} 须为数值")
                import math as _math
                if not _math.isclose(actual, exp_val, rel_tol=1e-9, abs_tol=1e-12):
                    raise EvaluationInputError(
                        f"{label}: {rate_field}={actual} 与声明重算 {exp_val} 不一致"
                    )
    else:
        # invalid/unreviewed: claims must be empty, counts/rates must be null
        if claims is not None and claims != []:
            raise EvaluationInputError(
                f"{label}: {status} 的 claims 须为空（得到 {type(claims).__name__} len={len(claims) if isinstance(claims, list) else '?'}）"
            )
        for field in ("supported", "contradicted", "unverifiable", "future_evidence",
                      "evidence_backed_claims", "factual_support_rate", "temporal_violation_rate",
                      "reviewed_claims_count"):
            if review.get(field) is not None:
                raise EvaluationInputError(
                    f"{label}: {status} 的 {field} 须为 null（得到 {review.get(field)!r}）"
                )


# ── Shared full metrics summarizer (evaluate AND compare import validation) ──


def compute_full_metrics(trials: list, expected: int, provided: int) -> dict:
    """Compute all metrics from trials + counts. Single source of truth."""
    completed = sum(1 for t in trials if t.get("status") == "completed")
    failed = sum(1 for t in trials if t.get("status") == "failed")
    invalid = sum(1 for t in trials if t.get("status") == "invalid")
    missing = sum(1 for t in trials if t.get("status") == "missing")
    passed = sum(1 for t in trials if t.get("contract_pass"))
    unknown_checks = sum(
        1 for t in trials for c in t.get("checks", [])
        if isinstance(c, dict) and c.get("status") == "unknown"
    )

    reviewed_trials = invalid_reviews = unreviewed_trials = 0
    total_supported = total_contradicted = total_unverifiable = 0
    total_future = total_evidence_backed = total_claims = 0

    for t in trials:
        rev = t.get("review") or {}
        status = rev.get("review_status")
        if status == "reviewed":
            reviewed_trials += 1
            total_claims += len(rev.get("claims") or [])
            total_supported += rev.get("supported") or 0
            total_contradicted += rev.get("contradicted") or 0
            total_unverifiable += rev.get("unverifiable") or 0
            total_future += rev.get("future_evidence") or 0
            total_evidence_backed += rev.get("evidence_backed_claims") or 0
        elif status == "invalid":
            invalid_reviews += 1
        else:
            unreviewed_trials += 1

    return {
        "expected": expected, "provided": provided,
        "completed": completed, "failed": failed, "invalid": invalid,
        "missing": missing, "contract_passed": passed,
        "completion_rate": completed / expected if expected else None,
        "failure_rate": (failed + invalid + missing) / expected if expected else None,
        "contract_pass_rate": passed / expected if expected else None,
        "unknown_checks": unknown_checks,
        "reviewed_trials": reviewed_trials, "invalid_reviews": invalid_reviews,
        "unreviewed_trials": unreviewed_trials,
        "reviewed_claims": total_claims,
        "evidence_backed_claims": total_evidence_backed,
        "total_supported": total_supported, "total_contradicted": total_contradicted,
        "total_unverifiable": total_unverifiable, "total_future_evidence": total_future,
        "factual_support_rate": total_supported / total_claims if total_claims > 0 else None,
        "temporal_violation_rate": total_future / total_evidence_backed if total_evidence_backed > 0 else None,
        "observations": _summarize_observations(trials),
    }


# ── Review processing ──


def _review_invalid(reason: str) -> dict:
    return {"review_status": "invalid", "invalid_reason": reason,
            "claims": [], "supported": None, "contradicted": None,
            "unverifiable": None, "future_evidence": None,
            "evidence_backed_claims": None,
            "factual_support_rate": None, "temporal_violation_rate": None}


def _review_unreviewed() -> dict:
    return {"review_status": "unreviewed",
            "claims": [], "supported": None, "contradicted": None,
            "unverifiable": None, "future_evidence": None,
            "evidence_backed_claims": None,
            "factual_support_rate": None, "temporal_violation_rate": None}


def _process_review(case: dict, report: dict, review: Optional[dict]) -> dict:
    if review is None:
        return _review_unreviewed()

    if not isinstance(review, dict):
        return _review_invalid("review 非对象")
    if not review.get("claims"):
        # Empty claims list is valid but rates are null
        return {**_review_unreviewed(), "review_status": "reviewed",
                "supported": 0, "contradicted": 0, "unverifiable": 0,
                "future_evidence": 0, "evidence_backed_claims": 0}

    report_dig = canonical_digest(report)
    if review.get("report_digest") != report_dig:
        return _review_invalid("report_digest 不绑定当前报告")

    evidence = {e["evidence_id"]: e for e in case.get("evidence", [])}
    as_of = parse_iso(case["as_of"])

    invalid_reason = None
    processed = []
    for claim in review.get("claims", []):
        cid = claim.get("claim_id", "?")
        field_text = report.get(claim.get("field", ""))
        if not isinstance(field_text, str) or claim.get("quote", "") not in field_text:
            invalid_reason = f"claim {cid}: quote 不在该字段文本中"
            break
        unknown = [e for e in claim.get("evidence_ids", []) if e not in evidence]
        if unknown:
            invalid_reason = f"claim {cid}: 引用未知 evidence_id {unknown}"
            break

        has_future = False
        for eid in claim["evidence_ids"]:
            avail = parse_iso(evidence[eid]["available_at"])
            if avail > as_of:
                has_future = True
                break

        processed.append({
            "claim_id": cid, "field": claim["field"], "verdict": claim["verdict"],
            "evidence_ids": claim["evidence_ids"], "has_future_evidence": has_future,
        })

    if invalid_reason:
        return _review_invalid(invalid_reason)

    # Reuse the shared claims summarizer (single source of truth)
    summary = summarize_review_claims({"review_status": "reviewed", "claims": processed})
    return {
        "review_status": "reviewed",
        "claims": processed,
        **{k: v for k, v in summary.items()},
    }


# ── Suite evaluation ──


def evaluate_suite(suite: dict, submission: dict, *,
                   report_loader: Callable[[str], dict]) -> dict:
    validate_suite(suite)
    validate_submission(submission, suite)

    cases = {c["case_id"]: c for c in suite["cases"]}
    tpc = suite["trials_per_case"]
    run_map = {(r["case_id"], r["trial_id"]): r for r in submission["runs"]}

    trials = []
    for case in suite["cases"]:
        cid = case["case_id"]
        for tid in range(1, tpc + 1):
            key = (cid, tid)
            run = run_map.get(key)

            if run is None:
                trials.append({"case_id": cid, "trial_id": tid, "status": "missing",
                               "checks": [], "contract_pass": False,
                               "report_digest": None, "detail": "遗漏 trial"})
                continue

            if run["status"] == "failed":
                trials.append({"case_id": cid, "trial_id": tid, "status": "failed",
                               "checks": [], "contract_pass": False,
                               "report_digest": None, "detail": "运行失败",
                               "observations": run.get("observations")})
                continue

            # completed: load and validate report
            try:
                report = report_loader(run["report_path"])
                validate_report_types(report)
            except (EvaluationInputError, ValueError, UnicodeDecodeError, OSError) as e:
                # Per-trial invalid; do NOT catch KeyboardInterrupt/SystemExit
                trials.append({"case_id": cid, "trial_id": tid, "status": "invalid",
                               "checks": [], "contract_pass": False,
                               "report_digest": None,
                               "detail": f"报告不可用: {type(e).__name__}",
                               "observations": run.get("observations")})
                continue

            try:
                report_dig = canonical_digest(report)
                graded = grade_report(case, report)
            except (EvaluationInputError, TypeError, AttributeError, ValueError) as e:
                trials.append({"case_id": cid, "trial_id": tid, "status": "invalid",
                               "checks": [], "contract_pass": False,
                               "report_digest": None,
                               "detail": f"报告不可评分: {type(e).__name__}",
                               "observations": run.get("observations")})
                continue
            review_result = _process_review(case, report, run.get("review"))

            trial = {
                "case_id": cid, "trial_id": tid, "status": "completed",
                "report_digest": report_dig,
                "checks": graded["checks"],
                "contract_pass": graded["contract_pass"],
                "recomputed_quality": graded["recomputed_quality"],
                "review": review_result,
                "observations": run.get("observations"),
            }
            if "run_id" in graded:
                trial["run_id"] = graded["run_id"]
                trial["config_fingerprint"] = graded["config_fingerprint"]
            trials.append(trial)

    result = {
        "schema_version": 1,
        "grader_version": GRADER_VERSION,
        "suite_id": suite["suite_id"],
        "suite_version": suite["suite_version"],
        "suite_digest": submission["suite_digest"],
        "submission_id": submission["submission_id"],
        "trials": sorted(trials, key=lambda t: (t["case_id"], t["trial_id"])),
        "metrics": _compute_metrics(trials, suite, submission),
    }
    return result


def _compute_metrics(trials: List[dict], suite: dict, submission: dict) -> dict:
    expected = len(suite["cases"]) * suite["trials_per_case"]
    return compute_full_metrics(trials, expected, len(submission["runs"]))



def _summarize_observations(trials: List[dict]) -> dict:
    from collections import defaultdict

    summary: Dict[str, Any] = {}
    for field in ("latency_ms", "input_tokens", "output_tokens"):
        values = [
            t["observations"][field]
            for t in trials
            if t.get("observations") and t["observations"].get(field) is not None
        ]
        summary[field] = {
            "observed_count": len(values),
            "mean": (sum(values) / len(values)) if values else None,
        }
    costs: Dict[str, List[float]] = defaultdict(list)
    for t in trials:
        obs = t.get("observations")
        if obs and obs.get("cost_amount") is not None and obs.get("cost_currency"):
            costs[obs["cost_currency"]].append(obs["cost_amount"])
    summary["cost_by_currency"] = {
        cur: {"observed_count": len(v), "mean": sum(v) / len(v)}
        for cur, v in sorted(costs.items())
    }
    summary["_note"] = "导入测量值，非评分器实测"
    return summary
