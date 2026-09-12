"""Reproducible financial panel (D1): deterministic metrics + scenario
valuation from an explicit offline manifest.

Contract: ``docs/D_FINANCIAL_PANEL_CONTRACT_2026-09-08.md`` (draft sections
reconciled with the authoritative Codex 23:28 correction appended there).

Design invariants:

- **Pure stdlib, offline**: no network, no LLM, no market requests;
  ``offline_ref`` provenance strings are labels only — never read.
- **Closed registries** (units / currencies / metrics) with CNY-only D1:
  cross-currency formulas are ``unknown``/``unsupported_currency``.
- **Typed finite numbers**: bool/NaN/Infinity/numeric-strings rejected;
  computation in ``Decimal(str(value))``; per-kind rounding on output.
  ``0`` and "not provided" are different things — null/未列报 never
  becomes 0 (all four debt items REQUIRED for net cash).
- **Availability before computation**: inputs disclosed after the trusted
  analysis cutoff (Shanghai end-of-day) or with unknown disclosure are
  excluded first; a result is only knowable once ALL dependencies are
  disclosed, so ``available_date`` is the MAX participating disclosure.
- **D1 accepts single explicit versions only**: any restatement chain
  (``restated``/``supersedes``) rejects the manifest — D1 does not claim
  historical version selection (declared limitation).
- **Reproducible**: every ok value carries its formula dependency chain
  (rule/formula_version/input_ids + normalized inputs), and the output
  JSON embeds the full normalized input table plus the verified
  ``manifest_digest``.
- Index instruments are ``not_applicable`` for the whole panel (trusted
  ``instrument_type`` from the caller, never guessed from the code).
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
FORMULA_VERSION = "d1"

# --- closed registries -------------------------------------------------------

MONEY_UNITS = {"CNY:yuan": Decimal("1"), "CNY:wanyuan": Decimal("1e4"), "CNY:yi_yuan": Decimal("1e8")}
PER_SHARE_UNITS = {"CNY:yuan_per_share"}
MULTIPLE_UNITS = {"ratio:x"}
FRACTION_UNIT = "ratio:fraction"
CURRENCY = "CNY"

FLOW_MONEY_METRICS = frozenset({"revenue", "net_profit", "net_profit_total", "ocf"})
SNAPSHOT_MONEY_METRICS = frozenset({
    "cash", "short_term_borrowing", "long_term_borrowing",
    "bonds_payable", "non_current_debt_due_within_1y",
})
EPS_METRICS = frozenset({"eps_ttm"})
MULTIPLE_METRICS = frozenset({"multiple_bear", "multiple_base", "multiple_bull"})
ALL_METRICS = FLOW_MONEY_METRICS | SNAPSHOT_MONEY_METRICS | EPS_METRICS | MULTIPLE_METRICS

METRIC_LABELS_ZH = {
    "revenue": "营业收入", "net_profit": "归母净利润", "net_profit_total": "净利润（含少数股东）",
    "ocf": "经营活动现金流净额", "cash": "货币资金", "short_term_borrowing": "短期借款",
    "long_term_borrowing": "长期借款", "bonds_payable": "应付债券",
    "non_current_debt_due_within_1y": "一年内到期非流动负债（有息部分）",
    "eps_ttm": "EPS(TTM)", "multiple_bear": "悲观倍数（用户给定）",
    "multiple_base": "基准倍数（用户给定）", "multiple_bull": "乐观倍数（用户给定）",
}

FLOW_KINDS = {"annual", "cumulative", "single_quarter", "ttm"}
CUMULATIVE_WINDOWS = {  # window -> (months, start md, end md)
    "Q1": (3, (1, 1), (3, 31)),
    "H1": (6, (1, 1), (6, 30)),
    "Q3": (9, (1, 1), (9, 30)),
}
ANNUAL_WINDOWS = {"FY": (12, (1, 1), (12, 31))}
SINGLE_Q_WINDOWS = {
    "Q1s": (3, (1, 1), (3, 31)), "Q2s": (3, (4, 1), (6, 30)),
    "Q3s": (3, (7, 1), (9, 30)), "Q4s": (3, (10, 1), (12, 31)),
}

_SH_TZ_OFFSET = timedelta(hours=8)

# Per-kind output rounding (Decimal exponent) and display hints.
_ROUND_MONEY = Decimal("0.01")
_ROUND_FRACTION = Decimal("0.000001")

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_NOT_REQUESTED = "not_requested"

NET_CASH_SCOPE_NOTE = (
    "限定口径：仅统计已显式披露的四项有息债务（短期借款/长期借款/应付债券/一年内到期非流动负债有息部分）"
    "减货币资金科目；不声称涵盖租赁负债、应付票据等其他债务，货币资金亦不等同于可自由支配现金。"
    "负值表示该限定口径下的净债务。"
)
SCENARIO_NOTE = (
    "情景假设为用户/fixture 显式给定（倍数与盈利均非财报事实）；区间是给定假设下的算术映射，"
    "不是预测——区间更精细不等于预测更准。D1 不做 FX、DCF 与股本路径。"
)


class ManifestError(ValueError):
    """Manifest rejected (deterministic error list). CLI exit code 2."""


# --- small helpers ----------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_decimal(value: Any) -> Optional[Decimal]:
    if not _is_number(value):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return d


def _parse_date_strict(value: str) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_disclosed(value: Any) -> Tuple[Optional[datetime], str, Optional[str]]:
    """Strict parse of disclosed_at.

    Accepts ``YYYY-MM-DD`` (date precision) or a full ISO datetime WITH
    offset (``Z`` normalized); never truncates a datetime to its first ten
    characters. Returns ``(aware_datetime|None, precision, error|None)``.
    ``"unknown"`` (or absent) yields ``(None, "unknown", None)`` — the input
    can exist in the manifest but cannot participate in any ok value.
    """
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "unknown", "未知")):
        return None, "unknown", None
    if not isinstance(value, str):
        return None, "invalid", "disclosed_at 必须是 YYYY-MM-DD 或带 offset 的 ISO datetime"
    s = value.strip()
    d = _parse_date_strict(s)
    if d is not None:
        return datetime(d.year, d.month, d.day, tzinfo=_tz_sh()), "date", None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None, "invalid", f"disclosed_at 无法严格解析：{value!r}"
    if dt.tzinfo is None:
        return None, "invalid", "disclosed_at 的 datetime 必须带时区 offset"
    return dt, "datetime", None


def _tz_sh() -> timezone:
    return timezone(_SH_TZ_OFFSET)


def _sh_end_of_day(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=_tz_sh()) + timedelta(days=1, microseconds=-1)


def _fmt_available(dt_values: List[datetime], precisions: List[str]) -> Tuple[Optional[str], str]:
    """MAX disclosure among participants, keeping the winner's own precision.

    Date-only disclosures are compared conservatively at end-of-day; the
    reported value keeps the winner's ORIGINAL precision — a date-only
    latest dependency never gains an invented midnight timestamp, and a
    datetime dependency never silently degrades to date-only.
    """
    if not dt_values:
        return None, "unknown"

    def _effective(i: int) -> datetime:
        if precisions[i] == "datetime":
            return dt_values[i]
        return dt_values[i] + timedelta(days=1, microseconds=-1)

    best = max(range(len(dt_values)),
               key=lambda i: (_effective(i), 1 if precisions[i] == "datetime" else 0))
    if precisions[best] == "datetime":
        return dt_values[best].isoformat(), "datetime"
    return dt_values[best].date().isoformat(), "date"


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _is_complete_month_window(ds: date, de: date, months: int) -> bool:
    """A full calendar window: starts on day 1, ends on month-end, and spans
    exactly ``months`` months (leap years handled by month arithmetic)."""
    if ds.day != 1:
        return False
    if de.day != calendar.monthrange(de.year, de.month)[1]:
        return False
    return de == _add_months(ds, months) - timedelta(days=1)


# --- manifest validation ----------------------------------------------------


def _validate_period(metric: str, period: Any, errors: List[str]) -> Optional[Dict[str, Any]]:
    """Validate and normalize a period block per the metric's kind."""
    if metric in MULTIPLE_METRICS:
        if period not in (None, {}):
            errors.append(f"{metric}: 情景假设不携带期间（period 必须为空）")
            return None
        return None
    if not isinstance(period, dict):
        errors.append(f"{metric}: period 必须是对象")
        return None
    if metric in SNAPSHOT_MONEY_METRICS:
        declared_kind = period.get("kind")
        if declared_kind not in (None, "snapshot"):
            # 存量科目声明的 kind 与 snapshot 矛盾 → 拒绝，不静默改写口径。
            errors.append(f"{metric}: 存量科目期间 kind 必须是 snapshot（声明为 {declared_kind!r}，拒绝而非归一化）")
            return None
        end = period.get("period_end")
        d = _parse_date_strict(end) if isinstance(end, str) else None
        if d is None:
            errors.append(f"{metric}: snapshot 需要 period_end=YYYY-MM-DD")
            return None
        extra = set(period) - {"kind", "period_end"}
        if extra:
            errors.append(f"{metric}: snapshot 期间不允许字段 {sorted(extra)}")
        return {"kind": "snapshot", "period_end": d.isoformat()}
    kind = period.get("kind")
    window = period.get("window")
    fy = period.get("fiscal_year")
    ps, pe = period.get("period_start"), period.get("period_end")
    if kind not in FLOW_KINDS:
        errors.append(f"{metric}: kind 必须是 {sorted(FLOW_KINDS)}")
        return None
    if not isinstance(fy, int) or isinstance(fy, bool) or not (1900 < fy < 2200):
        errors.append(f"{metric}: fiscal_year 必须是合理年份整数")
        return None
    ds, de = _parse_date_strict(ps) if isinstance(ps, str) else None, _parse_date_strict(pe) if isinstance(pe, str) else None
    if ds is None or de is None or ds > de:
        errors.append(f"{metric}: period_start/period_end 必须是合法日期且 start ≤ end")
        return None
    table = (ANNUAL_WINDOWS if kind == "annual" else
             SINGLE_Q_WINDOWS if kind == "single_quarter" else
             CUMULATIVE_WINDOWS if kind == "cumulative" else None)
    if table is not None:
        if window not in table:
            errors.append(f"{metric}: kind={kind} 的 window 必须是 {sorted(table)}")
            return None
        months, (sm, sd), (em, ed) = table[window]
        if (ds.year, ds.month, ds.day) != (fy, sm, sd) or (de.year, de.month, de.day) != (fy, em, ed):
            errors.append(f"{metric}: {kind}/{window}/{fy} 与起止日期不一致（应为 {fy}-{sm:02d}-{sd:02d} 至 {fy}-{em:02d}-{ed:02d}）")
            return None
    else:  # ttm — a COMPLETE full-month window of exactly 12 months
        if window != "TTM":
            errors.append(f"{metric}: ttm 的 window 必须是 TTM")
            return None
        if not _is_complete_month_window(ds, de, 12):
            errors.append(
                f"{metric}: TTM 必须是连续完整月窗口（起日为月初、止日为月末、恰 12 个自然月；"
                f"{ds}~{de} 不满足，不凭涉及十二个月份断言覆盖一年）")
            return None
        if fy != de.year:
            errors.append(f"{metric}: TTM fiscal_year 必须与 period_end 年份一致（{fy} vs {de.year}）")
            return None
    if metric in EPS_METRICS and kind != "ttm":
        # EPS(TTM) 只对应完整十二个月声明；其他科目的合法季度窗口不自动适用。
        errors.append(f"{metric}: EPS(TTM) 的期间 kind 必须是 ttm（完整十二个月），拒绝 {kind}/{window}")
        return None
    return {"kind": kind, "window": window, "fiscal_year": fy,
            "period_start": ds.isoformat(), "period_end": de.isoformat()}


