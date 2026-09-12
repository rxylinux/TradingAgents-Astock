"""F1: offline immutable review records + strict AND as-of retrieval.

Authoritative contract: ``docs/F1_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md``
(ten rules + finalized decisions). Pure functions only — no IO in validation
or retrieval, no network, no models. The CLI reads one explicit user file.

Key semantics (rules 3/4/5/6/8):

- Unknown stays unknown: numbers are finite non-bool only; null never
  becomes 0; no default fees; ratings never imply returns or correctness.
- Maturity is explicit: "3-6 months" is descriptive text only — no date is
  invented; no automatic 30-trading-day expiry (``expired_unresolvable`` is
  an explicit status carrying a reason).
- Availability is a strict AND of FIVE as-of gates — decided, mature,
  observed, published, record-version-available — never OR; any unknown
  gate yields an explicit ``unverifiable_*`` exclusion, never a derivation.
- Time parsing is strict (mirrors C1/E): date or offset-bearing datetime,
  naive datetimes rejected, no ten-char truncation; date precision compares
  conservatively at Shanghai end-of-day.
- Overlap filtering only with an EXPLICIT caller-provided query window
  (closed interval, equal boundary counts); otherwise reported as
  ``overlap_filter=not_requested``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
RETRIEVAL_RANKING_VERSION = 1

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 2000
MAX_LIMIT = 1000

_SH_TZ = timezone(timedelta(hours=8))

# Exclusion reason codes (auditable, stable)
NOT_YET_MATURE = "not_yet_mature"
OVERDUE_UNRESOLVED = "overdue_unresolved"
UNKNOWN_MATURITY = "unknown_maturity"
PENDING_STATUS = "pending_status"
UNVERIFIABLE = "unverifiable_{}"          # .format(field)
LATER_DECISION = "decided_after_as_of"
OVERLAPPING_WINDOW = "overlapping_window"
WINDOW_UNKNOWN_OVERLAP = "window_unknown_overlap"

STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_EXPIRED_UNRESOLVABLE = "expired_unresolvable"

_DIGEST_EXCLUDE_FIELDS = ("record_digest",)


class RecordValidationError(ValueError):
    """Loading/validation rejected (deterministic error list). CLI exit 2."""


# ---------------------------------------------------------------------------
# Strict time parsing (rule 6)
# ---------------------------------------------------------------------------


def _parse_date_strict(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_datetime_strict(value: Any) -> Optional[Tuple[datetime, str]]:
    """Strict date-or-offset-datetime parse.

    Returns ``(aware_datetime, precision)`` or ``None``. Dates are mapped to
    Shanghai END-OF-DAY (conservative availability comparison — a date-only
    disclosure never gains an invented midnight). Naive datetimes, invalid
    tails and ten-char truncation are rejected outright.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    d = _parse_date_strict(s)
    if d is not None:
        return datetime(d.year, d.month, d.day, tzinfo=_SH_TZ) + \
            timedelta(days=1, microseconds=-1), "date"
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt, "datetime"


def _now_anchor(value: Any) -> Optional[datetime]:
    parsed = _parse_datetime_strict(value)
    return parsed[0] if parsed else None


