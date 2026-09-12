"""Research thesis card (C2): deterministic construction, validation, display.

The Portfolio Manager's EXISTING structured-output call produces a
:class:`~tradingagents.agents.schemas.PortfolioDecision` whose optional
thesis fields feed this module. Everything else is code-derived — the model
cannot fabricate it:

- instrument / formed_at / analysis_date / run_id come from run state;
- evidence references are validated against the C1 evidence bundle
  (dangling / published-after-cutoff / same-ID-in-both-lists conflicts);
- ``assessment_status`` is derived from OBSERVABLE gaps only (data-quality
  card, evidence source failures, reference checks, missing thesis parts) —
  a model's self-declared confidence never upgrades it;
- ``confidence`` is always ``uncalibrated`` (no out-of-sample calibration
  exists); no win-rate is displayed anywhere;
- invalidation conditions only count as auto-observable when all measurable
  parts (indicator/comparator/threshold/period) are present — otherwise
  they are marked 待人工判断 (manual review);
- free-text fallback (provider without structured output) yields a card
  with unknown model fields and an explicit reason — nothing invented.

First batch scope: generation + persistence + display only. No data
subscriptions, no trading, no external alerts, no auto-triggering.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from tradingagents.agents.report_quality import (
    STATUS_INSUFFICIENT,
    STATUS_LIMITED,
    is_structured as is_structured_quality,
)
from tradingagents.evidence.ledger import (
    SCHEMA_BUNDLE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    canonicalize_bundle,
    classify_reference,
)

SCHEMA_VERSION = 1

ASSESSABLE = "assessable"
LIMITED = "limited"
INSUFFICIENT = "insufficient"
UNKNOWN = "unknown"

CONFIDENCE_UNCALIBRATED = "uncalibrated"

_STATUS_TAG = "🧪 研究假设卡评估"

# Placeholder tokens a model may emit instead of a real value. A non-empty
# string is NOT a declared horizon / measurable condition part (Codex C2 R1).
_PLACEHOLDER_TOKENS = frozenset({
    "unknown", "未知", "n/a", "na", "none", "null", "-", "—", "待定", "?", "？",
})

MAX_ASSESSMENT_NOTES = 12


def _norm_placeholder(value) -> Optional[str]:
    """Normalize placeholder strings to None; keep real values verbatim."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s or s.lower() in _PLACEHOLDER_TOKENS:
        return None
    return s


def _valid_analysis_date(value) -> Optional[str]:
    """Return the analysis date when it is a real YYYY-MM-DD, else None."""
    from datetime import datetime as _dt

    s = value.strip() if isinstance(value, str) else ""
    if not s:
        return None
    try:
        return _dt.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def is_thesis_card(card: Any) -> bool:
    return (
        isinstance(card, dict)
        and card.get("schema_version") == SCHEMA_VERSION
        and card.get("assessment_status") in
        (ASSESSABLE, LIMITED, INSUFFICIENT, UNKNOWN)
    )


def _condition_out(cond) -> Dict[str, Any]:
    """Serialize a ThesisCondition and derive observability in code.

    Placeholder tokens ("unknown"/"未知"/"n/a"/…) count as MISSING, not as
    real values. Per the C2 contract an observable condition needs ALL
    measurable parts — indicator, comparator, threshold, period — plus the
    observation SOURCE where it will be verifiable; anything less is
    待人工判断 (manual review), never fabricated.
    """
    raw = {
        "indicator": getattr(cond, "indicator", None),
        "comparator": getattr(cond, "comparator", None),
        "threshold": getattr(cond, "threshold", None),
        "period": getattr(cond, "period", None),
        "source": getattr(cond, "source", None),
    }
    normalized = {k: _norm_placeholder(v) for k, v in raw.items()}
    out = {
        "description": getattr(cond, "description", "") or "",
        "indicator": normalized["indicator"],
        "comparator": normalized["comparator"],
        "threshold": normalized["threshold"],
        "period": normalized["period"],
        "source": normalized["source"],
        "evidence_id": getattr(cond, "evidence_id", None),
    }
    measurable = all(normalized[k] for k in ("indicator", "comparator", "threshold", "period", "source"))
    out["observable"] = measurable
    out["manual_review"] = not measurable
    return out


