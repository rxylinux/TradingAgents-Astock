"""Evidence prompt context for decision makers (C1).

Research Manager / Trader / Portfolio Manager receive a compact, bounded
evidence index from ``state["evidence_bundle"]`` so decisions can reference
records by ``evidence_id``. Mirrors ``report_quality.quality_context_for_prompt``:

- old states (no bundle) → empty string, nothing fabricated;
- only bundle-shaped records are listed (canonicalize tolerates the raw
  first-write delta shape);
- bounded: at most ``max_records`` entries, overflow stated explicitly;
- referencing rules stay honest: a valid reference means the ID exists and
  is time-compliant — it is NOT proof the claim is factually supported.
"""

from __future__ import annotations

from typing import Any, Dict, List

from tradingagents.evidence.ledger import (
    SCHEMA_BUNDLE,
    bundle_record_count,
    canonicalize_bundle,
)

MAX_PROMPT_RECORDS = 30
MAX_PROMPT_NOTES = 10


def _source_status_line(bundle: Dict[str, Any]) -> str:
    parts = []
    for s in bundle.get("source_statuses") or []:
        parts.append(f"{s.get('source', '?')}={s.get('status', '?')}({s.get('record_count', 0)})")
    return ", ".join(parts)


def evidence_context_for_prompt(state: Any, max_records: int = MAX_PROMPT_RECORDS) -> str:
    """Compact evidence index block for RM/Trader/PM prompts ('' if none).

    Coverage notes ride along even when there are no records — an
    old-vendor fetch (provenance unknown) must still surface its
    limitation to the decision makers, not silently vanish.
    """
    bundle = canonicalize_bundle(state.get("evidence_bundle") if isinstance(state, dict) else None)
    if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA_BUNDLE:
        return ""
    records: List[Dict[str, Any]] = bundle.get("records") or []
    notes = list(bundle.get("coverage_notes") or [])
    statuses = bundle.get("source_statuses") or []
    exclusions = bundle.get("exclusions") or {}
    # None of the fields below depend on records existing: a legacy/failed
    # vendor fetch with zero records must still surface its event-level
    # statuses, exclusions and coverage limitations to the decision makers.
    if not records and not notes and not statuses and not exclusions:
        return ""

    lines = [
        "证据索引（本次运行实际抓取并通过时点过滤的记录；决策引用事实时优先给出 evidence_id）:",
    ]
    shown = records[:max_records]
    for r in shown:
        pub = (r.get("published_at") or "时间未知")[:16].replace("T", " ")
        lines.append(
            f"- [{r['evidence_id']}] {r.get('title', '')[:80]}"
            f"（来源: {r.get('source', '?')}, 发布: {pub}）"
        )
    if len(records) > max_records:
        lines.append(f"- …另有 {len(records) - max_records} 条记录未列出（共 {len(records)} 条）")

    status_line = _source_status_line(bundle)
    if status_line:
        lines.append(f"来源抓取状态: {status_line}")
    if exclusions:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(exclusions.items()))
        lines.append(f"排除记录（未进入证据索引）: {parts}")
    if notes:
        lines.append("覆盖限制（决策必须纳入考量）:")
        lines.extend(f"- {n}" for n in notes[:MAX_PROMPT_NOTES])
        if len(notes) > MAX_PROMPT_NOTES:
            lines.append(f"- …另有 {len(notes) - MAX_PROMPT_NOTES} 条覆盖说明未列出")
    lines.append(
        "（引用 evidence_id 仅表示该记录存在于本次运行且发布时点不晚于分析时点；"
        "引用有效不等于该论断受证据支持。）"
    )
    return "\n".join(lines) + "\n"