# ---------------------------------------------------------------------------
# Numbers (rule 3): finite, non-bool only — unknown stays null
# ---------------------------------------------------------------------------


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _canonical_record_json(record: Dict[str, Any]) -> str:
    # allow_nan=False: NaN/Infinity are NOT valid JSON — an illegal value must
    # fail loudly here, never be masked into the digest by default=str.
    return json.dumps(record, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def compute_record_digest(record: Dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k not in _DIGEST_EXCLUDE_FIELDS}
    return "sha256:" + hashlib.sha256(
        _canonical_record_json(body).encode("utf-8")).hexdigest()


def _validate_time_field(record: Dict[str, Any], path: str, errors: List[str],
                         required: bool = False) -> None:
    value = record
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if value is None:
        if required:
            errors.append(f"{path}: 缺失（必填）")
        return
    if _parse_datetime_strict(value) is None:
        errors.append(f"{path}: 不是合法 YYYY-MM-DD 或带 offset 的 ISO datetime")


# ---------------------------------------------------------------------------
# Loading (rules 2/3/4/9): validation + digest + identity + deep isolation
# ---------------------------------------------------------------------------

_REQUIRED_TOP = ("record_id", "identity", "decision", "outcome")
_REQUIRED_IDENTITY = ("run_id", "ticker", "instrument_type", "window", "version")
_REQUIRED_DECISION = ("rating", "decided_at")


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_json_legal(value: Any, path: str, errors: List[str]) -> None:
    """Recursive legality check at the parse boundary (Codex F1 R2 #4).

    Everything json.loads accepts but canonical JSON must not: NaN/Infinity
    floats anywhere in the tree, non-string dict keys, undecodable strings.
    Collected as explicit RecordValidationError entries — never a later
    ValueError traceback from digest computation.
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            errors.append(f"{path}: 含无法 UTF-8 编码的字符串")
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            errors.append(f"{path}: 非有限数值（NaN/Infinity 拒绝）")
        return
    if isinstance(value, int):
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                errors.append(f"{path}: 字典键必须是字符串（{type(k).__name__}）")
                continue
            _check_json_legal(v, f"{path}.{k}", errors)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_json_legal(item, f"{path}[{i}]", errors)
        return
    errors.append(f"{path}: 非法类型 {type(value).__name__}（JSON 边界拒绝）")


def _snapshot_digest(snapshot: Any) -> Optional[str]:
    if snapshot is None:
        return None
    try:
        return "sha256:" + hashlib.sha256(
            _canonical_record_json(snapshot).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return None


RETURN_UNIT = "ratio:fraction"


def validate_record(record: Any, index: int) -> Dict[str, Any]:
    """Validate one record; returns an isolated deep copy or raises with a
    deterministic per-record error list appended to the shared one.

    Declared metadata is NOT validation evidence (Codex F1 R1): schema /
    ranking versions are compared against known constants; identity fields
    are type- and emptiness-checked; wrong-typed sub-objects reject instead
    of silently bypassing nested checks.
    """
    where = f"records[{index}]"
    if not isinstance(record, dict):
        raise RecordValidationError([f"{where}: 必须是 JSON 对象"])
    errors: List[str] = []
    _check_json_legal(record, where, errors)  # 解析边界合法性（含扩展字段）
    for key in _REQUIRED_TOP:
        if key not in record:
            errors.append(f"{where}: 缺少必填键 {key}")
    if errors:
        raise RecordValidationError(errors)
    sv = record.get("schema_version")
    if isinstance(sv, bool) or not isinstance(sv, int) or sv != SCHEMA_VERSION:
        errors.append(f"{where}.schema_version: 非法版本 {sv!r}"
                      f"（必须是已知整数 {SCHEMA_VERSION}；bool/浮点拒绝）")
    if not _nonempty_str(record.get("record_id")):
        errors.append(f"{where}.record_id: 必须是非空字符串")

    identity = record.get("identity")
    if not isinstance(identity, dict):
        errors.append(f"{where}.identity: 必须是对象")
        identity = {}
    for key in _REQUIRED_IDENTITY:
        if key not in identity:
            errors.append(f"{where}.identity: 缺少 {key}")
    if not _nonempty_str(identity.get("run_id")):
        errors.append(f"{where}.identity.run_id: 必须是非空字符串")
    if not _nonempty_str(identity.get("ticker")):
        errors.append(f"{where}.identity.ticker: 必须是非空字符串")
    window = identity.get("window")
    ws = we = None
    if not isinstance(window, dict):
        errors.append(f"{where}.identity.window: 必须是对象（{type(window).__name__}）")
    else:
        ws = _parse_date_strict(window.get("start"))
        we = _parse_date_strict(window.get("end"))
        if ws is None or we is None:
            errors.append(f"{where}.identity.window: start/end 必须是 YYYY-MM-DD")
        elif ws > we:
            errors.append(f"{where}.identity.window: start > end")
    if not isinstance(identity.get("version"), int) or isinstance(identity.get("version"), bool):
        errors.append(f"{where}.identity.version: 必须是整数（非 bool）")
    if identity.get("instrument_type") not in ("stock", "index"):
        errors.append(f"{where}.identity.instrument_type: 必须是 stock 或 index")
    meta = record.get("retrieval_meta")
    if meta is not None:
        if not isinstance(meta, dict):
            errors.append(f"{where}.retrieval_meta: 必须是对象")
        else:
            rv = meta.get("ranking_version")
            if rv is not None and (isinstance(rv, bool) or rv != RETRIEVAL_RANKING_VERSION):
                errors.append(f"{where}.retrieval_meta.ranking_version: 未知 {rv!r}"
                              f"（实现支持 {RETRIEVAL_RANKING_VERSION}）")

    decision = record.get("decision")
    if not isinstance(decision, dict):
        errors.append(f"{where}.decision: 必须是对象")
        decision = {}
    for key in _REQUIRED_DECISION:
        if not decision.get(key):
            errors.append(f"{where}.decision: 缺少 {key}")
    _validate_time_field(record, "decision.decided_at", errors, required=True)

    outcome = record.get("outcome")
    if not isinstance(outcome, dict):
        errors.append(f"{where}.outcome: 必须是对象")
        outcome = {}
    status = outcome.get("status")
    if status not in (STATUS_PENDING, STATUS_RESOLVED, STATUS_EXPIRED_UNRESOLVABLE):
        errors.append(f"{where}.outcome.status: 必须是 pending/resolved/expired_unresolvable")
    if status == STATUS_EXPIRED_UNRESOLVABLE and not outcome.get("reason"):
        errors.append(f"{where}.outcome: expired_unresolvable 必须显式携带 reason")
    maturity = _parse_date_strict(outcome.get("maturity_date"))
    if outcome.get("maturity_date") is not None and maturity is None:
        errors.append(f"{where}.outcome.maturity_date: 非法日期")
    if maturity is not None and we is not None and maturity < we:
        errors.append(f"{where}.outcome.maturity_date: 到期早于结果窗口结束")
    for field in ("observed_at", "publication_time"):
        _validate_time_field(record, f"outcome.{field}", errors)
    if outcome.get("observed_at") is not None and status == STATUS_PENDING:
        errors.append(f"{where}.outcome.observed_at: pending 不应带观察时间")
    for field in ("raw_return", "alpha_return", "max_adverse_excursion"):
        value = outcome.get(field)
        if value is not None:
            if _finite_number(value) is None:
                errors.append(f"{where}.outcome.{field}: 数值必须有限且非 bool（未知保持 null）")
            elif abs(value) > 1e6:
                errors.append(f"{where}.outcome.{field}: 超出合理量级")
    fees = outcome.get("fees_assumption")
    if fees is not None:
        bps = fees.get("bps") if isinstance(fees, dict) else None
        if not isinstance(fees, dict) or _finite_number(bps) is None:
            errors.append(f"{where}.outcome.fees_assumption: 必须是 {{bps: 有限非 bool 数值}} "
                          "或 null（无默认费率；NaN/Infinity 拒绝）")
    unit = outcome.get("return_unit")
    if unit is not None and unit != RETURN_UNIT:
        errors.append(f"{where}.outcome.return_unit: 冲突单位 {unit!r}"
                      f"（收益固定规范 {RETURN_UNIT}）")
    expires = outcome.get("event_expires_at")
    if expires is not None and _parse_date_strict(expires) is None:
        errors.append(f"{where}.outcome.event_expires_at: 非法日期")

    annotations = record.get("annotations")
    if annotations is None:
        annotations = {}
    if not isinstance(annotations, dict):
        errors.append(f"{where}.annotations: 必须是对象")
        annotations = {}
    has_annotation = (annotations.get("error_type") is not None
                      or bool(annotations.get("correct_risk_flags")))
    if has_annotation:
        source = annotations.get("source")
        when = annotations.get("annotated_at")
        if not _nonempty_str(source) or _parse_datetime_strict(when) is None:
            errors.append(f"{where}.annotations: 显式标注必须带来源与标注时间")
        elif (_parse_datetime_strict(record.get("record_available_at")) is not None
              and _parse_datetime_strict(when)[0]
              > _parse_datetime_strict(record.get("record_available_at"))[0]):
            # 版本一致性（Codex F1 R1 #3）：标注晚于版本可用时间 = 矛盾输入——
            # 未来标注不得借旧版本进入历史检索。
            errors.append(f"{where}.annotations.annotated_at: 晚于 record_available_at"
                          "（矛盾：该版本尚不可用时不存在此标注）")
    if not isinstance(annotations.get("correct_risk_flags") or [], list):
        errors.append(f"{where}.annotations.correct_risk_flags: 必须是列表")

    _validate_time_field(record, "record_available_at", errors)
    if record.get("availability_source") not in (None, "declared_only", "verified_snapshot"):
        errors.append(f"{where}.availability_source: 必须是 declared_only/verified_snapshot 或缺省")

    # External digests (rule 9): unverifiable without a snapshot — but the
    # field shape must still be explicit.
    decision_d = record.get("decision")
    if isinstance(decision_d, dict):
        for field in ("thesis_digest", "evidence_version"):
            value = decision_d.get(field)
            if value is None:
                continue
            if isinstance(value, dict):
                verification = value.get("verification")
                if verification not in (None, "external_unverified", "verified_snapshot"):
                    errors.append(f"{where}.decision.{field}.verification: 非法值")
                snapshot = value.get("snapshot")
                if snapshot is not None:
                    # 契约 #9（Codex F1 R2 #3）：**提供快照即复算**——是否验证
                    # 不由调用方标签决定；verification 缺失/external_unverified
                    # 时带快照同样必须摘要一致（否则拒绝），且 verification
                    # 统一规范化为 external_unverified 或 verified_snapshot。
                    declared = value.get("digest")
                    recomputed = _snapshot_digest(snapshot)
                    if not isinstance(declared, str) or declared != recomputed:
                        errors.append(
                            f"{where}.decision.{field}: 快照摘要失配"
                            f"（声称 {declared!r}，复算 {recomputed!r}；"
                            "有快照即必须复算，与自述标签无关）")
                elif verification == "verified_snapshot":
                    errors.append(f"{where}.decision.{field}: verified_snapshot 必须携带原始快照")
            elif field == "evidence_version" and not isinstance(value, dict):
                errors.append(f"{where}.decision.{field}: 必须是对象（digest/event_count）")

    if errors:
        raise RecordValidationError(errors)
    return json.loads(json.dumps(record, ensure_ascii=False))  # isolated copy


class VerifiedRecords(list):
    """Validated record list + non-polluting load metadata (duplicates)."""

    def __init__(self, items):
        super().__init__(items)
        self.duplicates: List[str] = []


def load_records(source: Any) -> List[Dict[str, Any]]:
    """Load + validate + digest-verify + identity-dedup. No input mutation.

    ``source``: parsed list (already isolated upstream), or a JSONL **text**
    string. Duplicate record_id with IDENTICAL content dedups (reported via
    the returned records' ``_load_duplicates`` note on the first copy);
    same id with DIFFERENT content rejects the whole load (exit-2 class).
    """
    if isinstance(source, str):
        if len(source.encode("utf-8")) > MAX_FILE_BYTES:
            raise RecordValidationError(
                [f"输入文本超过 {MAX_FILE_BYTES} 字节上限（入口保护，非仅 CLI 末端）"])
        records: List[Any] = []
        for i, line in enumerate(source.splitlines()):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RecordValidationError(
                    [f"line {i + 1}: JSON 解析失败（{exc}）"]) from exc
    elif isinstance(source, list):
        records = source
    else:
        raise RecordValidationError(["输入必须是 JSONL 文本或已解析列表"])
    if len(records) > MAX_RECORDS:
        raise RecordValidationError(
            [f"原始条目 {len(records)} 超过 {MAX_RECORDS} 上限（去重前入口检查）"])

    validated: List[Dict[str, Any]] = []
    by_id: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    duplicates: List[str] = []
    for i, raw in enumerate(records):
        record = validate_record(raw, i)
        # Digest check (rule 2/9): recompute over full record minus self.
        claimed = record.get("record_digest")
        recomputed = compute_record_digest(record)
        if claimed is not None and claimed != recomputed:
            raise RecordValidationError(
                [f"records[{i}] {record.get('record_id')}: record_digest 失配"
                 f"（声称 {claimed}，复算 {recomputed}）"])
        record["record_digest"] = recomputed
        rid = record["record_id"]
        if rid in by_id:
            prev_digest, _prev = by_id[rid]
            if prev_digest != recomputed:
                raise RecordValidationError(
                    [f"相同 record_id {rid!r} 内容不同——拒绝隐式覆盖"])
            duplicates.append(rid)
            continue
        by_id[rid] = (recomputed, record)
        validated.append(record)
    # 去重说明放外层元数据（list 子类属性）——绝不住后污染已被摘要覆盖的
    # 记录本体，保证 load 输出自洽可再次验证（Codex F1 R1 #5）。
    out = VerifiedRecords(validated)
    out.duplicates = sorted(set(duplicates))
    return out


# ---------------------------------------------------------------------------
# Retrieval (rules 5/6/7/8): strict AND gates, explicit overlap, v1 ranking
# ---------------------------------------------------------------------------


def _gate(record: Dict[str, Any], path: str) -> Tuple[Optional[datetime], bool]:
    value = record
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if value is None:
        return None, False
    parsed = _parse_datetime_strict(value)
    if parsed is None:
        return None, False
    return parsed[0], True


def retrieve_as_of(records: List[Dict[str, Any]], *, ticker: str,
                   instrument_type: str, as_of: str, limit: int,
                   ranking_version: int = RETRIEVAL_RANKING_VERSION,
                   query_window=None) -> Dict[str, Any]:
    """Pure as-of retrieval with the strict AND availability gate.

    Gates (ALL must pass; any unknown → explicit unverifiable_<field>):
    decided_at, outcome.maturity_date, outcome.observed_at,
    outcome.publication_time, record_available_at. Pending records never
    enter verified experience (not_yet_mature / overdue_unresolved /
    unknown_maturity). Overlap exclusion ONLY with an explicit closed-interval
    ``query_window`` (equal boundary counts); otherwise
    ``overlap_filter=not_requested``. v1 ranking: same ticker > same type >
    decided_at desc > record_id asc; limit truncation reported separately.
    """
    if (isinstance(ranking_version, bool) or not isinstance(ranking_version, int)
            or ranking_version != RETRIEVAL_RANKING_VERSION):
        raise RecordValidationError(
            f"非法 ranking_version={ranking_version!r}"
            f"（必须是已知整数 {RETRIEVAL_RANKING_VERSION}；bool/浮点拒绝）")
    if not isinstance(ticker, str) or not ticker.strip():
        raise RecordValidationError("ticker 必须是非空字符串")
    if instrument_type not in ("stock", "index"):
        raise RecordValidationError("instrument_type 必须是 stock 或 index")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise RecordValidationError("limit 必须是有限正整数")
    if limit > MAX_LIMIT:
        raise RecordValidationError(f"limit 超过实际上限 {MAX_LIMIT}")
    as_of_dt = _now_anchor(as_of)
    if as_of_dt is None:
        raise RecordValidationError(f"as_of 非法：{as_of!r}")
    # 入口验证（Codex F1 R1 相邻项）：纯 API 不信任任意列表——逐条复验
    # 结构与摘要，防止调用方改写 record 后绕过加载校验。
    verified = load_records(list(records))

    q_start = q_end = None
    overlap_mode = "not_requested"
    if query_window is not None:
        if (not isinstance(query_window, dict)
                or _parse_date_strict(query_window.get("start")) is None
                or _parse_date_strict(query_window.get("end")) is None):
            raise RecordValidationError("query_window 必须是 {start,end} 合法日期")
        q_start = _parse_date_strict(query_window["start"])
        q_end = _parse_date_strict(query_window["end"])
        if q_start > q_end:
            raise RecordValidationError("query_window: start > end")
        overlap_mode = "explicit_closed_interval"

    eligible: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    for record in verified:
        rid = record.get("record_id", "?")
        identity = record.get("identity") or {}
        outcome = record.get("outcome") or {}

        def _exclude(reason: str) -> None:
            excluded.append({"record_id": rid, "reason": reason})

        # 事件有效期（相邻项）：仅消费显式声明字段，未知不伪造过期。
        expires = outcome.get("event_expires_at")
        if expires is not None:
            exp_dt = _now_anchor(expires)
            if exp_dt is not None and exp_dt < as_of_dt:
                _exclude("event_expired")
                continue
        decided, decided_ok = _gate(record, "decision.decided_at")
        if not decided_ok:
            _exclude(UNVERIFIABLE.format("decided_at"))
            continue
        if decided > as_of_dt:
            _exclude(LATER_DECISION)
            continue
        status = outcome.get("status")
        maturity_raw = outcome.get("maturity_date")
        maturity = _parse_date_strict(maturity_raw)
        if maturity is None:
            _exclude(UNKNOWN_MATURITY if maturity_raw is None
                     else UNVERIFIABLE.format("maturity_date"))
            continue
        maturity_dt = datetime(maturity.year, maturity.month, maturity.day,
                               tzinfo=_SH_TZ) + timedelta(days=1, microseconds=-1)
        if status == STATUS_PENDING:
            _exclude(NOT_YET_MATURE if maturity_dt > as_of_dt else OVERDUE_UNRESOLVED)
            continue
        if status == STATUS_EXPIRED_UNRESOLVABLE:
            _exclude("expired_unresolvable_not_verified_experience")
            continue
        failed_gate: Optional[str] = None
        for field in ("outcome.observed_at", "outcome.publication_time",
                      "record_available_at"):
            gate_dt, ok = _gate(record, field)
            if not ok:
                failed_gate = UNVERIFIABLE.format(field.split(".")[-1])
                break
            if gate_dt > as_of_dt:
                failed_gate = f"future_{field.split('.')[-1]}"
                break
        if failed_gate:
            _exclude(failed_gate)
            continue
        if maturity_dt > as_of_dt:
            _exclude("future_maturity")
            continue
        if overlap_mode == "explicit_closed_interval":
            # 契约 #8：重叠排除仅作用于**相同 instrument/type** 的记录——
            # 异标的记录按 F1 排序仍可作为经验，不受本查询窗口排除。
            if (identity.get("ticker") == ticker
                    and identity.get("instrument_type") == instrument_type):
                w = identity.get("window") or {}
                ws = _parse_date_strict(w.get("start"))
                we = _parse_date_strict(w.get("end"))
                if ws is None or we is None:
                    _exclude(WINDOW_UNKNOWN_OVERLAP)
                    continue
                if not (we < q_start or ws > q_end):  # closed interval: equal counts
                    _exclude(OVERLAPPING_WINDOW)
                    continue
        eligible.append(record)

    # F1 契约规则 7 的排序语义（Codex F1 R1 相邻项）：同标的优先、同类型
    # 次之——是**排序**不是排除；跨标的记录排后（可被 limit 截断），
    # "仅同标的同类型"的投影过滤属 F2 接入层职责，两层不混淆。
    eligible.sort(key=lambda r: (
        0 if (r.get("identity") or {}).get("ticker") == ticker else 1,
        0 if (r.get("identity") or {}).get("instrument_type") == instrument_type else 1,
        -(_parse_datetime_strict((r.get("decision") or {}).get("decided_at"))[0]
          .timestamp()),
        str(r.get("record_id", "")),
    ))
    selected = eligible[:limit]
    truncated = max(len(eligible) - len(selected), 0)
    # 排除清单稳定化（Codex F1 R1 #6）：按 (record_id, reason) 排序——
    # 反序输入不得改变排除报告。
    excluded.sort(key=lambda e: (e["record_id"], e["reason"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "ranking_version": ranking_version,
        "as_of": str(as_of),
        "params": {"ticker": ticker, "instrument_type": instrument_type,
                   "limit": limit,
                   "query_window": (dict(query_window) if query_window else None)},
        "overlap_filter": overlap_mode,
        "records": [json.loads(json.dumps(r, ensure_ascii=False)) for r in selected],
        "excluded": excluded,
        "limit_truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Rendering (CLI Markdown; shared shape for later outlets)
# ---------------------------------------------------------------------------


def render_retrieval_md(result: Dict[str, Any]) -> str:
    lines = ["**历史经验检索（F1 as-of）**：",
             f"- 参数: as_of={result['as_of']}，ticker={result['params']['ticker']}，"
             f"type={result['params']['instrument_type']}，limit={result['params']['limit']}，"
             f"ranking_version={result['ranking_version']}，"
             f"overlap_filter={result['overlap_filter']}"]
    records = result.get("records") or []
    if not records:
        lines.append("- 无合格记录（可用性门槛 AND 五条件；排除原因见下）")
    for r in records:
        identity = r.get("identity") or {}
        decision = r.get("decision") or {}
        outcome = r.get("outcome") or {}
        lines.append(
            f"  - `{r['record_id']}` {decision.get('decided_at', '?')[:10]} "
            f"{decision.get('rating')}（{identity.get('ticker')}，window "
            f"{(identity.get('window') or {}).get('start')}~{(identity.get('window') or {}).get('end')}；"
            f"maturity {outcome.get('maturity_date') or 'unknown'}；"
            f"raw_return={outcome.get('raw_return')}（未知为 null）；"
            f"fees={((outcome.get('fees_assumption') or {}).get('bps')) if outcome.get('fees_assumption') else 'null'}）")
    excluded = result.get("excluded") or []
    if excluded:
        by_reason: Dict[str, int] = {}
        for e in excluded:
            by_reason[e["reason"]] = by_reason.get(e["reason"], 0) + 1
        lines.append("- 排除汇总: " +
                     "，".join(f"{k}×{v}" for k, v in sorted(by_reason.items())))
        lines.append("- 排除清单（逐条身份，稳定排序）:")
        for e in excluded:
            lines.append(f"  - `{e['record_id']}`: {e['reason']}")
    if result.get("input_file_digest"):
        lines.append(f"- 输入文件摘要: `{result['input_file_digest']}`")
    if result.get("limit_truncated"):
        lines.append(f"- limit 截断: 另有 {result['limit_truncated']} 条合格记录未列出"
                     "（截断不计为时间违规）")
    lines.append("> 可用性门槛为 AND 五条件（决策/成熟/观测/发布/版本）；未知保持未知；"
                 "盈利≠推理正确。本检索为离线契约演示，不代表预测效果提高。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI (rule 1): one explicit file; JSON + Markdown; exit 2 on rejection
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tradingagents.evaluation.review_record",
        description="F1 offline review-record retrieval (no network, no models).")
    parser.add_argument("--file", required=True, help="显式 JSONL 记录文件路径")
    parser.add_argument("--as-of", required=True, help="as-of（YYYY-MM-DD 或带 offset datetime）")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--instrument-type", required=True, choices=["stock", "index"])
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--query-window-start", default=None)
    parser.add_argument("--query-window-end", default=None)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    def _fail(message: str) -> int:
        print(message, file=sys.stderr)
        return 2

    import os
    try:
        with open(args.file, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return _fail(f"记录文件读取失败: {exc}")
    if len(raw) > MAX_FILE_BYTES:
        return _fail(f"记录文件超过 {MAX_FILE_BYTES} 字节上限")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _fail(f"记录文件不是 UTF-8: {exc}")
    try:
        records = load_records(text)
    except RecordValidationError as exc:
        return _fail(f"记录校验拒绝: {exc}")
    if len(records) > MAX_RECORDS:
        return _fail(f"记录条目 {len(records)} 超过 {MAX_RECORDS} 上限")
    query_window = None
    if args.query_window_start or args.query_window_end:
        query_window = {"start": args.query_window_start or "",
                        "end": args.query_window_end or ""}
    try:
        result = retrieve_as_of(
            records, ticker=args.ticker, instrument_type=args.instrument_type,
            as_of=args.as_of, limit=args.limit, query_window=query_window)
    except RecordValidationError as exc:
        return _fail(f"检索参数拒绝: {exc}")

    result["input_file_digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    os.makedirs(args.output_dir, exist_ok=True)
    jpath = os.path.join(args.output_dir, "review_retrieval.json")
    mpath = os.path.join(args.output_dir, "review_retrieval.md")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(render_retrieval_md(result) + "\n")
    print(f"selected={len(result['records'])} excluded={len(result['excluded'])} "
          f"-> {jpath} , {mpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