def _validate_provenance(item: Dict[str, Any], errors: List[str]) -> Optional[str]:
    prov = item.get("provenance")
    if not isinstance(prov, dict):
        errors.append(f"{item.get('input_id', '?')}: provenance 必须是对象")
        return None
    keys = set(prov)
    if keys != {"evidence_id"} and keys != {"offline_ref"}:
        errors.append(f"{item.get('input_id', '?')}: provenance 必须恰好含 evidence_id 或 offline_ref 之一")
        return None
    val = next(iter(prov.values()))
    if not isinstance(val, str) or not val.strip():
        errors.append(f"{item.get('input_id', '?')}: provenance 值必须是非空字符串")
        return None
    return f"{next(iter(keys))}:{val}"


def _canonical_inputs_json(inputs: List[Dict[str, Any]]) -> str:
    return json.dumps(
        sorted(inputs, key=lambda x: x["input_id"]),
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


def compute_manifest_digest(inputs: List[Dict[str, Any]]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_inputs_json(inputs).encode("utf-8")).hexdigest()


def load_manifest(manifest: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str]]:
    """Validate a manifest dict → (normalized inputs, header, digest).

    Raises :class:`ManifestError` with a complete deterministic error list.
    """
    errors: List[str] = []
    if not isinstance(manifest, dict):
        raise ManifestError("manifest 必须是 JSON 对象")
    header = {k: manifest.get(k) for k in ("instrument", "instrument_type") if k in manifest}
    if "instrument_type" in header and header["instrument_type"] not in ("stock", "index"):
        raise ManifestError("instrument_type 必须是 stock 或 index")
    raw_inputs = manifest.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ManifestError("inputs 必须是非空数组（空面板无意义——不要为不存在的数据生成卡片）")

    inputs: List[Dict[str, Any]] = []
    seen_ids = set()
    seen_slots = {}
    for i, item in enumerate(raw_inputs):
        if not isinstance(item, dict):
            errors.append(f"inputs[{i}] 必须是对象")
            continue
        input_id = item.get("input_id")
        if not isinstance(input_id, str) or not input_id.strip():
            errors.append(f"inputs[{i}]: input_id 必须是非空字符串")
            continue
        if input_id in seen_ids:
            errors.append(f"{input_id}: input_id 重复")
            continue
        seen_ids.add(input_id)
        metric = item.get("metric")
        if metric not in ALL_METRICS:
            errors.append(f"{input_id}: metric {metric!r} 不在 D1 词表内")
            continue
        if item.get("restated") not in (False, None):
            errors.append(f"{input_id}: D1 只接单一显式版本（restated 必须为 false）")
            continue
        if item.get("supersedes") not in (None, ""):
            errors.append(f"{input_id}: D1 拒绝重述链（supersedes 必须为 null）——不支持历史版本选择")
            continue
        dec = _finite_decimal(item.get("value"))
        if dec is None:
            errors.append(f"{input_id}: value 必须是有限数值（bool/字符串/NaN/Infinity/null 均拒绝；未列报≠0，请勿提供该项）")
            continue
        unit = item.get("unit")
        if metric in EPS_METRICS:
            ok_units = PER_SHARE_UNITS
        elif metric in MULTIPLE_METRICS:
            ok_units = MULTIPLE_UNITS
        else:
            ok_units = set(MONEY_UNITS)
        if unit not in ok_units:
            errors.append(f"{input_id}: metric {metric} 的单位必须是 {sorted(ok_units)}（EPS 不得用金额单位）")
            continue
        currency = item.get("currency", CURRENCY)
        if not isinstance(currency, str) or not currency:
            errors.append(f"{input_id}: currency 必须是非空字符串")
            continue
        scope = item.get("scope")
        if scope not in ("consolidated", "parent_only"):
            errors.append(f"{input_id}: scope 必须是 consolidated 或 parent_only")
            continue
        dt, precision, derr = _parse_disclosed(item.get("disclosed_at"))
        if derr:
            errors.append(f"{input_id}: {derr}")
            continue
        period = _validate_period(metric, item.get("period"), errors)
        if period is None and metric not in MULTIPLE_METRICS:
            continue
        prov = _validate_provenance(item, errors)
        if prov is None:
            continue
        slot_key = (metric, scope, json.dumps(period, sort_keys=True, ensure_ascii=False) if period else "")
        if slot_key in seen_slots:
            errors.append(
                f"{input_id}: 与 {seen_slots[slot_key]} 重复（同 metric/scope/period 只允许一个显式版本）")
            continue
        seen_slots[slot_key] = input_id
        inputs.append({
            "input_id": input_id,
            "metric": metric,
            "value": item.get("value"),
            "normalized_value": format(dec.normalize(), "f"),
            "unit": unit,
            "currency": currency,
            "scope": scope,
            "period": period,
            "disclosed_at_raw": item.get("disclosed_at") if item.get("disclosed_at") is not None else "unknown",
            "disclosed_dt": dt,
            "disclosed_precision": precision,
            "provenance": prov,
        })

    if errors:
        raise ManifestError("manifest 拒绝：" + "；".join(errors))

    # Digest over the canonical PUBLIC input content (no internal fields);
    # availability is decided downstream from disclosed_at_raw.
    for inp in inputs:
        inp.pop("disclosed_dt", None)
    digest = compute_manifest_digest(inputs)
    claimed = manifest.get("manifest_digest")
    if claimed is not None and claimed != digest:
        raise ManifestError(f"manifest_digest 不匹配：声称 {claimed}，实际 {digest}")
    return inputs, header, digest


