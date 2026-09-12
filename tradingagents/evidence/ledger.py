"""Evidence ledger (C1): records, tool artifacts, deltas, and the state reducer.

Three payload shapes flow through the system:

1. **Artifact** (``SCHEMA_ARTIFACT``) — produced by the vendor layer from a
   single fetch: plain-text output + structured result (filtered records,
   per-source statuses, exclusions). Carried on ``ToolMessage.artifact`` via
   LangGraph's ``response_format="content_and_artifact"``.
2. **Delta** (``SCHEMA_DELTA``) — built by the branch-isolated tool node
   wrapper from *this execution's* ToolMessages. Events are keyed by
   ``{role}:{tool_call_id}`` for replay idempotency; the run identity comes
   from state (never from the model).
3. **Bundle** (``SCHEMA_BUNDLE``) — the accumulated ``AgentState
   .evidence_bundle`` value produced by :func:`evidence_reducer`. Immutable
   merge (inputs are never modified), deterministic ordering (order of
   concurrent branch writes cannot change the result), and mismatched
   ``run_id`` deltas are rejected so two concurrent runs cannot cross-
   contaminate.

Records are deduplicated by ``evidence_id`` (source + content digest). The
digest covers title, FULL content (before excerpt truncation), and the
normalized publish time — same title/summary at different event times is
NOT merged (draft bug), while the same article replayed or fetched by two
branches deduplicates to one record with both fetch events kept.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

# Schema markers — distinguish our artifacts from any foreign ToolMessage
# artifact, and version every payload/bundle for forward compatibility.
SCHEMA_ARTIFACT = "tradingagents.evidence/artifact/1"
SCHEMA_DELTA = "tradingagents.evidence/delta/1"
SCHEMA_BUNDLE = "tradingagents.evidence/bundle/1"

# Availability classification for each evidence record
AVAIL_PUBLICATION_ONLY = "publication_time_only"  # No snapshot; only pub time
AVAIL_UNKNOWN = "unknown"

# Time precision levels
PRECISION_DATE = "date"
PRECISION_DATETIME = "datetime"
PRECISION_UNKNOWN = "unknown"

EXCERPT_CAP = 500

# Valid artifact-level statuses
STATUS_SUCCESSFUL = "successful"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"
_VALID_STATUSES = frozenset({STATUS_SUCCESSFUL, STATUS_EMPTY, STATUS_FAILED, STATUS_PARTIAL})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_digest(title: str, content: str, published_at: str) -> str:
    """Stable SHA-256 over canonical JSON of the FULL content.

    Computed BEFORE excerpt truncation so a later re-fetch with longer
    content never collides with a truncated copy of itself.
    """
    payload = json.dumps(
        {"title": title, "content": content, "published_at": published_at},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_id(source: str, digest: str) -> str:
    return f"ev-{source}-{digest[:12]}"


def normalize_record(
    source: str,
    title: str,
    content: str = "",
    url: str = "",
    source_record_id: str = "",
    published_at: str = "",
    time_precision: str = PRECISION_UNKNOWN,
    timezone_name: str = "Asia/Shanghai",
    retrieved_at: Optional[str] = None,
    availability: str = AVAIL_PUBLICATION_ONLY,
) -> Dict[str, Any]:
    """Build one normalized evidence record dict (pure function).

    ``published_at`` must be a full ISO 8601 timestamp WITH offset (e.g.
    ``2024-11-05T11:37:00+08:00``) — the draft's ``time[:19]`` truncation
    lost the timezone and made cutoff comparison unreliable.
    """
    content = content or ""
    digest = _content_digest(title or "", content, published_at or "")
    return {
        "evidence_id": _evidence_id(source or "unknown", digest),
        "source": source or "unknown",
        "source_record_id": source_record_id or "",
        "title": title or "",
        "excerpt": content[:EXCERPT_CAP],
        "excerpt_truncated": len(content) > EXCERPT_CAP,
        "url": url or "",
        "published_at": published_at or "",
        "time_precision": time_precision or PRECISION_UNKNOWN,
        "timezone": timezone_name or "Asia/Shanghai",
        "retrieved_at": retrieved_at or _now_iso(),
        "content_digest": digest,
        "availability": availability or AVAIL_UNKNOWN,
    }


def _is_record(d: Any) -> bool:
    return (
        isinstance(d, dict)
        and isinstance(d.get("evidence_id"), str)
        and isinstance(d.get("content_digest"), str)
        and d.get("content_digest") != ""
    )


def normalize_artifact(artifact: Any, tool: str = "") -> Optional[Dict[str, Any]]:
    """Validate and normalize a ToolMessage artifact into a fetch payload.

    Returns ``None`` for anything that is not one of our artifacts (foreign
    artifact, None, non-dict) — the caller must treat that as "no evidence
    from this message", never guess.
    """
    if not isinstance(artifact, dict):
        return None
    if artifact.get("schema") != SCHEMA_ARTIFACT:
        return None
    status = artifact.get("status")
    if status not in _VALID_STATUSES:
        status = AVAIL_UNKNOWN
    records: List[Dict[str, Any]] = []
    for rec in artifact.get("records") or []:
        if _is_record(rec):
            records.append(dict(rec))
        elif isinstance(rec, dict):
            records.append(normalize_record(
                source=rec.get("source", ""),
                title=rec.get("title", ""),
                content=rec.get("content", ""),
                url=rec.get("url", ""),
                source_record_id=rec.get("source_record_id", ""),
                published_at=rec.get("published_at", ""),
                time_precision=rec.get("time_precision", PRECISION_UNKNOWN),
                retrieved_at=rec.get("retrieved_at") or None,
                availability=rec.get("availability", AVAIL_PUBLICATION_ONLY),
            ))
    return {
        "schema": SCHEMA_ARTIFACT,
        "tool": artifact.get("tool", tool or ""),
        "provenance": artifact.get("provenance", "unknown"),
        "status": status,
        "requested_window": artifact.get("requested_window"),
        "records": records,
        "source_statuses": [dict(s) for s in artifact.get("source_statuses") or [] if isinstance(s, dict)],
        "exclusions": {
            str(k): int(v) for k, v in (artifact.get("exclusions") or {}).items()
            if isinstance(v, int) and not isinstance(v, bool) and v > 0
        },
        "coverage_notes": [str(n) for n in artifact.get("coverage_notes") or []],
    }


def build_fetch_event(
    role: str,
    tool_call_id: str,
    artifact: Dict[str, Any],
    trade_date: str = "",
) -> Dict[str, Any]:
    """Build one fetch event (meta + normalized records) from an artifact.

    The event keeps per-invocation source statuses and exclusion counts;
    records are passed through so the reducer can dedupe them by
    ``evidence_id`` into the flat bundle record list.
    """
    payload = normalize_artifact(artifact)
    if payload is None:
        payload = normalize_artifact({
            "schema": SCHEMA_ARTIFACT, "tool": "", "provenance": "unknown",
            "status": AVAIL_UNKNOWN, "records": [], "source_statuses": [],
            "exclusions": {}, "coverage_notes": [],
        })
    meta = {
        "role": role,
        "tool": payload["tool"],
        "tool_call_id": tool_call_id,
        "provenance": payload["provenance"],
        "status": payload["status"],
        "requested_window": payload["requested_window"],
        "run_trade_date": trade_date or "",
        "record_count": len(payload["records"]),
        "evidence_ids": [r["evidence_id"] for r in payload["records"]],
        "source_statuses": payload["source_statuses"],
        "exclusions": payload["exclusions"],
        "coverage_notes": payload["coverage_notes"],
    }
    return {"event": meta, "records": payload["records"]}


def collect_tool_message_delta(
    messages: Iterable[Any],
    role: str,
    run_id: str = "",
    trade_date: str = "",
) -> Optional[Dict[str, Any]]:
    """Collect THIS execution's ToolMessage artifacts into one evidence delta.

    Only messages passed in are inspected (the branch-isolated tool node
    hands over exactly the ToolMessages produced by this invocation — never
    history). Artifacts without our schema marker, or messages without a
    ``tool_call_id`` (no stable identity → cannot dedupe on replay), are
    skipped. Returns ``None`` when nothing was collected.
    """
    events: Dict[str, Dict[str, Any]] = {}
    for m in messages or []:
        artifact = getattr(m, "artifact", None)
        if not isinstance(artifact, dict) or artifact.get("schema") != SCHEMA_ARTIFACT:
            continue
        tool_call_id = getattr(m, "tool_call_id", "") or ""
        if not tool_call_id:
            continue
        key = f"{role}:{tool_call_id}"
        # Same key within one batch: first occurrence wins (a repeated
        # tool_call_id in one execution is a protocol error, not new data).
        if key not in events:
            events[key] = build_fetch_event(role, tool_call_id, artifact, trade_date)
    if not events:
        return None
    return {"schema": SCHEMA_DELTA, "run_id": run_id or "", "events": events}


def _record_rank(rec: Dict[str, Any]) -> tuple:
    """Total order over records sharing an evidence_id.

    Earliest retrieval wins (first time this run observed the fact —
    independent of branch arrival order); remaining fields give a stable
    tiebreak so two merges in either order always pick the same record.
    """
    return (
        rec.get("retrieved_at", "") or "",
        rec.get("url", "") or "",
        rec.get("title", "") or "",
        rec.get("content_digest", "") or "",
    )


def evidence_reducer(old: Optional[Dict[str, Any]], delta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Merge an evidence delta into the accumulated bundle (LangGraph reducer).

    Contract (C1/Codex):

    - **Immutable**: neither ``old`` nor ``delta`` is ever modified.
    - **Idempotent**: an event keyed ``{role}:{tool_call_id}`` merges once;
      replaying the same delta (checkpoint resume, re-executed superstep)
      cannot double-count source stats or exclusions.
    - **Deterministic**: output ordering is canonical (events/records/
      statuses sorted), records sharing an ``evidence_id`` merge under a
      fixed rule (earliest ``retrieved_at``, stable tiebreak) — so merging
      concurrent branch deltas in either order yields identical bundles,
      even when the same article was fetched at different times.
    - **Run-isolated**: a delta whose ``run_id`` differs from the bundle's
      is rejected outright — two concurrent runs cannot cross-pollute, and
      a stale thread cannot absorb a new run's events.
    - **Conflict-flagging**: two records sharing an ``evidence_id`` with
      different content digests is a provenance anomaly — the canonical
      record is kept and a coverage note flags the conflict instead of
      letting arrival order decide the fact.
    - Only deltas carrying ``SCHEMA_DELTA`` are accepted; anything else
      (garbage writes) leaves the bundle untouched.
    """
    if delta is None:
        return old
    if not isinstance(delta, dict) or delta.get("schema") != SCHEMA_DELTA:
        return old

    # LangGraph's BinaryOperatorAggregate stores the FIRST write on an
    # empty channel without invoking the reducer — a bundle channel can
    # therefore hold a raw delta as its current value (single-write run,
    # checkpoint taken right after the first write). Bootstrap such a value
    # into a proper bundle before merging, so any write sequence converges
    # to the canonical shape.
    if isinstance(old, dict) and old.get("schema") == SCHEMA_DELTA:
        old = evidence_reducer(None, old)

    if old is None:
        run_id = delta.get("run_id") or ""
        events: Dict[str, Dict[str, Any]] = {}
        records: Dict[str, Dict[str, Any]] = {}
        digests: Dict[str, set] = {}
        statuses: List[Dict[str, Any]] = []
        exclusions: Dict[str, int] = {}
        notes: List[str] = []
    else:
        if not isinstance(old, dict) or old.get("schema") != SCHEMA_BUNDLE:
            return old
        if (old.get("run_id") or "") != (delta.get("run_id") or ""):
            return old  # mismatched run identity → reject the whole delta
        run_id = old.get("run_id") or ""
        events = dict(old.get("events") or {})
        records = {}
        digests = {}
        for r in old.get("records") or []:
            if _is_record(r):
                records[r["evidence_id"]] = dict(r)
                digests.setdefault(r["evidence_id"], set()).add(r["content_digest"])
        statuses = [dict(s) for s in old.get("source_statuses") or []]
        exclusions = {str(k): int(v) for k, v in (old.get("exclusions") or {}).items()}
        notes = list(old.get("coverage_notes") or [])

    incoming = delta.get("events") or {}
    if not isinstance(incoming, dict):
        return _finalize_bundle(run_id, events, records, digests, statuses, exclusions, notes)

    for key in sorted(incoming):
        if key in events:
            continue  # replay of an already-merged event → idempotent skip
        entry = incoming[key]
        if not isinstance(entry, dict) or "event" not in entry:
            continue
        meta = dict(entry["event"])
        meta["source_statuses"] = [dict(s) for s in meta.get("source_statuses") or []]
        meta["exclusions"] = dict(meta.get("exclusions") or {})
        meta["coverage_notes"] = list(meta.get("coverage_notes") or [])
        events[key] = meta
        for rec in entry.get("records") or []:
            if not _is_record(rec):
                continue
            rid = rec["evidence_id"]
            digests.setdefault(rid, set()).add(rec["content_digest"])
            current = records.get(rid)
            if current is None or _record_rank(rec) < _record_rank(current):
                records[rid] = dict(rec)
        for st in meta["source_statuses"]:
            tagged = dict(st)
            tagged["event"] = key
            statuses.append(tagged)
        for reason, count in meta["exclusions"].items():
            try:
                exclusions[str(reason)] = exclusions.get(str(reason), 0) + int(count)
            except (TypeError, ValueError):
                continue
        for note in meta["coverage_notes"]:
            if note not in notes:
                notes.append(note)

    return _finalize_bundle(run_id, events, records, digests, statuses, exclusions, notes)


