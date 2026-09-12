"""F2: read-only review-record projection into decision prompts.

Authoritative contract: ``docs/F2_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`
(ten rules). Layering (rule 2) is fixed: validate ENTIRE input → same
ticker+instrument_type filter BEFORE limit → F1 AND-gate retrieval on the
matching set → ≤5 records projected. The persisted payload carries verified
snapshots only — prompt text is deterministically rendered FROM the payload
(rule 3), so every prompt-visible byte is covered by ``payload_digest``.

No production memory edits, no new model calls, default off.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from tradingagents.evaluation.review_record import (
    MAX_FILE_BYTES,
    RETRIEVAL_RANKING_VERSION,
    RecordValidationError,
    load_records,
    retrieve_as_of,
)

SCHEMA_VERSION = 1
PROJECTION_POLICY = "same_ticker_and_type_only"
PROJECTION_LIMIT = 5
LINE_CHAR_LIMIT = 160
TOTAL_CHAR_LIMIT = 1200

INVALID_PROJECTION_NOTICE = (
    "⚠️ 历史经验投影：归属校验无效（invalid-projection）——"
    "不展示任何历史数值或标注正文，以保存的运行 JSON 复核。"
)


def _canonical_payload_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def compute_payload_digest(payload: Dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "payload_digest"}
    return "sha256:" + hashlib.sha256(
        _canonical_payload_json(body).encode("utf-8")).hexdigest()


def build_review_projection(records_text: str, *, ticker: str,
                            instrument_type: str, as_of: str,
                            run_id: str) -> Dict[str, Any]:
    """Fresh-preparation entry: validate → filter → retrieve → payload.

    ``records_text`` is the raw file content (byte/record limits enforced at
    F1 load). Trusted context (ticker/type/as_of) comes from run preparation
    and can never be overridden by the file. Raises RecordValidationError on
    any illegal input — fail-fast before any model call.
    """
    if len(records_text.encode("utf-8")) > MAX_FILE_BYTES:
        raise RecordValidationError(
            f"review_records 文件超过 {MAX_FILE_BYTES} 字节上限")
    records = load_records(records_text)  # 全量校验（含 JSON 边界/digest/去重）

    # 可信过滤（rule 2：先过滤、在 limit 之前）
    matching = [r for r in records
                if (r.get("identity") or {}).get("ticker") == ticker
                and (r.get("identity") or {}).get("instrument_type") == instrument_type]
    cross = len(records) - len(matching)

    result = retrieve_as_of(matching, ticker=ticker,
                            instrument_type=instrument_type, as_of=as_of,
                            limit=PROJECTION_LIMIT,
                            ranking_version=RETRIEVAL_RANKING_VERSION)
    selected_snapshots = []
    for r in result["records"]:
        outcome = r.get("outcome") or {}
        decision = r.get("decision") or {}
        selected_snapshots.append({
            "record_id": r.get("record_id", ""),
            "decided_at": decision.get("decided_at", ""),
            "rating": decision.get("rating", ""),
            "maturity_date": outcome.get("maturity_date"),
            # 五门复核所需时间（Codex F2 R2 #3）：不能只留收益/到期而丢
            # 观察/发布/版本可用来源说明。
            "observed_at": outcome.get("observed_at"),
            "publication_time": outcome.get("publication_time"),
            "record_available_at": r.get("record_available_at"),
            "raw_return": outcome.get("raw_return"),
            "return_unit": outcome.get("return_unit", "ratio:fraction"),
            "availability_source": r.get("availability_source", ""),
        })

    excluded_stats: Dict[str, int] = {}
    for e in result.get("excluded") or []:
        reason = e["reason"]
        excluded_stats[reason] = excluded_stats.get(reason, 0) + 1

    input_digest = "sha256:" + hashlib.sha256(
        records_text.encode("utf-8")).hexdigest()
    limitations: List[str] = []
    if not selected_snapshots:
        limitations.append("本次无合格历史经验记录（无合格≠历史无事件）")
    limitations.append("availability 来自输入声明（declared_only 不升级为已验证历史事实）；盈利≠推理正确")

    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_binding": {"run_id": str(run_id)},
        "trusted_context": {"ticker": str(ticker), "instrument_type": str(instrument_type),
                            "as_of": str(as_of)},
        "projection_policy": PROJECTION_POLICY,
        "ranking_version": RETRIEVAL_RANKING_VERSION,
        "input_digest": input_digest,
        "selected": selected_snapshots,
        "excluded_stats": excluded_stats,
        "excluded_cross_instrument": cross,
        "limit_truncated": result.get("limit_truncated", 0),
        "overlap_filter": result.get("overlap_filter", "not_requested"),
        "limitations": limitations,
    }
    payload["payload_digest"] = compute_payload_digest(payload)
    return payload


def _shape_and_json_legal(payload: Dict[str, Any]) -> List[str]:
    """Recursive shape/JSON legality BEFORE any digest math (Codex F2 R1 #3/#5).

    Digest recomputation must never see NaN/Infinity or wrong-typed nodes;
    every failure becomes an explicit validation problem.
    """
    import math as _math
    problems: List[str] = []

    def _walk(value: Any, path: str) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                problems.append(f"{path}: 含无法 UTF-8 编码的字符串")
            return
        if isinstance(value, float):
            if not _math.isfinite(value):
                problems.append(f"{path}: 非有限数值（NaN/Infinity 拒绝）")
            return
        if isinstance(value, int):
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if not isinstance(k, str):
                    problems.append(f"{path}: 字典键必须是字符串")
                    continue
                _walk(v, f"{path}.{k}")
            return
        if isinstance(value, list):
            for i, item in enumerate(value):
                _walk(item, f"{path}[{i}]", )
            return
        problems.append(f"{path}: 非法类型 {type(value).__name__}")

    _walk(payload, "payload")
    return problems


def _schema_validate_payload(payload: Dict[str, Any]) -> List[str]:
    """Full schema validation (Codex F2 R1 #5) — hash is NOT a substitute."""
    problems: List[str] = []
    sv = payload.get("schema_version")
    if isinstance(sv, bool) or not isinstance(sv, int) or sv != SCHEMA_VERSION:
        problems.append(f"schema_version 非法（{sv!r}）")
    binding = payload.get("run_binding")
    if not isinstance(binding, dict) or not isinstance(binding.get("run_id"), str) \
            or not binding.get("run_id", "").strip():
        problems.append("run_binding.run_id 缺失/非字符串/为空")
    ctx = payload.get("trusted_context")
    if not isinstance(ctx, dict):
        problems.append("trusted_context 必须是对象")
        ctx = {}
    else:
        for field in ("ticker", "instrument_type", "as_of"):
            if not isinstance(ctx.get(field), str) or not str(ctx.get(field, "")).strip():
                problems.append(f"trusted_context.{field} 缺失/非字符串/为空")
    if payload.get("projection_policy") != PROJECTION_POLICY:
        problems.append("projection_policy 不符")
    rv = payload.get("ranking_version")
    if isinstance(rv, bool) or not isinstance(rv, int) or rv != RETRIEVAL_RANKING_VERSION:
        problems.append(f"ranking_version 非法（{rv!r}）")
    if not isinstance(payload.get("input_digest"), str) or \
            not payload["input_digest"].startswith("sha256:"):
        problems.append("input_digest 缺失/非法")
    selected = payload.get("selected")
    if not isinstance(selected, list) or len(selected) > PROJECTION_LIMIT:
        problems.append("selected 必须是列表且不超过上限")
        selected = []
    import math as _math
    from tradingagents.evaluation.review_record import _parse_datetime_strict
    for i, snap in enumerate(selected):
        if not isinstance(snap, dict):
            problems.append(f"selected[{i}] 必须是对象")
            continue
        if not isinstance(snap.get("record_id"), str) or not snap.get("record_id", "").strip():
            problems.append(f"selected[{i}].record_id 非法")
        for field in ("decided_at", "rating"):
            if not isinstance(snap.get(field), str):
                problems.append(f"selected[{i}].{field} 必须是字符串")
        # 快照时间契约（Codex F2 R2 #3）：F1 严格时间原语 + AND 可见性。
        decided_parsed = _parse_datetime_strict(snap.get("decided_at"))
        decided = decided_parsed[0] if decided_parsed else None
        if decided is None:
            problems.append(f"selected[{i}].decided_at 非法时间（严格 F1 原语）")
        maturity_parsed = _parse_datetime_strict(snap.get("maturity_date")) \
            if snap.get("maturity_date") is not None else None
        maturity = maturity_parsed[0] if maturity_parsed else None
        if snap.get("maturity_date") is not None and maturity is None:
            problems.append(f"selected[{i}].maturity_date 非法时间")
        for gate in ("observed_at", "publication_time", "record_available_at"):
            value = snap.get(gate)
            if value is not None and _parse_datetime_strict(value) is None:
                problems.append(f"selected[{i}].{gate} 非法时间")
        # AND 五门（对投影 as_of）：缺失的必需门 = 不可验证 → 拒绝
        ctx_obj = payload.get("trusted_context")
        as_of_raw = ctx_obj.get("as_of") if isinstance(ctx_obj, dict) else None
        ctx_as_of_parsed = _parse_datetime_strict(as_of_raw)
        ctx_as_of = ctx_as_of_parsed[0] if ctx_as_of_parsed else None
        if ctx_as_of is None:
            # as_of 无法解析 → 拒绝而非跳过时间门（Codex F2 R3 完成边界）
            problems.append("trusted_context.as_of 无法解析——时间门拒绝执行"
                            "（不跳过）")
        if decided is not None and ctx_as_of is not None:
            for gate_name, gate_val in (("decided_at", decided),
                                        ("maturity_date", maturity)):
                if gate_val is None:
                    problems.append(f"selected[{i}] 缺少 {gate_name}（不可验证）")
                elif gate_val > ctx_as_of:
                    problems.append(f"selected[{i}].{gate_name} 晚于投影 as_of"
                                    "（未来数据不可见，AND 门失败）")
            for gate_name in ("observed_at", "publication_time", "record_available_at"):
                gate_val = snap.get(gate_name)
                if gate_val is None:
                    problems.append(f"selected[{i}] 缺少 {gate_name}（不可验证）")
                else:
                    gate_parsed = _parse_datetime_strict(gate_val)
                    if gate_parsed and gate_parsed[0] > ctx_as_of:
                        problems.append(f"selected[{i}].{gate_name} 晚于投影 as_of")
        rr = snap.get("raw_return")
        if rr is not None and (isinstance(rr, bool) or not isinstance(rr, (int, float))
                               or not _math.isfinite(rr)):
            problems.append(f"selected[{i}].raw_return 必须是有限非 bool 数值或 null")
        unit = snap.get("return_unit", "ratio:fraction")
        if unit != "ratio:fraction":
            problems.append(f"selected[{i}].return_unit 冲突（{unit!r}）")
        if snap.get("availability_source") not in ("declared_only", "verified_snapshot", ""):
            problems.append(f"selected[{i}].availability_source 非法")
    stats = payload.get("excluded_stats")
    if not isinstance(stats, dict):
        problems.append("excluded_stats 必须是对象")
    else:
        for k, v in stats.items():
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                problems.append(f"excluded_stats[{k!r}] 必须是非负整数")
    cross = payload.get("excluded_cross_instrument")
    if isinstance(cross, bool) or not isinstance(cross, int) or cross < 0:
        problems.append("excluded_cross_instrument 必须是非负整数")
    trunc = payload.get("limit_truncated")
    if isinstance(trunc, bool) or not isinstance(trunc, int) or trunc < 0:
        problems.append("limit_truncated 必须是非负整数")
    if not isinstance(payload.get("limitations"), list) or \
            not all(isinstance(x, str) for x in payload["limitations"]):
        problems.append("limitations 必须是字符串列表")
    return problems


def validate_projection_payload(payload: Any, *, run_id: str = "",
                                ticker: str = "", instrument_type: str = "",
                                as_of: str = "",
                                input_digest: Any = None,
                                anchor: Any = None,
                                mode: str = "strict") -> List[str]:
    """Validation with EXPLICIT modes (Codex F2 R3):

    - ``mode="structure"`` — report rendering: shape/JSON legality, schema,
      digest math. NO trusted-identity comparison (no run context here);
      shape failures short-circuit — bad objects are never dereferenced.
    - ``mode="strict"`` (default) — prepare/resume/five factories: adds the
      mandatory trusted-identity comparison (missing/None/empty trusted
      values are themselves invalid) and the independent anchor check.

    ``as_of`` that fails strict parsing refuses the time gates (they are
    never silently skipped).
    """
    if not isinstance(payload, dict):
        return ["投影不是对象"]
    problems: List[str] = []
    shape = _shape_and_json_legal(payload)
    if shape:
        return shape  # shape illegal → short-circuit; NEVER dereference bad nodes
    problems += _schema_validate_payload(payload)
    if mode != "strict":
        # structure-only: digest math is still content verification
        try:
            claimed = payload.get("payload_digest")
            recomputed = compute_payload_digest(payload)
            if claimed != recomputed:
                problems.append(f"payload_digest 失配（声称 {claimed!r}，复算 {recomputed!r}）")
        except (TypeError, ValueError) as exc:
            problems.append(f"payload_digest 复算失败（{exc}）")
        return problems
    ctx = payload.get("trusted_context") if isinstance(payload.get("trusted_context"), dict) else {}
    binding_obj = payload.get("run_binding")
    binding = binding_obj.get("run_id", "") if isinstance(binding_obj, dict) else ""
    # 严格可信模式（Codex F2 R2 #1）：传入受信任值即要求其存在且非空——
    # 缺失/None/空串都是无效身份，不作为"可选未提供"跳过比较。
    if run_id is not None:
        if not isinstance(run_id, str) or not run_id.strip():
            problems.append(f"可信 run_id 缺失/非字符串/为空（{run_id!r}）")
        elif str(binding) != str(run_id):
            problems.append(f"run_id 不符（{binding!r} vs {run_id!r}）")
    for field, trusted in (("ticker", ticker), ("instrument_type", instrument_type),
                           ("as_of", as_of)):
        if trusted is not None:
            if not isinstance(trusted, str) or not trusted.strip():
                problems.append(f"可信 {field} 缺失/非字符串/为空（{trusted!r}）")
            elif str(ctx.get(field)) != str(trusted):
                problems.append(f"trusted_context.{field} 不符"
                                f"（{ctx.get(field)!r} vs {trusted!r}）")
    if input_digest is not None and payload.get("input_digest") != input_digest:
        problems.append(f"input_digest 与运行元数据锚不符（{payload.get('input_digest')!r}）")
    if anchor is not None:
        if not isinstance(anchor, dict):
            problems.append("运行元数据锚损坏（非对象）")
        else:
            if not anchor.get("enabled"):
                problems.append("锚点声明 disabled 但 state 含投影（矛盾）")
            if anchor.get("payload_digest") != payload.get("payload_digest"):
                problems.append("锚点 payload_digest 不符")
            if anchor.get("input_digest") != payload.get("input_digest"):
                problems.append("锚点 input_digest 不符")
            a_schema = anchor.get("schema_version")
            if (isinstance(a_schema, bool) or not isinstance(a_schema, int)
                    or a_schema != SCHEMA_VERSION):
                problems.append(f"锚 schema_version 非法（{a_schema!r}，"
                                f"必须已知整数 {SCHEMA_VERSION}）")
            for field, trusted in (("ticker", ticker), ("instrument_type", instrument_type),
                                   ("as_of", as_of)):
                if trusted and str(anchor.get(field)) != str(trusted):
                    problems.append(f"锚点 {field} 不符（{anchor.get(field)!r} vs {trusted!r}）")
    try:
        claimed = payload.get("payload_digest")
        recomputed = compute_payload_digest(payload)
        if claimed != recomputed:
            problems.append(f"payload_digest 失配（声称 {claimed!r}，复算 {recomputed!r}）")
    except (TypeError, ValueError) as exc:
        problems.append(f"payload_digest 复算失败（{exc}）")
    return problems


def _line(text: str, limit: int = LINE_CHAR_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def render_projection_text(payload: Dict[str, Any]) -> str:
    """Deterministic bounded prompt block rendered FROM the verified payload.

    Rule 7 budgets include heading, limitations and truncation notice;
    limitations are reserved first so long records can never squeeze them out.
    """
    limitations = [str(x) for x in (payload.get("limitations") or [])]
    selected = payload.get("selected") or []
    lines: List[str] = ["历史经验检索（F2 只读投影，same_ticker_and_type_only）："]
    for snap in selected[:PROJECTION_LIMIT]:
        rr = snap.get("raw_return")
        rr_text = "unknown" if rr is None else f"{rr}({snap.get('return_unit', 'ratio:fraction')})"
        lines.append(_line(
            f"- `{snap['record_id']}` {str(snap.get('decided_at', ''))[:10]} "
            f"{snap.get('rating', '')}（到期 {snap.get('maturity_date') or 'unknown'}；"
            f"raw_return={rr_text}；availability={snap.get('availability_source') or 'unknown'}）"))
    stats = payload.get("excluded_stats") or {}
    if stats or payload.get("excluded_cross_instrument"):
        parts = [f"{k}×{v}" for k, v in sorted(stats.items())]
        if payload.get("excluded_cross_instrument"):
            parts.append(f"异标的/类型×{payload['excluded_cross_instrument']}")
        lines.append(_line("- 排除统计: " + "，".join(parts)))
    if payload.get("limit_truncated"):
        lines.append(f"- limit 截断: 另有 {payload['limit_truncated']} 条未列出")
    for lim in limitations:
        lines.append(_line(f"- {lim}"))

    trailer = "（投影达字符上限，截断明示；详情见运行 JSON）"

    def _join(body: List[str]) -> str:
        return "\n".join(body) + "\n"

    text = _join(lines)
    if len(text) <= TOTAL_CHAR_LIMIT:
        return text
    # 预算内截断（rule 7）：限制优先保留——标题 + 尾部（排除统计/限制）固定，
    # 从记录行头部收缩；截断行数明示；尾注长度预留。
    n_tail = len(limitations) + (1 if (payload.get("excluded_stats")
                                      or payload.get("excluded_cross_instrument")) else 0) + 1
    tail = lines[-n_tail:] if n_tail else []
    head = [lines[0]]
    middle = lines[1:len(lines) - n_tail] if n_tail else lines[1:]
    dropped = 0
    while middle and len(_join(head + middle + tail)) + len(trailer) > TOTAL_CHAR_LIMIT:
        middle.pop(0)
        dropped += 1
    if dropped:
        note = f"- …另有 {dropped} 行因字符上限截断"
        text = _join(head + [note] + middle + tail)
    else:
        text = _join(head + middle + tail)
    if len(text) + len(trailer) > TOTAL_CHAR_LIMIT:
        text = text[:TOTAL_CHAR_LIMIT - len(trailer)]
    return text + trailer


def projection_for_prompt(state: Any) -> str:
    """Five-factory prompt entry ('' when disabled; controlled notice when
    invalid — Codex F2 R1: the INDEPENDENT run-metadata anchor is REQUIRED;
    payload self-consistency never proves run ownership; missing trusted
    identity is itself invalid. Never crashes on corrupt input."""
    try:
        meta = state.get("run_metadata") if isinstance(state, dict) else None
        if isinstance(meta, dict):
            anchor = meta.get("review_projection")
            if isinstance(anchor, dict) and anchor.get("enabled") is False:
                # 显式 disabled 锚（fresh 两种模式都写锚）：合法关闭态 →
                # 零块并立即返回（不继续验证任何 payload）。
                if state.get("review_projection") is not None:
                    return (f"{INVALID_PROJECTION_NOTICE}\n- 校验失败："
                            "锚点声明 disabled 但 state 含投影（矛盾）\n")
                return ""
            if isinstance(anchor, dict) and anchor.get("enabled") is True:
                # enabled 锚存在：payload 缺失是被破坏的启用态，不是关闭
                payload = state.get("review_projection")
                if payload is None:
                    return (f"{INVALID_PROJECTION_NOTICE}\n- 校验失败："
                            "锚点声明 enabled 但 state 缺少投影（损坏，非关闭）\n")
            else:
                payload = state.get("review_projection")
                if payload is not None:
                    return (f"{INVALID_PROJECTION_NOTICE}\n- 校验失败："
                            "缺少 enabled 的运行元数据锚点\n")
                if not isinstance(anchor, dict):
                    return ""  # 真正未启用（无锚）或旧缺席 → 无块
        else:
            payload = state.get("review_projection") if isinstance(state, dict) else None
            if payload is None:
                return ""
        if not isinstance(meta, dict) or not str(meta.get("run_id") or "").strip():
            return f"{INVALID_PROJECTION_NOTICE}\n- 校验失败：缺少可信 run_metadata.run_id\n"
        anchor = meta.get("review_projection")
        problems = validate_projection_payload(
            payload,
            run_id=str(meta.get("run_id") or ""),
            ticker=str(state.get("company_of_interest") or ""),
            instrument_type=str(state.get("instrument_type") or ""),
            as_of=str(state.get("trade_date") or ""),
            anchor=anchor)
        if problems:
            return f"{INVALID_PROJECTION_NOTICE}\n- 校验失败：{'；'.join(problems[:3])}\n"
        return render_projection_text(payload)
    except Exception:  # noqa: BLE001 — 提示词入口永不裸崩（Codex F2 R1 #3）
        return f"{INVALID_PROJECTION_NOTICE}\n- 校验失败：投影结构损坏（受控拒绝）\n"


def render_projection_md(payload: Any) -> str:
    """Shared Markdown for Web/MD/PDF outlets (three states, rule 9).

    Structure-only validation (no run context here) — malformed payloads
    (list run_binding/context, NaN, …) produce a controlled notice, never
    a crash and never echoed historical bodies.
    """
    if not isinstance(payload, dict):
        return ("**历史经验投影（F2）**: 未记录（未启用 review_records_path，或旧版本报告）。\n"
                "（只读投影，不改生产记忆；合成/声明数据不代表预测效果）")
    if not isinstance(payload.get("schema_version"), int) or \
            isinstance(payload.get("schema_version"), bool) or \
            payload.get("schema_version") != SCHEMA_VERSION:
        return ("**历史经验投影（F2）**: 未记录或版本未知"
                f"（schema_version={payload.get('schema_version')!r}）。\n")
    # 结构专用模式（Codex F2 R3）：shape 短路——坏对象不被解引用；不做
    # 可信身份比较（那属于准备/恢复/工厂的严格模式），也不按错误文案过滤。
    problems = validate_projection_payload(payload, mode="structure")
    if problems:
        return (f"**历史经验投影（F2）**: ⚠️ 结构/摘要校验失败（{'；'.join(problems[:3])}）——"
                "数值不展示，以运行 JSON 复核（结构自洽不证明运行归属）。\n")
    ctx = payload.get("trusted_context") or {}
    lines = ["**历史经验投影（F2 只读）**：",
             f"- 可信上下文: ticker={ctx.get('ticker')}，type={ctx.get('instrument_type')}，"
             f"as_of={ctx.get('as_of')}；策略={payload.get('projection_policy')}；"
             f"ranking={payload.get('ranking_version')}",
             f"- 摘要: input=`{payload.get('input_digest')}`，"
             f"payload=`{payload.get('payload_digest')}`"]
    for snap in payload.get("selected") or []:
        rr = snap.get("raw_return")
        lines.append(f"  - `{snap['record_id']}` {str(snap.get('decided_at', ''))[:10]} "
                     f"{snap.get('rating', '')}（到期 {snap.get('maturity_date') or 'unknown'}；"
                     f"raw_return={'unknown' if rr is None else rr}；"
                     f"availability={snap.get('availability_source') or 'unknown'}）")
    stats = payload.get("excluded_stats") or {}
    if stats:
        lines.append("- 排除统计: " + "，".join(f"{k}×{v}" for k, v in sorted(stats.items())))
    if payload.get("excluded_cross_instrument"):
        lines.append(f"- 异标的/类型排除: {payload['excluded_cross_instrument']} 条")
    if payload.get("limit_truncated"):
        lines.append(f"- limit 截断: 另有 {payload['limit_truncated']} 条合格记录未列出"
                     "（完整清单见运行 JSON）")
    lines.append(f"- overlap_filter: {payload.get('overlap_filter', 'not_requested')}"
                 "（F2 不臆造预测期限，不传查询窗口）")
    for lim in payload.get("limitations") or []:
        lines.append(f"- {lim}")
    return "\n".join(lines)
