"""N04 evaluation comparison with per-check regression detection."""

from __future__ import annotations

import math
from typing import Any, Dict, List

from .schema import (
    EvaluationInputError,
    FIXED_CHECK_IDS,
    validate_evaluation_result,
)


def _key(t: dict) -> tuple:
    return (t["case_id"], t["trial_id"])


def _recompute_contract_pass(checks: list) -> bool:
    """contract_pass must match: all applicable checks are pass."""
    applicable = [c for c in checks if c.get("status") != "not_applicable"]
    return all(c.get("status") == "pass" for c in applicable) if applicable else False


def _validate_semantics(result: dict, label: str) -> None:
    """Recompute contract_pass, status counts, rates, and validate observations."""
    trials = result["trials"]
    m = result["metrics"]

    # Recompute contract_pass from checks
    for t in trials:
        if t["status"] == "completed":
            recomputed = _recompute_contract_pass(t["checks"])
            if t["contract_pass"] != recomputed:
                raise EvaluationInputError(
                    f"{label}: trial ({t['case_id']},{t['trial_id']}) contract_pass="
                    f"{t['contract_pass']} 与检查结果不符（应为 {recomputed}）"
                )
        else:
            # Non-completed must have empty checks and contract_pass=false
            if t["contract_pass"] is not False:
                raise EvaluationInputError(
                    f"{label}: 非 completed trial contract_pass 须为 false"
                )
            if t["checks"]:
                raise EvaluationInputError(
                    f"{label}: 非 completed trial 不应有检查项"
                )

    # Check for duplicate check IDs within each trial
    for t in trials:
        seen_cids = set()
        for c in t["checks"]:
            cid = c.get("check_id")
            if cid in seen_cids:
                raise EvaluationInputError(
                    f"{label}: trial ({t['case_id']},{t['trial_id']}) 检查 {cid} 重复"
                )
            seen_cids.add(cid)

    # Recompute status counts
    exp = m["expected"]
    completed = sum(1 for t in trials if t["status"] == "completed")
    failed = sum(1 for t in trials if t["status"] == "failed")
    invalid = sum(1 for t in trials if t["status"] == "invalid")
    missing = sum(1 for t in trials if t["status"] == "missing")
    passed = sum(1 for t in trials if t.get("contract_pass"))

    if m["completed"] != completed:
        raise EvaluationInputError(
            f"{label}: metrics.completed={m['completed']} 与实际 {completed} 不符"
        )
    if m["failed"] != failed:
        raise EvaluationInputError(
            f"{label}: metrics.failed={m['failed']} 与实际 {failed} 不符"
        )
    if m["contract_passed"] != passed:
        raise EvaluationInputError(
            f"{label}: metrics.contract_passed={m['contract_passed']} 与实际 {passed} 不符"
        )

    # Validate rates match counts
    if exp > 0:
        expected_completion = completed / exp
        expected_failure = (failed + invalid + missing) / exp
        expected_pass = passed / exp
        for rate_key, expected_val, count_desc in [
            ("completion_rate", expected_completion, f"completed={completed}/expected={exp}"),
            ("failure_rate", expected_failure, f"(failed+invalid+missing)/expected"),
            ("contract_pass_rate", expected_pass, f"contract_passed={passed}/expected={exp}"),
        ]:
            actual_val = m.get(rate_key)
            if actual_val is not None and not math.isclose(actual_val, expected_val, rel_tol=1e-9, abs_tol=1e-12):
                raise EvaluationInputError(
                    f"{label}: metrics.{rate_key}={actual_val} 与重算 {expected_val} 不符 ({count_desc})"
                )

    # Validate observations: no NaN/Infinity
    obs = m.get("observations") or {}
    for field in ("latency_ms", "input_tokens", "output_tokens"):
        info = obs.get(field)
        if isinstance(info, dict) and info.get("mean") is not None:
            if not math.isfinite(info["mean"]):
                raise EvaluationInputError(f"{label}: observations.{field}.mean 非有限数")
    for cur, info in (obs.get("cost_by_currency") or {}).items():
        if isinstance(info, dict) and info.get("mean") is not None:
            if not math.isfinite(info["mean"]):
                raise EvaluationInputError(f"{label}: observations.cost_by_currency.{cur}.mean 非有限数")
    # Check trial-level observations for NaN
    for t in trials:
        t_obs = t.get("observations")
        if isinstance(t_obs, dict):
            for k, v in t_obs.items():
                if isinstance(v, float) and not math.isfinite(v):
                    raise EvaluationInputError(
                        f"{label}: trial ({t['case_id']},{t['trial_id']}) observations.{k} 非有限数"
                    )