# --- availability & lookup --------------------------------------------------


class _Avail:
    """Availability-filtered view over the manifest for one computation."""

    def __init__(self, inputs: List[Dict[str, Any]], cutoff: datetime):
        self.inputs = inputs
        self.cutoff = cutoff

    def usable(self, inp: Dict[str, Any]) -> Tuple[bool, str]:
        if inp["disclosed_precision"] == "unknown":
            return False, "unknown_disclosure"
        dt, _, _ = _parse_disclosed(inp["disclosed_at_raw"])
        if dt is None:
            return False, "unknown_disclosure"
        if dt > self.cutoff:
            return False, "future_disclosure"
        if inp["currency"] != CURRENCY:
            return False, "unsupported_currency"
        return True, ""

    def find(self, metric: str, scope: str, period: Optional[Dict[str, Any]],
             kind: Optional[str] = None, window: Optional[str] = None,
             fiscal_year: Optional[int] = None) -> List[Dict[str, Any]]:
        out = []
        for inp in self.inputs:
            if inp["metric"] != metric:
                continue
            if scope is not None and inp["scope"] != scope:
                continue
            if period is not None and inp["period"] != period:
                continue
            if kind is not None and (inp["period"] or {}).get("kind") != kind:
                continue
            if window is not None and (inp["period"] or {}).get("window") != window:
                continue
            if fiscal_year is not None and (inp["period"] or {}).get("fiscal_year") != fiscal_year:
                continue
            out.append(inp)
        return out


def _money_yuan(inp: Dict[str, Any]) -> Decimal:
    return Decimal(inp["normalized_value"]) * MONEY_UNITS[inp["unit"]]


def _as_float(q: Decimal) -> float:
    return float(format(q, "f"))


def _period_label(period: Optional[Dict[str, Any]]) -> str:
    """Complete period identity. TTM spans carry their own dates (two TTM
    windows can end in the same year); window+fiscal_year uniquely fixes
    the start/end of every other flow kind."""
    if not period:
        return "assumption"
    if period["kind"] == "snapshot":
        return f"snapshot:{period['period_end']}"
    if period["kind"] == "ttm":
        return f"ttm:{period['period_start']}:{period['period_end']}"
    return f"{period['kind']}:{period['window']}:{period['fiscal_year']}"


def _result_key(family: str, metric: str, scope: str, period: Optional[Dict[str, Any]]) -> str:
    """Result identity = family + metric + scope + complete period identity.

    Same-period consolidated and parent results are BOTH kept — sorting
    only stabilizes output order, it must never decide which one survives.
    """
    return f"{family}:{metric}:{scope}:{_period_label(period)}"


def _rule_key(rule: str, scope: str, period: Optional[Dict[str, Any]]) -> str:
    """Identity for rule-named results (margin/ocf/net-cash): rule name
    already encodes the metric — scope + complete period complete it."""
    return f"{rule}:{scope}:{_period_label(period)}"


def _available_from(used: List[Dict[str, Any]]) -> Tuple[Optional[str], str, List[str]]:
    dts, precs = [], []
    for inp in used:
        dt, prec, _ = _parse_disclosed(inp["disclosed_at_raw"])
        if dt is not None:
            dts.append(dt)
        precs.append(prec)
    return (*_fmt_available(dts, precs), [inp["provenance"] for inp in used])


def _entry(status: str, key: str, label: str, reason_code: str = "", reason: str = "",
           value=None, unit: str = "", display: str = "", scope: str = "",
           period: Optional[Dict[str, Any]] = None, used: Optional[List[Dict[str, Any]]] = None,
           rule: str = "", limitations: Optional[List[str]] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "label": label, "status": status, "reason_code": reason_code, "reason": reason,
    }
    if status == STATUS_OK:
        avail_d, avail_p, provs = _available_from(used or [])
        entry.update({
            "value": value, "unit": unit, "display_hint": display, "currency": CURRENCY,
            "scope": scope, "period": period,
            "available_date": avail_d, "available_date_precision": avail_p,
            "derivation": {
                "rule": rule, "formula_version": FORMULA_VERSION,
                "input_ids": [i["input_id"] for i in used],
                "inputs": [{
                    "input_id": i["input_id"], "metric": i["metric"],
                    "normalized_value": i["normalized_value"], "unit": i["unit"],
                    "period": i["period"], "provenance": i["provenance"],
                } for i in used],
            },
            "provenance_refs": provs,
        })
    if limitations:
        entry["limitations"] = limitations
    return entry


def _slot_or_unknown(avail: _Avail, candidates: List[Dict[str, Any]], key, label, scope, period):
    """Resolve one input slot.

    Returns ``(input, None)`` when a usable candidate exists, else
    ``(None, unknown_entry)`` explaining the deterministic reason.
    """
    for cand in candidates:
        ok, why = avail.usable(cand)
        if ok:
            return cand, None
        reason = {
            "future_disclosure": "依赖输入披露晚于分析时点，按 as-of 排除",
            "unknown_disclosure": "依赖输入披露时间未知，不能产生 ok 数值",
            "unsupported_currency": "依赖输入币种非 CNY（D1 不做 FX 换算）",
        }[why]
        return None, _entry(STATUS_UNKNOWN, key, label, reason_code=why, reason=reason,
                            scope=scope, period=period)
    return None, _entry(STATUS_UNKNOWN, key, label, reason_code="missing_input",
                        reason="缺少匹配输入（未提供该科目/期间）", scope=scope, period=period)


# --- formulas ---------------------------------------------------------------