def _finalize_bundle(
    run_id: str,
    events: Dict[str, Dict[str, Any]],
    records: Dict[str, Dict[str, Any]],
    digests: Dict[str, set],
    statuses: List[Dict[str, Any]],
    exclusions: Dict[str, int],
    notes: List[str],
) -> Dict[str, Any]:
    """Assemble the canonical bundle dict with stable ordering everywhere."""
    ordered_statuses = sorted(
        statuses,
        key=lambda s: (str(s.get("event", "")), str(s.get("source", "")), int(s.get("record_count", 0) or 0)),
    )
    conflict_ids = sorted(rid for rid, seen in digests.items() if len(seen) > 1)
    final_notes = list(notes)
    if conflict_ids:
        note = "证据 ID 冲突（同 ID 不同内容摘要，已保留规范记录并标记）: " + ", ".join(conflict_ids)
        if note not in final_notes:
            final_notes.append(note)
    return {
        "schema": SCHEMA_BUNDLE,
        "run_id": run_id,
        "events": {k: events[k] for k in sorted(events)},
        "records": [records[k] for k in sorted(records)],
        "source_statuses": ordered_statuses,
        "exclusions": {k: exclusions[k] for k in sorted(exclusions)},
        "coverage_notes": sorted(set(final_notes)),
    }


