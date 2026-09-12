"""Evidence section rendering shared by Web / Markdown / PDF (C1).

Old reports (no ``evidence_bundle``) show an explicit "未记录" notice —
nothing is fabricated. The section states fetch/filter facts only; it is
NOT an investment-accuracy claim.
"""

from __future__ import annotations

from typing import Any

from tradingagents.evidence.ledger import (
    SCHEMA_BUNDLE,
    canonicalize_bundle,
)

_STATUS_ICON = {"successful": "✓", "empty": "○", "failed": "✗", "partial": "◐"}


MAX_DISPLAY_RECORDS = 60
_EXCERPT_DISPLAY_CAP = 120


def _safe_url(url: Any) -> str:
    """Return the URL only when it is a real HTTP(S) link.

    Vendor quirks ("nan", empty, non-http schemes, control chars) render as
    链接未知 — an untrusted or malformed string must never be presented as a
    clickable/valid source link, and no URL is ever generated.
    """
    if not isinstance(url, str):
        return ""
    cleaned = url.strip()
    if cleaned.lower().startswith(("http://", "https://")) and " " not in cleaned:
        return cleaned
    return ""


def _availability_note(availability: Any) -> str:
    if availability == "publication_time_only":
        return "可得性: 仅发布时点（无原文快照）"
    if availability == "unknown":
        return "可得性: 未知"
    return ""


def render_evidence_md(bundle: Any) -> str:
    """Markdown block for the '数据来源与证据' section (Web/MD/PDF 共用).

    Every record is listed so an evidence_id resolves to its source article
    (title / source / publish time / original HTTP(S) link, missing parts
    marked 未知), with excerpt, retrieval time and availability limits —
    a cited ID stays inspectable from the report alone. Bounded; overflow
    stated. Old reports show 未记录, nothing fabricated.
    """
    data = canonicalize_bundle(bundle)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_BUNDLE:
        return (
            "**数据来源与证据**: 未记录来源证据（此报告由旧版本生成，"
            "或本次运行未调用支持证据采集的工具）。\n"
            "（本节是抓取与过滤的事实记录，不代表投资准确率）"
        )

    n_records = len(data.get("records") or [])
    n_events = len(data.get("events") or {})
    lines = [
        f"**数据来源与证据**: {n_records} 条证据记录 / {n_events} 次工具抓取"
        f"（run: `{data.get('run_id') or '未记录'}`）",
    ]

    records = data.get("records") or []
    if records:
        lines.append("- 证据记录（evidence_id → 来源文章；逐条可检查）:")
        for r in records[:MAX_DISPLAY_RECORDS]:
            pub = (r.get("published_at") or "")[:16].replace("T", " ")
            pub_display = pub if pub else "发布时间未知"
            source = r.get("source") or "来源未知"
            title = r.get("title") or "（无标题）"
            url = _safe_url(r.get("url"))
            retrieved = (r.get("retrieved_at") or "")[:16].replace("T", " ")
            retrieved_display = retrieved if retrieved else "采集时间未知"
            head = f"  - `{r.get('evidence_id', '?')}` {title}（来源: {source}，发布: {pub_display}，采集: {retrieved_display}）"
            lines.append(head)
            detail_parts = []
            excerpt = (r.get("excerpt") or "").strip()
            if excerpt:
                shown = excerpt[:_EXCERPT_DISPLAY_CAP]
                more = "…" if len(excerpt) > _EXCERPT_DISPLAY_CAP else ""
                detail_parts.append(f"摘要: {shown}{more}")
            avail = _availability_note(r.get("availability"))
            if avail:
                detail_parts.append(avail)
            detail_parts.append(f"链接: {url}" if url else "链接: 未知（未提供可信 HTTP(S) 地址）")
            lines.append("    - " + "；".join(detail_parts))
        if len(records) > MAX_DISPLAY_RECORDS:
            lines.append(
                f"  - …另有 {len(records) - MAX_DISPLAY_RECORDS} 条记录未列出"
                f"（共 {len(records)} 条，完整清单见运行 JSON）"
            )

    statuses = data.get("source_statuses") or []
    if statuses:
        lines.append("- 来源抓取状态:")
        for s in statuses:
            icon = _STATUS_ICON.get(s.get("status"), "?")
            event = s.get("event", "")
            suffix = f" [{event}]" if event else ""
            line = (
                f"  - {icon} {s.get('source', '?')}{suffix}: "
                f"{s.get('status', '?')}（{s.get('record_count', 0)} 条）"
            )
            if s.get("detail"):
                line += f" — {s['detail']}"
            lines.append(line)

    exclusions = data.get("exclusions") or {}
    if exclusions:
        lines.append("- 排除记录（未进入证据索引）:")
        lines.extend(
            f"  - {reason}: {count} 条" for reason, count in sorted(exclusions.items())
        )

    notes = data.get("coverage_notes") or []
    if notes:
        lines.append("- 覆盖说明:")
        lines.extend(f"  - {note}" for note in notes)

    lines.append("> 本节为抓取与过滤的事实记录（含失败与排除），不代表投资准确率。")
    return "\n".join(lines)