def _compute_yoy(avail: _Avail, metrics: Dict[str, Any]) -> None:
    """YoY over EXACT comparable window families, as-of selected.

    - Window family = kind + window + start/end MONTH-DAY: a TTM ending
      2024-09-30 only pairs with a TTM ending 2023-09-30 — "fiscal_year
      differs by one" alone proves nothing about comparability.
    - Availability filtering runs BEFORE year selection: a not-yet-disclosed
      FY2025 input can never displace an as-of computable FY2024 result;
      it is reported separately as an excluded gap.
    """
    groups: Dict[Tuple[str, str, str, str, str, str], List[Dict[str, Any]]] = {}
    for inp in avail.inputs:
        if inp["metric"] in FLOW_MONEY_METRICS:
            p = inp["period"]
            start_md = p["period_start"][5:]
            end_md = p["period_end"][5:]
            groups.setdefault(
                (inp["metric"], inp["scope"], p["kind"], p["window"], start_md, end_md),
                []).append(inp)
    for (metric, scope, kind, window, _smd, _emd), members in sorted(groups.items()):
        usable = [m for m in members if avail.usable(m)[0]]
        excluded = [m for m in members if not avail.usable(m)[0]]
        if usable:
            # As-of selection: only inputs knowable at the analysis point
            # compete for "current year" — a future FY2025 can never
            # displace a computable FY2024 result.
            t_year = max(m["period"]["fiscal_year"] for m in usable)
        else:
            # Nothing in this family is as-of available: report the gap on
            # the latest DECLARED member as the deterministic representative.
            t_year = max(m["period"]["fiscal_year"] for m in members)
        t_period = next(m["period"] for m in members
                        if m["period"]["fiscal_year"] == t_year)
        key = _result_key("yoy_growth", metric, scope, t_period)
        label = f"{METRIC_LABELS_ZH[metric]} 同比（{scope}，{_period_label(t_period)}）"
        t_cands = [m for m in members if m["period"]["fiscal_year"] == t_year]
        t_inp, t_entry = _slot_or_unknown(avail, t_cands, key, label, scope, t_period)
        if t_inp is None:
            gap = t_entry
            later = [m for m in excluded if m["period"]["fiscal_year"] > t_year]
            if later and usable:
                gap = dict(t_entry)
                gap["reason"] = (t_entry["reason"] +
                                 f"；另有 {len(later)} 个更晚年度输入未披露（{t_year + 1}+），"
                                 "已单列排除、不影响当时可算结果")
            metrics[key] = gap
            continue
        prior_cands = [m for m in members if m["period"]["fiscal_year"] == t_year - 1]
        prior_inp, prior_entry = _slot_or_unknown(
            avail, prior_cands, key, label, scope,
            prior_cands[0]["period"] if prior_cands else None)
        if prior_inp is None:
            if prior_cands:
                metrics[key] = prior_entry
            else:
                entry = _entry(STATUS_UNKNOWN, key, label, reason_code="missing_prior_year",
                               reason=f"缺少 {t_year - 1} 年同窗口输入（窗口身份=同起止月日、相隔一年）",
                               scope=scope, period=t_period)
                later = [m for m in excluded if m["period"]["fiscal_year"] > t_year]
                if later:
                    entry["reason"] += f"；另有 {len(later)} 个更晚年度输入未披露（已单列排除）"
                metrics[key] = entry
            continue
        base = _money_yuan(prior_inp)
        if base <= 0:
            metrics[key] = _entry(STATUS_NOT_APPLICABLE, key, label, reason_code="invalid_base",
                                  reason=f"{t_year - 1} 年基数为零/负（{format(base.normalize(), 'f')} 元），同比无意义",
                                  scope=scope, period=t_period,
                                  limitations=[f"基数原始值：{prior_inp['normalized_value']} {prior_inp['unit']}"])
            continue
        cur = _money_yuan(t_inp)
        growth = (cur / base - 1).quantize(_ROUND_FRACTION, rounding=ROUND_HALF_UP)
        metrics[key] = _entry(
            STATUS_OK, key, label, value=_as_float(growth), unit=FRACTION_UNIT, display="percent",
            scope=scope, period=t_period, used=[t_inp, prior_inp], rule="yoy_growth")


def _compute_margin(avail: _Avail, metrics: Dict[str, Any]) -> None:
    for profit_metric in ("net_profit", "net_profit_total"):
        profits = [i for i in avail.inputs if i["metric"] == profit_metric]
        for p_inp in profits:
            p_ok, why = avail.usable(p_inp)
            period = p_inp["period"]
            key = _rule_key(f"margin_{profit_metric}", p_inp["scope"], period)
            label = f"{METRIC_LABELS_ZH[profit_metric]}利润率（{p_inp['scope']}，{_period_label(period)}，分子口径={METRIC_LABELS_ZH[profit_metric]}）"
            if not p_ok:
                metrics[key] = _entry(STATUS_UNKNOWN, key, label, reason_code=why,
                                      reason="分子输入不可得（披露未知/晚于分析时点/币种不支持）",
                                      scope=p_inp["scope"], period=period)
                continue
            rev_cands = avail.find("revenue", p_inp["scope"], period)
            rev_inp, rev_entry = _slot_or_unknown(avail, rev_cands, key, label, p_inp["scope"], period)
            if rev_inp is None:
                metrics[key] = rev_entry
                continue
            revenue = _money_yuan(rev_inp)
            if revenue <= 0:
                metrics[key] = _entry(STATUS_NOT_APPLICABLE, key, label, reason_code="nonpositive_revenue",
                                      reason="营业收入为零/负，利润率不适用", scope=p_inp["scope"], period=period)
                continue
            margin = (_money_yuan(p_inp) / revenue).quantize(_ROUND_FRACTION, rounding=ROUND_HALF_UP)
            limitations = None
            if _money_yuan(p_inp) < 0:
                limitations = ["负利润下的利润率仅作记录，不构成质量判断"]
            metrics[key] = _entry(
                STATUS_OK, key, label, value=_as_float(margin), unit=FRACTION_UNIT, display="percent",
                scope=p_inp["scope"], period=period, used=[p_inp, rev_inp],
                rule=f"margin_{profit_metric}", limitations=limitations)


def _compute_ocf_ratio(avail: _Avail, metrics: Dict[str, Any]) -> None:
    ocfs = [i for i in avail.inputs if i["metric"] == "ocf"]
    for o_inp in ocfs:
        o_ok, why = avail.usable(o_inp)
        period = o_inp["period"]
        # Prefer net_profit_total (same consolidated scope); attributable
        # profit is a SEPARATE output name — never silently interchangeable.
        # Candidates that exist but are unusable (future/unknown/currency)
        # must surface THAT reason, not "missing".
        total_cands = avail.find("net_profit_total", o_inp["scope"], period)
        total_ok = [i for i in total_cands if avail.usable(i)[0]]
        attrib_cands = avail.find("net_profit", o_inp["scope"], period)
        attrib_ok = [i for i in attrib_cands if avail.usable(i)[0]]
        if total_ok:
            np_slot, name = total_ok[0], "ocf_to_net_profit_total"
            note = None
        elif attrib_ok:
            np_slot, name = attrib_ok[0], "ocf_to_net_profit_attributable"
            note = "归母净利润口径（合并总净利润未提供），非 IFRS 现金含量对照的同范围口径"
        else:
            key = _rule_key("ocf_to_net_profit_total", o_inp["scope"], period)
            all_cands = total_cands or attrib_cands
            if all_cands:
                _, why = avail.usable(all_cands[0])
                metrics[key] = _entry(
                    STATUS_UNKNOWN, key, "经营现金流/净利润", reason_code=why,
                    reason="同期间净利润输入不可得（披露未知/晚于分析时点/币种不支持）",
                    scope=o_inp["scope"], period=period)
            else:
                metrics[key] = _entry(
                    STATUS_UNKNOWN, key, "经营现金流/净利润", reason_code="missing_input",
                    reason="缺少同期间净利润（优先 net_profit_total，其次 net_profit）",
                    scope=o_inp["scope"], period=period)
            continue
        if not o_ok:
            key = _rule_key(name, o_inp["scope"], period)
            metrics[key] = _entry(STATUS_UNKNOWN, key, "经营现金流/净利润", reason_code=why,
                                  reason="OCF 输入不可得", scope=o_inp["scope"], period=period)
            continue
        denom = _money_yuan(np_slot)
        if denom <= 0:
            key = _rule_key(name, o_inp["scope"], period)
            metrics[key] = _entry(
                STATUS_NOT_APPLICABLE, key, "经营现金流/净利润", reason_code="nonpositive_net_profit",
                reason="净利润为零/负，比率不适用", scope=o_inp["scope"], period=period,
                limitations=[f"OCF 原始值：{o_inp['normalized_value']} {o_inp['unit']}；"
                             f"净利润原始值：{np_slot['normalized_value']} {np_slot['unit']}"])
            continue
        ratio = (_money_yuan(o_inp) / denom).quantize(_ROUND_FRACTION, rounding=ROUND_HALF_UP)
        key = _rule_key(name, o_inp["scope"], period)
        metrics[key] = _entry(
            STATUS_OK, key, "经营现金流/净利润", value=_as_float(ratio), unit=FRACTION_UNIT,
            display="percent", scope=o_inp["scope"], period=period, used=[o_inp, np_slot],
            rule=name, limitations=[note] if note else None)