def empty_bundle(run_id: str = "") -> Dict[str, Any]:
    """Canonical empty bundle — used to seed fresh runs so the channel value
    is bundle-shaped from the very first write."""
    return {
        "schema": SCHEMA_BUNDLE,
        "run_id": run_id,
        "events": {},
        "records": [],
        "source_statuses": [],
        "exclusions": {},
        "coverage_notes": [],
    }


def canonicalize_bundle(value: Any) -> Any:
    """Coerce a channel value into the canonical bundle shape when possible.

    Passes through ``None`` (legacy/no evidence) and anything that is not
    ours; converts a raw delta (single first write stored by LangGraph
    without the reducer) into a bundle. Readers/savers use this so saved
    JSON always carries one stable schema.
    """
    if isinstance(value, dict) and value.get("schema") == SCHEMA_DELTA:
        return evidence_reducer(None, value)
    return value


def bundle_event_count(bundle: Optional[Dict[str, Any]]) -> int:
    if not isinstance(bundle, dict):
        return 0
    return len(bundle.get("events") or {})


def bundle_record_count(bundle: Optional[Dict[str, Any]]) -> int:
    if not isinstance(bundle, dict):
        return 0
    return len(bundle.get("records") or [])


_SHANGHAI_TZ = timezone(timedelta(hours=8))


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    iso = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    # Naive timestamps follow the vendor convention: Shanghai local time.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SHANGHAI_TZ)
    return dt