def _classify_refs_at(bundle, refs: List[str], cutoff: str) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {
        "valid": [], "dangling": [], "future": [], "unknown_time": [],
        "unverifiable_cutoff": [], "no_bundle": [], "empty": [],
    }
    for ref in refs or []:
        status = classify_reference(bundle, ref, cutoff)["status"]
        buckets.setdefault(status, []).append(ref)
    return {k: sorted(set(v)) for k, v in buckets.items()}


def _check_references(
    bundle: Optional[Dict[str, Any]],
    supporting: List[str],
    contradicting: List[str],
    condition_refs: List[str],
    cutoff: str,
) -> Dict[str, Any]:
    """Validate ALL provided evidence references (supporting / contradicting
    / condition positions) against the run's evidence bundle.

    Verdicts are structural (``classify_reference``), never parsed from
    reason strings. Condition references are optional — but once provided
    they are validated exactly like the others (Codex C2 R1). Flat combined
    lists exist for display/back-compat.
    """
    by_position = {
        "supporting": _classify_refs_at(bundle, supporting, cutoff),
        "contradicting": _classify_refs_at(bundle, contradicting, cutoff),
        "condition": _classify_refs_at(bundle, condition_refs, cutoff),
    }
    valid_support = set(by_position["supporting"]["valid"])
    valid_contra = set(by_position["contradicting"]["valid"])
    conflicting = sorted(valid_support & valid_contra)

    def flat(status: str) -> List[str]:
        merged = set()
        for pos in by_position.values():
            merged.update(pos[status])
        return sorted(merged)

    return {
        "by_position": by_position,
        "dangling": flat("dangling"),
        "future": flat("future"),
        "unknown_time": flat("unknown_time"),
        "unverifiable_cutoff": flat("unverifiable_cutoff"),
        "conflicting": conflicting,
        "valid_supporting": len(valid_support - valid_contra),
        "valid_contradicting": len(valid_contra - valid_support),
    }