DEBT_METRICS = ("short_term_borrowing", "long_term_borrowing", "bonds_payable",
                "non_current_debt_due_within_1y")


def _compute_net_cash(avail: _Avail, metrics: Dict[str, Any]) -> None:
    cash_inputs = [i for i in avail.inputs if i["metric"] == "cash"]
    debt_inputs = [i for i in avail.inputs if i["metric"] in DEBT_METRICS]
    # Group by (scope, period_end): all six must exist at the same date.
    groups: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    for inp in cash_inputs + debt_inputs:
        g = groups.setdefault((inp["scope"], inp["period"]["period_end"]), {})
        g[inp["metric"]] = inp
    for (scope, period_end), g in sorted(groups.items()):
        key = _rule_key("net_cash_limited_scope", scope,
                        {"kind": "snapshot", "period_end": period_end})
        label = f"净现金（限定口径，{scope}，snapshot {period_end}）"
        used: List[Dict[str, Any]] = []
        for m in ("cash", *DEBT_METRICS):
            cand = g.get(m)
            if cand is None:
                cands = avail.find(m, scope, {"kind": "snapshot", "period_end": period_end})
                cand = cands[0] if cands else None
            if cand is None:
                metrics[key] = _entry(
                    STATUS_UNKNOWN, key, label, reason_code="missing_input",
                    reason=f"限定口径要求现金与四项有息债务全部显式披露，缺 {METRIC_LABELS_ZH[m]}"
                           f"（未列报≠0）", scope=scope,
                    period={"kind": "snapshot", "period_end": period_end})
                used = []
                break
            cand_u, cand_entry = _slot_or_unknown(
                avail, [cand], key, label, scope,
                {"kind": "snapshot", "period_end": period_end})
            if cand_u is None:
                metrics[key] = cand_entry
                used = []
                break
            used.append(cand_u)
        if not used:
            continue
        cash = _money_yuan(used[0])
        debts = sum((_money_yuan(u) for u in used[1:]), Decimal("0"))
        net = (cash - debts).quantize(_ROUND_MONEY, rounding=ROUND_HALF_UP)
        metrics[key] = _entry(
            STATUS_OK, key, label, value=_as_float(net), unit="CNY:yuan", display="money",
            scope=scope, period={"kind": "snapshot", "period_end": period_end},
            used=used, rule="net_cash_limited_scope",
            limitations=[NET_CASH_SCOPE_NOTE] + (
                ["结果为负：该限定口径下为净债务"] if net < 0 else []))


def _compute_scenarios(avail: _Avail, metrics: Dict[str, Any], card: Dict[str, Any]) -> None:
    eps_inputs = [i for i in avail.inputs if i["metric"] == "eps_ttm"]
    mult_inputs = {i["metric"]: i for i in avail.inputs if i["metric"] in MULTIPLE_METRICS}
    scenario: Dict[str, Any] = {"status": STATUS_NOT_REQUESTED, "reason_code": "",
                                "reason": "未提供任何情景假设（eps/倍数全缺）——不显示区间、不默认倍数"}
    if not eps_inputs and not mult_inputs:
        card["scenarios"] = scenario
        return
    if not eps_inputs or len(mult_inputs) < 3:
        card["scenarios"] = {
            "status": STATUS_UNKNOWN, "reason_code": "partial_assumptions",
            "reason": "情景假设不齐全（需要 eps_ttm 与三档倍数全部显式给定；只给一部分不计算）",
            "provided": (["eps_ttm"] if eps_inputs else []) + sorted(mult_inputs),
        }
        return
    eps_ok_list = [i for i in eps_inputs if avail.usable(i)[0]]
    if not eps_ok_list:
        why = avail.usable(eps_inputs[0])[1]
        card["scenarios"] = {
            "status": STATUS_UNKNOWN, "reason_code": why,
            "reason": "EPS 输入不可得（披露未知/晚于分析时点/币种不支持）"}
        return
    # Deterministic grouping — no implicit "first EPS / last multiple wins".
    # D1 rejects ambiguity: multiple usable EPS (different period/scope) or
    # multiples spanning scopes leave no defensible single combination.
    mult_ok: Dict[str, List[Dict[str, Any]]] = {}
    any_unusable = None
    for name in ("multiple_bear", "multiple_base", "multiple_bull"):
        ok_list = [i for i in avail.find(name, None, None) if avail.usable(i)[0]]
        bad = [i for i in avail.find(name, None, None) if not avail.usable(i)[0]]
        if not ok_list and bad:
            any_unusable = (name, avail.usable(bad[0])[1])
            break
        for m in ok_list:
            d = Decimal(m["normalized_value"])
            if d <= 0:
                any_unusable = (name, "nonpositive_multiple")
                break
        if any_unusable:
            break
        mult_ok[name] = ok_list
    if any_unusable:
        name, w = any_unusable
        reason = {"future_disclosure": "假设形成日晚于分析时点（用户给定假设也必须 as-of）",
                  "unknown_disclosure": "假设形成日未知",
                  "unsupported_currency": "假设币种非 CNY",
                  "nonpositive_multiple": "倍数必须为有限正数"}[w]
        card["scenarios"] = {"status": STATUS_UNKNOWN, "reason_code": w, "reason": f"{name}: {reason}"}
        return
    eps_scopes = sorted({i["scope"] for i in eps_ok_list})
    mult_scopes = sorted({i["scope"] for name in mult_ok for i in mult_ok[name]})
    if len(eps_ok_list) > 1 or len({i["scope"] for i in eps_ok_list}) != 1 or \
            any(len(mult_ok[n]) != 1 for n in mult_ok) or len(set(mult_scopes)) != 1 or \
            eps_scopes != mult_scopes:
        card["scenarios"] = {
            "status": STATUS_UNKNOWN, "reason_code": "ambiguous_assumptions",
            "reason": "存在多组可用 EPS/倍数（不同期间/口径无法唯一组合）——D1 拒绝隐式选择，"
                      "请提供单一口径组合",
            "eps_input_ids": sorted(i["input_id"] for i in eps_ok_list),
            "multiple_input_ids": sorted(i["input_id"] for n in mult_ok for i in mult_ok[n]),
        }
        return
    eps = eps_ok_list[0] if len(eps_ok_list) == 1 else min(
        eps_ok_list, key=lambda i: (i["input_id"]))
    tiers = {}
    for name in ("multiple_bear", "multiple_base", "multiple_bull"):
        m = mult_ok[name][0]
        tiers[name] = (m, Decimal(m["normalized_value"]))
    bear, base, bull = tiers["multiple_bear"][1], tiers["multiple_base"][1], tiers["multiple_bull"][1]
    if not (bear <= base <= bull):
        card["scenarios"] = {
            "status": STATUS_UNKNOWN, "reason_code": "tier_order_violation",
            "reason": "三档倍数必须满足 bear ≤ base ≤ bull（顺序不合规拒绝计算，不重排标签）"}
        return
    eps_d = Decimal(eps["normalized_value"])
    if eps_d <= 0:
        card["scenarios"] = {
            "status": STATUS_NOT_APPLICABLE, "reason_code": "nonpositive_earnings",
            "reason": "EPS(TTM) ≤ 0，PE 情景估值不适用"}
        return
    used = [eps] + [tiers[n][0] for n in ("multiple_bear", "multiple_base", "multiple_bull")]
    avail_d, avail_p, provs = _available_from(used)

    def tier_value(m: Dict[str, Any], d: Decimal) -> Dict[str, Any]:
        price = (eps_d * d).quantize(_ROUND_MONEY, rounding=ROUND_HALF_UP)
        return {"multiple": _as_float(d), "unit": "ratio:x",
                "value_per_share": _as_float(price), "value_unit": "CNY:yuan_per_share",
                "input_id": m["input_id"], "provenance": m["provenance"],
                "assumption_kind": "user_provided"}

    card["scenarios"] = {
        "status": STATUS_OK,
        "eps": {"value": _as_float(eps_d), "unit": eps["unit"], "period": eps["period"],
                "input_id": eps["input_id"], "provenance": eps["provenance"],
                "assumption_kind": "user_provided"},
        "bear": tier_value(*tiers["multiple_bear"]),
        "base": tier_value(*tiers["multiple_base"]),
        "bull": tier_value(*tiers["multiple_bull"]),
        "available_date": avail_d, "available_date_precision": avail_p,
        "derivation": {"rule": "scenario_valuation_pe", "formula_version": FORMULA_VERSION,
                       "input_ids": [i["input_id"] for i in used],
                       "inputs": [{"input_id": i["input_id"], "metric": i["metric"],
                                   "normalized_value": i["normalized_value"], "unit": i["unit"],
                                   "period": i["period"], "provenance": i["provenance"]}
                                  for i in used]},
        "limitations": [SCENARIO_NOTE],
    }