def _parse_cutoff(cutoff: str) -> Optional[datetime]:
    """Parse an analysis cutoff into an aware datetime.

    A date-only ``YYYY-MM-DD`` cutoff means end-of-day Shanghai time —
    the same day boundary the news vendor filter uses (A batch). An
    explicit timestamp is honored with its own offset (naive → Shanghai).
    Unparseable cutoffs return ``None`` (callers must not silently pass).
    """
    if not cutoff or not isinstance(cutoff, str):
        return None
    trimmed = cutoff.strip()
    try:
        day = datetime.strptime(trimmed, "%Y-%m-%d")
    except ValueError:
        return _parse_iso(trimmed)
    end_of_day = day + timedelta(days=1, microseconds=-1)
    return end_of_day.replace(tzinfo=_SHANGHAI_TZ)


def render_source_summary(bundle: Optional[Dict[str, Any]]) -> str:
    """Render a compact summary of sources and limits for report display."""
    if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA_BUNDLE:
        return ""
    lines: List[str] = []
    statuses = bundle.get("source_statuses") or []
    if statuses:
        lines.append("数据来源状态:")
        for s in statuses:
            icon = {
                STATUS_SUCCESSFUL: "✓", STATUS_EMPTY: "○",
                STATUS_FAILED: "✗", STATUS_PARTIAL: "◐",
            }.get(s.get("status"), "?")
            role = s.get("event", "")
            suffix = f" [{role}]" if role else ""
            lines.append(
                f"  {icon} {s.get('source', '?')}{suffix}: "
                f"{s.get('status', '?')} ({s.get('record_count', 0)} 条)"
            )
            if s.get("detail"):
                lines.append(f"    {s['detail']}")
    exclusions = bundle.get("exclusions") or {}
    if exclusions:
        lines.append("排除记录:")
        for reason in sorted(exclusions):
            lines.append(f"  {reason}: {exclusions[reason]} 条")
    notes = bundle.get("coverage_notes") or []
    if notes:
        lines.append("覆盖说明:")
        for note in notes:
            lines.append(f"  {note}")
    return "\n".join(lines)