def _derive_gaps(
    decision_like: bool,
    quality: Any,
    bundle: Optional[Dict[str, Any]],
    refs: Dict[str, Any],
    horizon: Optional[str],
    supporting_any: bool,
    conditions: List[Dict[str, Any]],
    analysis_date: Optional[str],
) -> tuple:
    """Structured observable gaps; returns ``(gaps, has_observable_inputs)``.

    Each gap is ``{"code": str, "detail": str}`` — classification is
    structural; the detail string is for humans/reports.
    """
    gaps: List[Dict[str, str]] = []
    quality_known = is_structured_quality(quality)
    has_inputs = quality_known or (isinstance(bundle, dict) and bundle.get("schema") == SCHEMA_BUNDLE)
    if not decision_like:
        gaps.append({"code": "freetext_output",
                     "detail": "本次运行未产出结构化假设卡（供应商不支持结构化输出或解析失败），模型字段为 unknown"})
        return gaps, has_inputs
    if not has_inputs:
        gaps.append({"code": "no_observable_inputs",
                     "detail": "无可观察的质量卡与证据账本输入（旧式运行），评估为 unknown"})
        return gaps, has_inputs

    if quality_known and quality.get("status") == STATUS_INSUFFICIENT:
        gaps.append({"code": "quality_insufficient",
                     "detail": f"数据质量卡状态为 {STATUS_INSUFFICIENT}（多数启用分析师报告未通过完整性检查）"})
    elif quality_known and quality.get("status") == STATUS_LIMITED:
        gaps.append({"code": "quality_limited",
                     "detail": "数据质量卡状态为 limited（部分报告未通过完整性检查）"})

    if analysis_date is None:
        gaps.append({"code": "analysis_date_unverifiable",
                     "detail": "缺少有效分析时点（trade_date 缺失/非法），引用时点合规性无法验证"})

    ref_labels = (("supporting", "支持"), ("contradicting", "反驳"), ("condition", "失效条件"))
    for status, label in (("dangling", "悬空"), ("future", "晚于分析时点"),
                          ("unknown_time", "发布时间未知"), ("unverifiable_cutoff", "时点未验证")):
        for position, zh in ref_labels:
            ids = refs["by_position"][position][status]
            if ids:
                gaps.append({
                    "code": f"{position}_ref_{status}",
                    "detail": f"{zh}证据引用{'失效条件引用' if position == 'condition' else ''}"
                              f"存在 {label}（{len(ids)} 个: {', '.join(ids[:5])}）",
                })
    if refs["conflicting"]:
        gaps.append({"code": "conflicting_refs",
                     "detail": f"同一证据 ID 同时列为支持与反驳（{len(refs['conflicting'])} 个冲突）"})

    if isinstance(bundle, dict) and bundle.get("schema") == SCHEMA_BUNDLE:
        bad_events = sorted(
            key for key, ev in (bundle.get("events") or {}).items()
            if isinstance(ev, dict) and ev.get("status") in (STATUS_FAILED, STATUS_PARTIAL, "unknown")
        )
        if bad_events:
            gaps.append({"code": "failed_evidence_event",
                         "detail": f"证据抓取事件存在失败/部分失败/未知状态（{', '.join(bad_events[:5])}）"})
        failed_sources = sorted({
            str(s.get("source")) for s in bundle.get("source_statuses") or []
            if s.get("status") in (STATUS_FAILED, STATUS_PARTIAL)
        })
        if failed_sources:
            gaps.append({"code": "failed_evidence_source",
                         "detail": f"证据来源存在失败/部分失败（{', '.join(failed_sources[:5])}）"})
        notes = [str(n) for n in bundle.get("coverage_notes") or []]
        for note in notes[:MAX_ASSESSMENT_NOTES]:
            gaps.append({"code": "evidence_coverage_note", "detail": f"证据覆盖限制：{note}"})
        if len(notes) > MAX_ASSESSMENT_NOTES:
            gaps.append({"code": "evidence_coverage_note",
                         "detail": f"另有 {len(notes) - MAX_ASSESSMENT_NOTES} 条证据覆盖限制未逐条列出"})

    if not supporting_any:
        gaps.append({"code": "no_valid_supporting_evidence",
                     "detail": "无任何通过校验的支持证据引用"})
    if horizon is None:
        gaps.append({"code": "horizon_placeholder",
                     "detail": "未声明有效预测期限（prediction_horizon 为空或 unknown/未知 占位）"})
    if not conditions:
        gaps.append({"code": "no_invalidation_condition",
                     "detail": "未给出任何失效条件"})
    else:
        manual = sum(1 for c in conditions if c["manual_review"])
        if manual:
            gaps.append({"code": "condition_manual_review",
                         "detail": f"{manual} 条失效条件缺少可观察要素（指标/比较符/阈值/期间/观察来源）或为占位值，标记待人工判断"})
    return gaps, has_inputs