# --- card assembly ----------------------------------------------------------


def _overall_status(metrics: Dict[str, Any], scenarios: Dict[str, Any]) -> str:
    statuses = [m["status"] for m in metrics.values()]
    if scenarios.get("status") not in (None, STATUS_NOT_REQUESTED):
        statuses.append(scenarios["status"])
    if not statuses:
        return STATUS_NOT_REQUESTED
    if STATUS_UNKNOWN in statuses:
        return "partial" if STATUS_OK in statuses else STATUS_UNKNOWN
    if STATUS_OK in statuses:
        return "partial" if any(s != STATUS_OK for s in statuses) else "computed"
    return STATUS_NOT_APPLICABLE


def compute_financial_panel(manifest: Any, *, instrument: str, instrument_type: str,
                            analysis_date: str) -> Dict[str, Any]:
    """Build the panel card from a manifest + trusted caller context.

    ``instrument``/``instrument_type``/``analysis_date`` are trusted
    parameters of the calling context — the manifest cannot override them
    (a mismatching manifest header is rejected).
    """
    ad = _parse_date_strict(analysis_date) if isinstance(analysis_date, str) else None
    if ad is None:
        raise ManifestError(f"analysis_date 必须是合法 YYYY-MM-DD（可信调用上下文参数）：{analysis_date!r}")
    if instrument_type not in ("stock", "index"):
        raise ManifestError("instrument_type 必须是 stock 或 index（可信参数，不从代码猜测）")
    inputs, header, digest = load_manifest(manifest)
    if "instrument" in header and str(header["instrument"]) != str(instrument):
        raise ManifestError(f"manifest instrument {header['instrument']!r} 与可信参数 {instrument!r} 不一致")
    if "instrument_type" in header and header["instrument_type"] != instrument_type:
        raise ManifestError(f"manifest instrument_type 与可信参数不一致（{header['instrument_type']!r} vs {instrument_type!r}）")

    cutoff = _sh_end_of_day(ad)
    metrics: Dict[str, Any] = {}
    card: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "formula_version": FORMULA_VERSION,
        "instrument": str(instrument),
        "instrument_type": instrument_type,
        "analysis_date": ad.isoformat(),
        "analysis_cutoff": cutoff.isoformat(),
        "manifest_digest": digest,
        "inputs": [_public_input(i) for i in inputs],
        "metrics": metrics,
        "scenarios": {},
        "limitations": [],
        "overall_status": "",
    }

    if instrument_type == "index":
        card["overall_status"] = STATUS_NOT_APPLICABLE
        card["scenarios"] = {"status": STATUS_NOT_APPLICABLE, "reason_code": "index_not_applicable",
                             "reason": "指数不适用个股财务报表与个股倍数公式"}
        card["limitations"] = ["指数标的：财务面板整体不适用（未套用任何个股公式）。"]
        return card

    avail = _Avail(inputs, cutoff)
    _compute_yoy(avail, metrics)
    _compute_margin(avail, metrics)
    _compute_ocf_ratio(avail, metrics)
    _compute_net_cash(avail, metrics)
    _compute_scenarios(avail, metrics, card)
    card["overall_status"] = _overall_status(metrics, card["scenarios"])
    return card


def _public_input(inp: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in inp.items() if k != "disclosed_dt"}


# --- shared rendering (Web / Markdown / PDF / CLI) --------------------------

_PCT = Decimal("100")


def _render_value(entry: Dict[str, Any]) -> str:
    if entry.get("display_hint") == "percent":
        return f"{(Decimal(str(entry['value'])) * _PCT).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
    if entry.get("unit") == "CNY:yuan":
        return f"{entry['value']:,.2f} 元"
    if entry.get("unit") == "CNY:yuan_per_share":
        return f"{entry['value']:,.2f} 元/股"
    return str(entry.get("value"))