def classify_reference(
    bundle: Optional[Dict[str, Any]],
    evidence_id: str,
    analysis_cutoff: str = "",
) -> Dict[str, str]:
    """Structured verdict for one evidence reference (additive helper).

    Companion to :func:`validate_reference` with the same time semantics
    (date-only cutoff = Shanghai end-of-day; naive → Shanghai) but a
    machine-readable status so callers never have to parse reason strings:

    - ``valid``               ID exists and is not published after the cutoff
    - ``dangling``            ID not in this run's bundle
    - ``unknown_time``        record exists but publish time is missing/unparseable
    - ``unverifiable_cutoff`` no cutoff given, or the cutoff is unparseable —
                              existence known, time-compliance NOT verified
    - ``no_bundle``           no evidence ledger in this run
    - ``empty``               empty reference
    """
    verdict = {"evidence_id": evidence_id or ""}
    if not evidence_id:
        verdict["status"] = "empty"
        return verdict
    if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA_BUNDLE:
        verdict["status"] = "no_bundle"
        return verdict
    record = next(
        (r for r in bundle.get("records") or [] if r.get("evidence_id") == evidence_id),
        None,
    )
    if record is None:
        verdict["status"] = "dangling"
        return verdict
    published_at = record.get("published_at", "")
    if not published_at:
        verdict["status"] = "unknown_time"
        return verdict
    pub = _parse_iso(published_at)
    if pub is None:
        verdict["status"] = "unknown_time"
        return verdict
    if not analysis_cutoff:
        verdict["status"] = "unverifiable_cutoff"
        return verdict
    cutoff = _parse_cutoff(analysis_cutoff)
    if cutoff is None:
        verdict["status"] = "unverifiable_cutoff"
        return verdict
    verdict["status"] = "future" if pub > cutoff else "valid"
    return verdict


def validate_reference(
    bundle: Optional[Dict[str, Any]],
    evidence_id: str,
    analysis_cutoff: str = "",
) -> Dict[str, Any]:
    """Check that an evidence reference is valid — a JOINT check.

    ``valid`` is True only when BOTH hold: the ID exists in this run's
    bundle AND (when a cutoff is requested) the record's publish time is
    verifiably not after the cutoff. Unknown or unparseable publish times
    therefore FAIL the joint check — they are never reported as
    time-compliant; an unparseable cutoff likewise fails rather than
    silently passing. Date-only cutoffs mean end-of-day Shanghai (same
    boundary as the vendor filter). "valid" does NOT mean the claim
    referencing the record is factually supported.
    """
    if not evidence_id:
        return {"valid": False, "reason": "空引用"}
    if not isinstance(bundle, dict):
        return {"valid": False, "reason": "本次运行无证据账本"}
    record = next(
        (r for r in bundle.get("records") or [] if r.get("evidence_id") == evidence_id),
        None,
    )
    if record is None:
        return {"valid": False, "reason": f"证据 {evidence_id} 不存在于本次运行"}
    if not analysis_cutoff:
        return {"valid": True, "reason": "ID 存在（未提供分析截止时间，时点未验证；引用有效≠声明受支持）"}
    published_at = record.get("published_at", "")
    if not published_at:
        return {"valid": False, "reason": f"证据 {evidence_id} 发布时间未知，无法验证时点合规"}
    pub = _parse_iso(published_at)
    if pub is None:
        return {"valid": False, "reason": f"证据 {evidence_id} 发布时间无法解析（{published_at}）"}
    cutoff = _parse_cutoff(analysis_cutoff)
    if cutoff is None:
        return {"valid": False, "reason": f"分析截止时间无法解析（{analysis_cutoff}），拒绝默认放行"}
    if pub > cutoff:
        return {
            "valid": False,
            "reason": f"证据 {evidence_id} 发布于分析截止之后（{published_at} > {analysis_cutoff}）",
        }
    return {"valid": True, "reason": "ID 存在且时点合规（引用有效≠声明受支持）"}