def build_thesis_card(
    decision: Optional[Any],
    state: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the deterministic thesis card from the PM decision + run state.

    ``decision`` is the parsed ``PortfolioDecision`` when the structured
    call succeeded, ``None`` on free-text fallback.
    """
    run_meta = state.get("run_metadata") or {}
    quality = state.get("data_quality")
    bundle = canonicalize_bundle(state.get("evidence_bundle"))
    if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA_BUNDLE:
        bundle = None
    cutoff = str(state.get("trade_date") or "")
    # A card is only temporally anchored when the run has a VALID analysis
    # date — a placeholder/garbage/missing trade_date must not silently pass
    # temporal validation (Codex C2 R1).
    analysis_date = _valid_analysis_date(cutoff)

    decision_like = decision is not None
    supporting = list(getattr(decision, "supporting_evidence_ids", []) or []) if decision_like else []
    contradicting = list(getattr(decision, "contradicting_evidence_ids", []) or []) if decision_like else []
    conditions = [_condition_out(c) for c in (getattr(decision, "invalidation_conditions", []) or [])] if decision_like else []
    # Placeholder horizons ("unknown"/"未知"/…) are NOT declared horizons;
    # the raw text is preserved for audit, the KNOWN flag comes from the
    # normalized value.
    horizon_raw = (getattr(decision, "time_horizon", None) or "") if decision_like else ""
    horizon = _norm_placeholder(horizon_raw)
    condition_refs = [c["evidence_id"] for c in conditions if c.get("evidence_id")]
    refs = _check_references(bundle, supporting, contradicting, condition_refs, cutoff)
    gaps, has_inputs = _derive_gaps(
        decision_like, quality, bundle, refs,
        horizon=horizon,
        supporting_any=refs["valid_supporting"] > 0,
        conditions=conditions,
        analysis_date=analysis_date,
    )
    reasons = [g["detail"] for g in gaps]

    if not has_inputs:
        status = UNKNOWN
    elif is_structured_quality(quality) and quality.get("status") == STATUS_INSUFFICIENT:
        status = INSUFFICIENT
    elif reasons:
        status = LIMITED
    else:
        status = ASSESSABLE

    rating = getattr(decision, "rating", None) if decision_like else None
    return {
        "schema_version": SCHEMA_VERSION,
        "instrument": str(state.get("company_of_interest") or "") or "unknown",
        "formed_at": str(run_meta.get("created_at") or "") or str(cutoff or "") or "unknown",
        "analysis_date": analysis_date or "unknown",
        "run_id": str(run_meta.get("run_id") or "") or "unknown",
        "rating": str(getattr(rating, "value", rating) or "unknown"),
        "structured_output": decision_like,
        "prediction_horizon": horizon or "unknown",
        "prediction_horizon_raw": (horizon_raw or "unknown") if decision_like else "unknown",
        "main_thesis": (getattr(decision, "investment_thesis", "") or "") if decision_like else "",
        "supporting_evidence_ids": supporting,
        "contradicting_evidence_ids": contradicting,
        "hypotheses_to_verify": list(getattr(decision, "hypotheses_to_verify", []) or []) if decision_like else [],
        "catalysts": list(getattr(decision, "catalysts", []) or []) if decision_like else [],
        "invalidation_conditions": conditions,
        "next_check_trigger": (getattr(decision, "next_check_trigger", None) or "unknown") if decision_like else "unknown",
        "evidence_reference_check": refs,
        "assessment_status": status,
        "assessment_gaps": gaps,
        "assessment_reasons": reasons,
        # 模型自述信心不可校准；无样本外校准 → 永远明示 uncalibrated，
        # 任何出口不得展示投资胜率。
        "confidence": CONFIDENCE_UNCALIBRATED,
    }


def render_thesis_status_line(card: Any) -> str:
    """Deterministic one-line status for the decision text (idempotent tag)."""
    if not is_thesis_card(card):
        return ""
    status = card.get("assessment_status")
    label = {
        ASSESSABLE: "可评估（未发现可观察缺口）",
        LIMITED: "受限（存在可观察缺口）",
        INSUFFICIENT: "不足（数据质量不满足）",
        UNKNOWN: "未记录（无可观察输入）",
    }.get(status, status)
    reasons = card.get("assessment_reasons") or []
    lines = [
        f"{_STATUS_TAG}: {label}（`{status}`）；信心=uncalibrated（无样本外校准，不构成投资胜率）。",
    ]
    if reasons:
        lines.append("限制来源（可观察缺口）:")
        lines.extend(f"- {r}" for r in reasons)
    lines.append("假设卡详情见「研究假设卡」板块；失效条件含待人工判断项时不会自动触发。")
    return "\n".join(lines)


def append_thesis_status(decision_text: str, card: Any) -> str:
    """Append the deterministic thesis status to the final decision text.

    Idempotent on the CURRENT full status text (mirrors
    ``append_limitation_notice``): downstream consumers cannot wash the
    limited assessment out of the decision body.
    """
    status = render_thesis_status_line(card)
    if not status:
        return decision_text
    text = decision_text or ""
    if status in text:
        return decision_text
    if not text:
        return status
    return f"{text}\n\n{status}"


def render_thesis_md(card: Any) -> str:
    """Markdown block for Web / Markdown / PDF (三出口共用渲染).

    Old reports (no card) show 未记录 — nothing fabricated.
    """
    if not is_thesis_card(card):
        return (
            "**研究假设卡**: 未记录（此报告由旧版本生成，或组合经理未产出结构化结论）。\n"
            "（本卡为结论的可追溯结构化记录，不代表投资准确率；信心未校准）"
        )
    lines = [
        f"**研究假设卡**（标的: {card.get('instrument', '?')}；评级: {card.get('rating', '?')}；"
        f"形成: {card.get('formed_at', '?')}；期限: {card.get('prediction_horizon', '?')}）",
    ]
    if card.get("main_thesis"):
        lines.append(f"- 主要论点: {card['main_thesis'][:400]}")
    sup = card.get("supporting_evidence_ids") or []
    con = card.get("contradicting_evidence_ids") or []
    if sup or con:
        lines.append(f"- 支持证据: {', '.join(sup) if sup else '无'}")
        lines.append(f"- 反驳证据: {', '.join(con) if con else '无'}")
    chk = card.get("evidence_reference_check") or {}
    bad_ref = any(
        chk.get(k) for k in ("dangling", "future", "unknown_time", "unverifiable_cutoff", "conflicting")
    )
    if bad_ref:
        lines.append(
            f"- ⚠️ 引用校验: 悬空 {len(chk.get('dangling') or [])} / "
            f"晚于分析时点 {len(chk.get('future') or [])} / "
            f"发布时间未知 {len(chk.get('unknown_time') or [])} / "
            f"时点未验证 {len(chk.get('unverifiable_cutoff') or [])} / "
            f"支持反驳冲突 {len(chk.get('conflicting') or [])}（含失效条件引用）"
        )
    for key, title in (("hypotheses_to_verify", "待验证假设"), ("catalysts", "催化事件")):
        items = card.get(key) or []
        if items:
            lines.append(f"- {title}: " + "；".join(str(i) for i in items))
    conds = card.get("invalidation_conditions") or []
    if conds:
        lines.append("- 失效条件:")
        for c in conds:
            mark = "可观察" if c.get("observable") else "待人工判断"
            parts = [c.get("description", "")]
            detail = "；".join(
                f"{k}={c.get(k)}" for k in ("indicator", "comparator", "threshold", "period")
                if c.get(k)
            )
            if detail:
                parts.append(detail)
            lines.append(f"  - [{mark}] " + "（".join(p for p in parts if p) + ("）" if len(parts) > 1 else ""))
    if card.get("next_check_trigger") and card["next_check_trigger"] != "unknown":
        lines.append(f"- 下次检查触发点: {card['next_check_trigger']}")
    status = card.get("assessment_status")
    reasons = card.get("assessment_reasons") or []
    lines.append(f"- 评估状态: {status}" + ("；".join(f"{r}" for r in reasons) if reasons else "（未发现可观察缺口）"))
    lines.append("> 信心=uncalibrated（模型自述不可校准，无样本外校准）；本卡不代表投资准确率；"
                 "失效条件仅生成与展示，不订阅数据、不自动交易、不发送提醒。")
    return "\n".join(lines)