def render_financial_panel_md(card: Any) -> str:
    """Shared Markdown block for Web / Markdown / PDF（三出口同一渲染）.

    Old reports (no panel) show 未记录 — nothing fabricated.
    """
    if not isinstance(card, dict) or card.get("schema_version") != SCHEMA_VERSION:
        return ("**财务面板（可复算）**: 未记录（此报告由旧版本生成，或本次运行未注入离线面板）。\n"
                "（本面板为确定性算术与显式输入的可复算记录，不代表投资准确率）")
    lines = [
        f"**财务面板（可复算）**: 状态 `{card.get('overall_status')}`"
        f"（标的: {card.get('instrument')}；分析时点: {card.get('analysis_date')}；"
        f"公式版本: {card.get('formula_version')}；manifest: `{card.get('manifest_digest')}`）",
    ]
    if card.get("instrument_type") == "index":
        lines.append("- 指数标的：财务面板整体不适用（未套用任何个股公式）。")
        lines.append("> 本面板为确定性算术与显式输入的可复算记录，不代表投资准确率。")
        return "\n".join(lines)

    inputs = card.get("inputs") or []
    if inputs:
        lines.append("- 输入表（显式清单，逐条出处）:")
        for i in inputs:
            period = i.get("period")
            period_s = _period_label(period) if period else "假设（无期间）"
            lines.append(
                f"  - `{i['input_id']}` {METRIC_LABELS_ZH.get(i['metric'], i['metric'])} = "
                f"{i['normalized_value']} {i['unit']}（{i['scope']}，{period_s}，"
                f"披露: {i.get('disclosed_at_raw', 'unknown')}，出处: {i['provenance']}）")

    metrics = card.get("metrics") or {}
    if metrics:
        lines.append("- 计算表（每个数值可经依赖链复算）:")
        for key, m in metrics.items():
            if m["status"] == STATUS_OK:
                deriv = m.get("derivation") or {}
                lines.append(
                    f"  - ✅ {m['label']}: {_render_value(m)}"
                    f"（可知时点: {m.get('available_date')}；规则: {deriv.get('rule')}；"
                    f"输入: {', '.join(deriv.get('input_ids', []))}）")
            else:
                lines.append(
                    f"  - {'○' if m['status'] == STATUS_NOT_APPLICABLE else '✗'} {m['label']}: "
                    f"{m['status']}（{m.get('reason_code')}）— {m.get('reason', '')}")
            for lim in m.get("limitations") or []:
                lines.append(f"    - {lim}")

    sc = card.get("scenarios") or {}
    if sc.get("status") == STATUS_OK:
        lines.append("- 情景估值（每股，全部为用户给定假设，非财报事实）:")
        for tier in ("bear", "base", "bull"):
            t = sc[tier]
            zh = {"bear": "悲观", "base": "基准", "bull": "乐观"}[tier]
            lines.append(
                f"  - {zh}: EPS {sc['eps']['value']} × 倍数 {t['multiple']} = "
                f"{t['value_per_share']:,.2f} 元/股（{t['input_id']}）")
    else:
        note = {"not_requested": "未提供情景假设——不显示区间、不默认倍数",
                "unknown": f"情景不可计算：{sc.get('reason', '')}",
                "not_applicable": f"情景不适用：{sc.get('reason', '')}"}.get(
                    sc.get("status"), f"情景：{sc.get('status')}")
        lines.append(f"- 情景估值: {note}")

    for lim in card.get("limitations") or []:
        lines.append(f"- {lim}")
    lines.append("> 本面板为确定性算术与显式输入的可复算记录；比率存储为小数（0.1523 渲染为 15.23%，仅缩放一次）。"
                 "不代表投资准确率。")
    return "\n".join(lines)