def compare_evaluations(baseline: dict, candidate: dict) -> dict:
    """Compare two evaluation results with full semantic validation."""
    validate_evaluation_result(baseline, "基线评测")
    validate_evaluation_result(candidate, "候选评测")
    _validate_semantics(baseline, "基线评测")
    _validate_semantics(candidate, "候选评测")

    for field in ("suite_digest", "grader_version"):
        if baseline.get(field) != candidate.get(field):
            raise EvaluationInputError(
                f"不可比较: {field} 不同 ({baseline.get(field)} vs {candidate.get(field)})"
            )

    b_trials = {_key(t): t for t in baseline["trials"]}
    c_trials = {_key(t): t for t in candidate["trials"]}
    if set(b_trials) != set(c_trials):
        raise EvaluationInputError("不可比较: trial 身份不一致")

    pairs = []
    improved = degraded = unchanged = both_fail = 0
    degraded_checks: List[dict] = []

    for key in sorted(b_trials):
        b = b_trials[key]
        c = c_trials[key]
        b_pass = b["contract_pass"]
        c_pass = c["contract_pass"]

        if c_pass and not b_pass:
            improved += 1
        elif b_pass and not c_pass:
            degraded += 1
        elif b_pass and c_pass:
            unchanged += 1
        else:
            both_fail += 1

        # Per-check regression scan on ALL trials (including missing → absent)
        b_checks = {ch["check_id"]: ch["status"] for ch in b.get("checks", [])}
        c_checks = {ch["check_id"]: ch["status"] for ch in c.get("checks", [])}
        for cid in sorted(FIXED_CHECK_IDS):
            bs = b_checks.get(cid)
            cs = c_checks.get(cid)
            if bs is None:
                continue
            if bs == "pass" and cs != "pass":
                degraded_checks.append({
                    "case_id": key[0], "trial_id": key[1], "check_id": cid,
                    "baseline_status": bs,
                    "candidate_status": cs if cs is not None else "absent",
                })

        pair_entry: Dict[str, Any] = {
            "case_id": key[0], "trial_id": key[1],
            "baseline_pass": b_pass, "candidate_pass": c_pass,
            "baseline_status": b.get("status"),
            "candidate_status": c.get("status"),
        }

        # Paired review: only when BOTH have non-null rates
        b_rev = b.get("review") or {}
        c_rev = c.get("review") or {}
        if (b_rev.get("review_status") == "reviewed" and
                c_rev.get("review_status") == "reviewed"):
            entry: Dict[str, Any] = {}
            b_sr = b_rev.get("factual_support_rate")
            c_sr = c_rev.get("factual_support_rate")
            if b_sr is not None and c_sr is not None:
                entry["baseline_support_rate"] = b_sr
                entry["candidate_support_rate"] = c_sr
            b_vr = b_rev.get("temporal_violation_rate")
            c_vr = c_rev.get("temporal_violation_rate")
            if b_vr is not None and c_vr is not None:
                entry["baseline_violation_rate"] = b_vr
                entry["candidate_violation_rate"] = c_vr
            if entry:
                pair_entry["review_comparison"] = entry

        # Paired cost (same currency, both present)
        b_obs = b.get("observations") or {}
        c_obs = c.get("observations") or {}
        b_cost, c_cost = b_obs.get("cost_amount"), c_obs.get("cost_amount")
        if (b_cost is not None and c_cost is not None and
                b_obs.get("cost_currency") == c_obs.get("cost_currency")):
            pair_entry["cost_comparison"] = {
                "currency": b_obs.get("cost_currency"),
                "baseline": b_cost, "candidate": c_cost,
            }

        pairs.append(pair_entry)

    # Paired review stats: only count when both non-null (not treating null as 0)
    support_improved = support_degraded = 0
    covered_support = 0
    for p in pairs:
        rc = p.get("review_comparison")
        if not rc:
            continue
        b_sr = rc.get("baseline_support_rate")
        c_sr = rc.get("candidate_support_rate")
        if b_sr is not None and c_sr is not None:
            covered_support += 1
            if c_sr > b_sr:
                support_improved += 1
            elif c_sr < b_sr:
                support_degraded += 1

    b_metrics = baseline.get("metrics", {})
    c_metrics = candidate.get("metrics", {})

    return {
        "schema_version": 1,
        "comparable": True,
        "suite_digest": baseline["suite_digest"],
        "grader_version": baseline["grader_version"],
        "baseline_submission": baseline["submission_id"],
        "candidate_submission": candidate["submission_id"],
        "summary": {
            "total_pairs": len(pairs),
            "improved": improved, "degraded": degraded,
            "unchanged": unchanged, "both_fail": both_fail,
        },
        "degraded_checks": degraded_checks,
        "baseline_metrics": {
            k: b_metrics.get(k)
            for k in ("contract_pass_rate", "completion_rate", "failure_rate",
                       "factual_support_rate", "temporal_violation_rate")
        },
        "candidate_metrics": {
            k: c_metrics.get(k)
            for k in ("contract_pass_rate", "completion_rate", "failure_rate",
                       "factual_support_rate", "temporal_violation_rate")
        },
        "paired_review": {
            "covered_trials": sum(1 for p in pairs if "review_comparison" in p),
            "support_covered_trials": covered_support,
            "support_improved": support_improved,
            "support_degraded": support_degraded,
        },
        "pairs": pairs,
    }