# --- offline CLI ------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tradingagents.dataflows.financial_panel",
        description="D1 offline reproducible financial panel (no LLM, no network).")
    parser.add_argument("--manifest", required=True, help="manifest JSON 路径（显式输入清单）")
    parser.add_argument("--instrument", required=True, help="可信标的（如 600519）")
    parser.add_argument("--instrument-type", required=True, choices=["stock", "index"],
                        help="可信标的类型（不从代码猜测）")
    parser.add_argument("--analysis-date", required=True, help="分析时点 YYYY-MM-DD（可信上下文）")
    parser.add_argument("--output-dir", required=True, help="输出目录（写入 financial_panel.json/md）")
    args = parser.parse_args(argv)

    try:
        with open(args.manifest, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"manifest 读取失败: {exc}", file=sys.stderr)
        return 2
    try:
        card = compute_financial_panel(
            manifest, instrument=args.instrument, instrument_type=args.instrument_type,
            analysis_date=args.analysis_date)
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    import os
    os.makedirs(args.output_dir, exist_ok=True)
    json_path = os.path.join(args.output_dir, "financial_panel.json")
    md_path = os.path.join(args.output_dir, "financial_panel.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(card, fh, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_financial_panel_md(card) + "\n")
    print(f"overall_status={card['overall_status']} -> {json_path} , {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --- D2: run binding, resume verification, prompt projection ----------------
#
# Codex D2 implementation contract (docs/D2_CODEX_IMPLEMENTATION_CONTRACT_
# 2026-09-09.md) rules 2/4/6/8: the model only ever sees a projection of ok
# computed values and their ACTUAL eligible dependencies — the embedded raw
# input table (which retains future/unknown-excluded declarations for audit)
# never reaches a prompt. Machine paths and uploaded file bytes never enter
# prompts either.

PANEL_CONTEXT_MAX_ROWS = 15
PANEL_CONTEXT_MAX_LINE_CHARS = 220
PANEL_CONTEXT_MAX_TOTAL_CHARS = 4000

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_ITEMS = 500

INVALID_PANEL_NOTICE = (
    "⚠️ 财务面板：归属校验无效（invalid-panel）——数值一律不展示，"
    "以保存的代码表复核为准。"
)


def bind_run(card: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    """Bind a freshly computed card to THIS run's identity (injection layer).

    The pure computation never knows the run; binding is the preparation
    layer's job (contract rule 4: same inputs → same values; each fresh run
    gets a NEW run_id — cards are not byte-identical across fresh runs by
    design).
    """
    card = dict(card)
    card["run_binding"] = {"run_id": str(run_id)}
    return card


def verify_run_binding(
    card: Any,
    *,
    run_id: str,
    instrument: str,
    instrument_type: str,
    analysis_date: str,
    metadata_digest: Any = None,
) -> List[str]:
    """Return the list of provenance mismatches (empty = valid).

    Checks the trusted run context against the card, the card's own
    integrity (schema version, digest recomputed over its embedded input
    table), and — when the run metadata carries one — the INDEPENDENT
    ``run_metadata.financial_panel.manifest_digest`` anchor. A card whose
    anchor is missing cannot self-certify (D2 R1). Corruption must be
    REPORTED, never silently downgraded to "unrecorded" (contract rule 6).
    """
    problems: List[str] = []
    if not isinstance(card, dict):
        return ["面板不是对象"]
    if card.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"未知 schema_version={card.get('schema_version')!r}")
    binding = card.get("run_binding")
    if not isinstance(binding, dict) or not binding.get("run_id"):
        problems.append("缺少 run_binding.run_id（D2 之前的卡或损坏）")
    elif str(binding.get("run_id")) != str(run_id):
        problems.append(f"run_id 不符（卡 {binding.get('run_id')!r} vs 运行 {run_id!r}）")
    if str(card.get("instrument")) != str(instrument):
        problems.append(f"instrument 不符（卡 {card.get('instrument')!r} vs {instrument!r}）")
    if str(card.get("instrument_type")) != str(instrument_type):
        problems.append(f"instrument_type 不符（卡 {card.get('instrument_type')!r} vs {instrument_type!r}）")
    if str(card.get("analysis_date")) != str(analysis_date):
        problems.append(f"analysis_date 不符（卡 {card.get('analysis_date')!r} vs {analysis_date!r}）")
    if metadata_digest is None:
        problems.append("运行元数据缺少 financial_panel.manifest_digest 锚定（有卡不能自证有效）")
    elif str(metadata_digest) != str(card.get("manifest_digest")):
        problems.append(
            f"元数据摘要锚定不符（元数据 {metadata_digest!r} vs 卡 {card.get('manifest_digest')!r}）")
    if isinstance(card.get("inputs"), list):
        recomputed = compute_manifest_digest(card["inputs"])
        if recomputed != card.get("manifest_digest"):
            problems.append(
                f"manifest_digest 不自洽（声称 {card.get('manifest_digest')!r}，"
                f"按内嵌输入表复算 {recomputed!r}）")
    else:
        problems.append("卡内嵌输入表缺失或损坏（无法复验 digest）")
    return problems


def _panel_line(label: str, body: str) -> str:
    line = f"- {label}: {body}"
    if len(line) > PANEL_CONTEXT_MAX_LINE_CHARS:
        line = line[:PANEL_CONTEXT_MAX_LINE_CHARS - 1] + "…"
    return line


def _format_metric_value(entry: Dict[str, Any]) -> str:
    if entry.get("display_hint") == "percent":
        return f"{(Decimal(str(entry['value'])) * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
    if entry.get("unit") == "CNY:yuan":
        return f"{entry['value']:,.2f} 元"
    if entry.get("unit") == "CNY:yuan_per_share":
        return f"{entry['value']:,.2f} 元/股"
    return str(entry.get("value"))


def _trusted_problems(state: Any, card: Any) -> List[str]:
    """Full provenance validation of a card against the trusted STATE.

    Shared by the prompt helper (invalid → controlled notice, no numbers)
    and any caller holding a real production state. Malformed state/card
    yields explicit problems — never an unexplained KeyError, and never a
    silently-passing unbound card.
    """
    if not isinstance(state, dict):
        return ["state 不是对象"]
    if not isinstance(card, dict):
        return ["面板不是对象"]
    meta = state.get("run_metadata")
    if not isinstance(meta, dict):
        return ["state 缺少 run_metadata"]
    problems: List[str] = []
    if card.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"未知 schema_version={card.get('schema_version')!r}")
    binding = card.get("run_binding")
    state_run_id = str(meta.get("run_id") or "")
    if not isinstance(binding, dict) or not binding.get("run_id"):
        problems.append("卡缺少 run_binding.run_id（未绑定运行）")
    elif state_run_id and str(binding.get("run_id")) != state_run_id:
        problems.append(f"run_id 不符（卡 {binding.get('run_id')!r} vs 运行 {state_run_id!r}）")
    if state.get("company_of_interest") and \
            str(card.get("instrument")) != str(state.get("company_of_interest")):
        problems.append(
            f"instrument 不符（卡 {card.get('instrument')!r} vs 运行 {state.get('company_of_interest')!r}）")
    if state.get("instrument_type") and \
            str(card.get("instrument_type")) != str(state.get("instrument_type")):
        problems.append(
            f"instrument_type 不符（卡 {card.get('instrument_type')!r} vs {state.get('instrument_type')!r}）")
    if state.get("trade_date") and \
            str(card.get("analysis_date")) != str(state.get("trade_date")):
        problems.append(
            f"analysis_date 不符（卡 {card.get('analysis_date')!r} vs {state.get('trade_date')!r}）")
    fp_meta = meta.get("financial_panel")
    anchor = fp_meta.get("manifest_digest") if isinstance(fp_meta, dict) else None
    if not anchor:
        problems.append("运行元数据缺少 financial_panel.manifest_digest 锚定")
    elif str(anchor) != str(card.get("manifest_digest")):
        problems.append("元数据摘要锚定不符")
    if isinstance(card.get("inputs"), list):
        if compute_manifest_digest(card["inputs"]) != card.get("manifest_digest"):
            problems.append("manifest_digest 不自洽（内嵌输入表被篡改或损坏）")
    else:
        problems.append("卡内嵌输入表缺失或损坏")
    return problems


def panel_context_for_prompt(state: Any) -> str:
    """Bounded projection of the financial panel for decision prompts ('').

    D2 R1 hardening — the helper runs the FULL trusted-state validation
    (run_id / instrument / instrument_type / trade_date / metadata digest
    anchor / card self-digest) BEFORE reading any value:

    - any problem → controlled ``invalid-panel`` notice, NO numbers, with
      an explicit reason line (no unexplained KeyError on malformed input);
    - valid card → only ``ok`` computed values and their ACTUAL
      participating dependencies (the raw embedded input table — which
      retains future/unknown-excluded declarations for audit — never
      enters a prompt; machine paths are masked);
    - zero-ok panels still emit their deterministic limitations — the
      limitation block is assembled first and can never be squeezed out
      by value rows hitting the character budget;
    - row budget covers metric AND scenario rows together (≤15); the total
      character cap includes the truncation notice itself.
    """
    card = state.get("financial_panel") if isinstance(state, dict) else None
    if card is None:
        return ""
    problems = _trusted_problems(state, card)
    if problems:
        return f"{INVALID_PANEL_NOTICE}\n- 校验失败：{'；'.join(problems[:3])}\n"

    # Limitations / gap summary FIRST — they survive any budget pressure.
    limit_lines: List[str] = []
    non_ok = [e for e in (card.get("metrics") or {}).values()
              if isinstance(e, dict) and e.get("status") != STATUS_OK]
    ok_entries = [(k, e) for k, e in (card.get("metrics") or {}).items()
                  if isinstance(e, dict) and e.get("status") == STATUS_OK]
    if non_ok or not ok_entries:
        by_code: Dict[str, int] = {}
        for e in non_ok:
            code = str(e.get("reason_code") or e.get("status") or "?")
            by_code[code] = by_code.get(code, 0) + 1
        if by_code:
            summary = "，".join(f"{c}×{n}" for c, n in sorted(by_code.items()))
            limit_lines.append(_panel_line("未算出指标（缺口分类）", summary))
        else:
            limit_lines.append("- 未算出指标: 无 ok 数值（面板存在但全部不可计算）")
    for note in (card.get("limitations") or [])[:3]:
        limit_lines.append(_panel_line("面板限制", str(note)[:120]))

    sc = card.get("scenarios") or {}

    def _prompt_provenance(prov: Any) -> str:
        text = str(prov or "")
        if "/" in text or "\\" in text or text.startswith(("~", ".")):
            return "[路径已隐去]"
        return text

    # Scenario rows share the value-row budget with metrics.
    scenario_rows: List[str] = []
    if sc.get("status") == STATUS_OK:
        for tier, zh in (("bear", "悲观"), ("base", "基准"), ("bull", "乐观")):
            t = sc.get(tier) or {}
            if t:
                scenario_rows.append(_panel_line(
                    f"情景·{zh}（用户给定假设，非财报事实）",
                    f"{t.get('value_per_share'):,.2f} 元/股 = EPS {sc['eps']['value']} × "
                    f"{t.get('multiple')}（假设输入 {t.get('input_id')}）"))
    elif sc.get("status") == STATUS_NOT_REQUESTED:
        limit_lines.insert(0, "- 情景估值: 未提供假设（不显示区间、不默认倍数）")
    else:
        limit_lines.insert(0, _panel_line(
            "情景估值", f"{sc.get('status')}（{sc.get('reason_code')}）{sc.get('reason', '')}"))

    value_lines: List[str] = []
    for _key, e in ok_entries:
        deriv = e.get("derivation") or {}
        deps = ", ".join(
            f"{i.get('input_id')}[{_prompt_provenance(i.get('provenance'))}]"
            for i in (deriv.get("inputs") or [])[:6])
        value_lines.append(_panel_line(
            e.get("label", "?"),
            f"{_format_metric_value(e)}（{e.get('scope')}，{_period_label(e.get('period'))}，"
            f"可知 {e.get('available_date')}；依据 {deps}）"))

    budget_rows = max(PANEL_CONTEXT_MAX_ROWS - len(scenario_rows), 0)
    shown_values = value_lines[:budget_rows]
    dropped_rows = len(value_lines) - len(shown_values)

    header = "财务面板（代码复算，引用时照抄数值，不得改写或重算）："
    notice = "\n（投影已达字符上限，数值行截断明示；缺口与限制独立保留；完整代码表见运行 JSON）"

    def _assemble(rows_values: List[str]) -> str:
        parts = [header] + limit_lines + rows_values
        if dropped_rows:
            parts.append(f"- …另有 {dropped_rows} 个已计算数值行未列出（行数预算含情景行）")
        return "\n".join(parts) + "\n"

    text = _assemble(shown_values + scenario_rows)
    if len(text) > PANEL_CONTEXT_MAX_TOTAL_CHARS:
        kept = list(shown_values)
        while kept and len(_assemble(kept + scenario_rows)) + len(notice) > \
                PANEL_CONTEXT_MAX_TOTAL_CHARS:
            kept.pop()
        text = _assemble(kept + scenario_rows).rstrip("\n") + notice + "\n"
    return text[:PANEL_CONTEXT_MAX_TOTAL_CHARS]
