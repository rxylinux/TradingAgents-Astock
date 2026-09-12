"""F3: offline paired evaluation — stage A (plan/identity/splits).

Authoritative contract: ``docs/F3_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md``
(rules 1-4 for stage A). Two-stage offline flow: prepare reads explicit
features/trial-config/time-splits and emits an IMMUTABLE evaluation plan; it
never accepts label paths, never opens label files, never reads full F1
resolved records, never calls models or fetches data, and never touches the
production default flow or memory.

Stage-A invariants:

- **Feature-only schema** (rule 2): strict whitelist incl. nested keys — a
  full F1 record fails; ``feature_available_at`` is the feature-version
  availability time (never derived from F1 outcomes, never replaced by
  decided_at); both decided_at and feature_available_at must be ≤
  prediction_at; digest recomputed, claimed digests untrusted.
- **Fixed planned denominators** (rule 3): every planned repeat is a plan
  entry; arms carry RAW normalized configs whose recomputed digests must
  differ ONLY by top-level ``evidence_debate_enabled`` false→true (bool).
- **Splits & purge** (rule 4): caller-given mutually-ordered train/
  validation/holdout boundaries assigned by prediction_at; same
  instrument+type grouping; ALL cross-split closed-interval target_window
  overlaps (incl. train×holdout) purge from the EARLIER split with both IDs
  recorded; result independent of deletion order; embargo default 0 in
  explicit calendar days (Shanghai); split/embargo/window all feed the plan
  digest. Purge governs label-window overlap only — no statistical
  independence claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FEATURES = 1000
MAX_REPEATS = 100
MAX_PLANNED_ARM_RUNS = 20000

SWITCH_KEY = "evidence_debate_enabled"
SH_TZ = timezone(timedelta(hours=8))


class PlanValidationError(ValueError):
    """Prepare-stage rejection (deterministic error list). CLI exit 2."""


# ---------------------------------------------------------------------------
# Strict time primitives (F1 semantics)
# ---------------------------------------------------------------------------


# F1 时间原语直接复用（Codex F3a R1 #3：实际复用而非复制实现）
from tradingagents.evaluation.review_record import (  # noqa: E402
    _parse_date_strict,
    _parse_datetime_strict as _f1_parse_dt,
)


def _parse_time_strict(value: Any) -> Optional[Tuple[datetime, str]]:
    """F1 primitive wrapper: date → Shanghai EOD; naive datetime rejected."""
    parsed = _f1_parse_dt(value)
    return parsed if parsed is not None else None


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(obj).encode("utf-8")).hexdigest()


def _check_json_legal(value: Any, path: str, errors: List[str]) -> None:
    import math as _math
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            errors.append(f"{path}: 含无法 UTF-8 编码的字符串")
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, float):
        if not _math.isfinite(value):
            errors.append(f"{path}: 非有限数值（NaN/Infinity 拒绝）")
        return
    if isinstance(value, int):
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                errors.append(f"{path}: 字典键必须是字符串")
                continue
            _check_json_legal(v, f"{path}.{k}", errors)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_json_legal(item, f"{path}[{i}]", errors)
        return
    errors.append(f"{path}: 非法类型 {type(value).__name__}")


# ---------------------------------------------------------------------------
# Feature schema (rule 2)
# ---------------------------------------------------------------------------

_FEATURE_TOP = {"feature_id", "ticker", "instrument_type", "prediction_at",
                "feature_available_at", "decision", "target_window",
                "binary_event"}
_DECISION_KEYS = {"rating", "prediction_horizon", "decided_at"}
_FORBIDDEN_MARKERS = ("raw_return", "alpha_return", "max_adverse_excursion",
                      "observed_at", "publication_time", "maturity_date",
                      "record_available_at", "error_type", "correct_risk_flags",
                      "fees_assumption", "annotations", "outcome")


def validate_feature(raw: Any, index: int) -> Dict[str, Any]:
    where = f"features[{index}]"
    if not isinstance(raw, dict):
        raise PlanValidationError([f"{where}: 必须是 JSON 对象"])
    errors: List[str] = []
    _check_json_legal(raw, where, errors)
    extra = set(raw) - _FEATURE_TOP
    if extra:
        errors.append(f"{where}: 非白名单键 {sorted(extra)}（完整 F1 记录应失败）")
    for key in ("feature_id", "ticker"):
        if not isinstance(raw.get(key), str) or not str(raw.get(key, "")).strip():
            errors.append(f"{where}.{key}: 必须是非空字符串")
    if raw.get("instrument_type") not in ("stock", "index"):
        errors.append(f"{where}.instrument_type: 必须是 stock 或 index")
    decision = raw.get("decision")
    if not isinstance(decision, dict):
        errors.append(f"{where}.decision: 必须是对象")
        decision = {}
    else:
        if set(decision) != _DECISION_KEYS:
            errors.append(f"{where}.decision: 键集必须是 {sorted(_DECISION_KEYS)}"
                          f"（得到 {sorted(set(decision))}）")
        for key in ("rating", "prediction_horizon", "decided_at"):
            if not isinstance(decision.get(key), str) or not decision[key].strip():
                errors.append(f"{where}.decision.{key}: 必须是非空字符串")
    window = raw.get("target_window")
    if not isinstance(window, dict) or set(window) != {"start", "end"}:
        errors.append(f"{where}.target_window: 必须是 {{start,end}}")
    else:
        ws = _parse_date_strict(window.get("start"))
        we = _parse_date_strict(window.get("end"))
        if ws is None or we is None:
            errors.append(f"{where}.target_window: start/end 必须是 YYYY-MM-DD")
        elif ws > we:
            errors.append(f"{where}.target_window: start > end")
    if "binary_event" in raw and raw["binary_event"] is not None:
        be = raw["binary_event"]
        # 严格嵌套白名单（Codex F3a R1 #1）：额外键拒绝——未来答案可换任何
        # 键名，不能靠枚举 outcome 关键字替代字段边界。
        if not isinstance(be, dict):
            errors.append(f"{where}.binary_event: 必须是对象或 null")
        else:
            be_extra = set(be) - {"event", "resolve_by"}
            if be_extra:
                errors.append(f"{where}.binary_event: 非白名单键 {sorted(be_extra)}")
            if not isinstance(be.get("event"), str) or not be.get("event", "").strip():
                errors.append(f"{where}.binary_event.event: 必须是非空字符串")
            if _parse_date_strict(be.get("resolve_by")) is None:
                errors.append(f"{where}.binary_event.resolve_by: 非法日期")
    pred = _parse_time_strict(raw.get("prediction_at"))
    avail = _parse_time_strict(raw.get("feature_available_at"))
    decided = _parse_time_strict(decision.get("decided_at"))
    if pred is None:
        errors.append(f"{where}.prediction_at: 非法时间（F1 严格原语）")
    if avail is None:
        errors.append(f"{where}.feature_available_at: 非法/缺失——特征版本可知时间"
                      "不得用 decided_at 替代或从 F1 结果倒推")
    if decided is None:
        errors.append(f"{where}.decision.decided_at: 非法时间")
    if pred and avail and avail[0] > pred[0]:
        errors.append(f"{where}: feature_available_at 晚于 prediction_at")
    if pred and decided and decided[0] > pred[0]:
        errors.append(f"{where}: decided_at 晚于 prediction_at")
    # 派生统计禁入（关键名即拒——outcome/annotation 及派生统计不得进入特征）
    flat = json.dumps(raw, ensure_ascii=False, default=str)
    for marker in _FORBIDDEN_MARKERS:
        if f'"{marker}"' in flat:
            errors.append(f"{where}: 特征含 outcome/annotation 派生字段 {marker!r}（禁入）")
    if errors:
        raise PlanValidationError(errors)
    return json.loads(json.dumps(raw, ensure_ascii=False))  # isolated copy


def load_features(source: Any) -> List[Dict[str, Any]]:
    if isinstance(source, str):
        if len(source.encode("utf-8")) > MAX_FILE_BYTES:
            raise PlanValidationError([f"特征文件超过 {MAX_FILE_BYTES} 字节上限"])
        raws: List[Any] = []
        for i, line in enumerate(source.splitlines()):
            if not line.strip():
                continue
            try:
                raws.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise PlanValidationError([f"line {i+1}: JSON 解析失败（{exc}）"]) from exc
    elif isinstance(source, list):
        raws = source
    else:
        raise PlanValidationError(["features 必须是 JSONL 文本或已解析列表"])
    if len(raws) > MAX_FEATURES:
        raise PlanValidationError([f"特征条目 {len(raws)} 超过 {MAX_FEATURES} 上限"])
    out = []
    for i, raw in enumerate(raws):
        out.append(validate_feature(raw, i))
    return out


def feature_snapshot(feature: Dict[str, Any]) -> Dict[str, Any]:
    """Frozen per-pair shared snapshot (rule 3: shared digest must match)."""
    return {
        "feature_id": feature["feature_id"],
        "ticker": feature["ticker"],
        "instrument_type": feature["instrument_type"],
        "prediction_at": feature["prediction_at"],
        "feature_available_at": feature["feature_available_at"],
        "decision": dict(feature["decision"]),
        "target_window": dict(feature["target_window"]),
        "binary_event": feature.get("binary_event"),
    }


# ---------------------------------------------------------------------------
# Trial config / arms (rule 3): raw normalized configs, recomputed digests,
# exactly ONE top-level bool diff (E false→true)
# ---------------------------------------------------------------------------


def _normalize_config(config: Any) -> Dict[str, Any]:
    if not isinstance(config, dict):
        raise PlanValidationError(["normalized_config 必须是对象"])
    errors: List[str] = []
    _check_json_legal(config, "normalized_config", errors)
    if errors:
        raise PlanValidationError(errors)
    return json.loads(_canonical_json(config))


def _typed_config_diff(base: Dict[str, Any], cand: Dict[str, Any]) -> set:
    """Type-SENSITIVE canonical-JSON comparison (Codex F3a R1 #2).

    Python's ``!=`` treats True == 1 and missing == None-equal via ``get``;
    we compare (key, type-tag, canonical-value) triples so a missing key,
    an explicit null, bool True and int 1 are all DIFFERENT.
    """
    def _type_key(value: Any) -> str:
        if isinstance(value, bool):
            return "bool"
        if value is None:
            return "null"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "str"
        if isinstance(value, list):
            return "list"
        if isinstance(value, dict):
            return "dict"
        return type(value).__name__

    diff: set = set()
    for k in set(base) | set(cand):
        bv, cv = base.get(k, _MISSING), cand.get(k, _MISSING)
        if bv is _MISSING or cv is _MISSING:
            diff.add(k)  # 缺失 ≠ 显式 null/任意值
            continue
        if _type_key(bv) != _type_key(cv) or _canonical_json(bv) != _canonical_json(cv):
            diff.add(k)
    return diff


class _Missing:
    pass


_MISSING = _Missing()


def validate_arms(trials: Any) -> Dict[str, Any]:
    """Validate trial config → {pair_id: {baseline_cfg, candidate_cfg,
    repeat_count}}; reject any config difference beyond the single switch.

    Codex F3a R1 #2: type-sensitive exact diff (missing≠null, bool≠int);
    CLAIMED config digests must be recomputed and compared (generated only
    when absent); bad arm objects reject (never AttributeError); pair_id
    must be a non-empty string.
    """
    if not isinstance(trials, dict):
        raise PlanValidationError(["trials 必须是对象 {pair_id: trial}"])
    out: Dict[str, Any] = {}
    for pair_id, trial in trials.items():
        if not isinstance(pair_id, str) or not pair_id.strip():
            raise PlanValidationError([f"trials 键 {pair_id!r}: 必须是非空字符串"])
        where = f"trials[{pair_id!r}]"
        if not isinstance(trial, dict):
            raise PlanValidationError([f"{where}: 必须是对象"])
        rc = trial.get("repeat_count")
        if isinstance(rc, bool) or not isinstance(rc, int) or rc < 1:
            raise PlanValidationError([f"{where}.repeat_count: 必须是正整数（非 bool）"])
        if rc > MAX_REPEATS:
            raise PlanValidationError([f"{where}.repeat_count: 超过 {MAX_REPEATS} 上限"])
        arms = trial.get("arms")
        if not isinstance(arms, dict) or set(arms) != {"baseline", "candidate"}:
            raise PlanValidationError([f"{where}.arms: 必须恰好含 baseline/candidate"])
        for arm_name in ("baseline", "candidate"):
            arm = arms.get(arm_name)
            if not isinstance(arm, dict):
                raise PlanValidationError(
                    [f"{where}.arms.{arm_name}: 必须是对象（非 {type(arm).__name__}）"])
        base_cfg = _normalize_config(arms["baseline"].get("normalized_config"))
        cand_cfg = _normalize_config(arms["candidate"].get("normalized_config"))
        # 唯一允许差异：顶层 evidence_debate_enabled false→true（必须 bool）
        diff = _typed_config_diff(base_cfg, cand_cfg)
        if diff != {SWITCH_KEY}:
            raise PlanValidationError(
                [f"{where}: 配置差集 {sorted(diff)} 不等于仅 [{SWITCH_KEY}]——"
                 "模型版本/预算/数据/种子等必须全相同（类型敏感比较："
                 "缺失≠null、bool≠int）"])
        bv, cv = base_cfg.get(SWITCH_KEY), cand_cfg.get(SWITCH_KEY)
        if isinstance(bv, bool) and isinstance(cv, bool) and bv is False and cv is True:
            pass
        else:
            raise PlanValidationError(
                [f"{where}: {SWITCH_KEY} 必须 bool false→true（得到 {bv!r}→{cv!r}）"])
        # 声明摘要必须复算比对；未提供才生成（Codex F3a R1 #2）
        digests = {}
        for arm_name, cfg in (("baseline", base_cfg), ("candidate", cand_cfg)):
            recomputed = _digest(cfg)
            claimed = arms[arm_name].get("config_digest")
            if claimed is not None and claimed != recomputed:
                raise PlanValidationError(
                    [f"{where}.arms.{arm_name}.config_digest 失配"
                     f"（声称 {claimed!r}，复算 {recomputed!r}）"])
            digests[arm_name] = recomputed
        out[pair_id] = {
            "pair_id": pair_id,
            "repeat_count": rc,
            "baseline_config": base_cfg,
            "baseline_config_digest": digests["baseline"],
            "candidate_config": cand_cfg,
            "candidate_config_digest": digests["candidate"],
        }
    return out


# ---------------------------------------------------------------------------
# Splits + purge (rule 4)
# ---------------------------------------------------------------------------


REQUIRED_SPLITS = ("train", "validation", "holdout")


def validate_splits(splits: Any, embargo: int) -> Dict[str, Any]:
    """Absolute-time-only split validation (Codex F3a R1 #3).

    All boundary comparisons use PARSED datetimes (never raw-string lexical
    order); train/validation/holdout required by name; adjacent splits may
    not touch (closed intervals: equal endpoints REJECTED); reverse-time
    named labels (train after validation in absolute time) rejected.
    """
    if isinstance(embargo, bool) or not isinstance(embargo, int) or embargo < 0:
        raise PlanValidationError(["embargo 必须是非负整数日历日（默认 0）"])
    if not isinstance(splits, dict):
        raise PlanValidationError(["splits 必须是对象"])
    unknown = set(splits) - set(REQUIRED_SPLITS)
    if unknown:
        raise PlanValidationError(
            [f"splits 含未知名称 {sorted(unknown)}——只允许 "
             f"{list(REQUIRED_SPLITS)}"])
    missing = set(REQUIRED_SPLITS) - set(splits)
    if missing:
        raise PlanValidationError([f"splits 缺少必需名称 {sorted(missing)}"])
    out: Dict[str, Any] = {}
    parsed: Dict[str, Tuple[datetime, datetime]] = {}
    for name in REQUIRED_SPLITS:
        split = splits[name]
        if not isinstance(split, dict) or set(split) != {"start", "end"}:
            raise PlanValidationError([f"splits[{name!r}]: 必须是 {{start,end}}"])
        ss = _parse_time_strict(split.get("start"))
        se = _parse_time_strict(split.get("end"))
        if ss is None or se is None:
            raise PlanValidationError([f"splits[{name!r}]: 边界非法时间"])
        if ss[0] > se[0]:
            raise PlanValidationError([f"splits[{name!r}]: start > end"])
        out[name] = {"start": split["start"], "end": split["end"]}
        parsed[name] = (ss[0], se[0])
    # 固定语义顺序 train→validation→holdout；绝对时间必须严格递增且不相接
    for a, b in zip(REQUIRED_SPLITS, REQUIRED_SPLITS[1:]):
        a_end = parsed[a][1]
        b_start = parsed[b][0]
        if b_start < a_end:
            raise PlanValidationError(
                [f"切分时间顺序非法：{b} 开始（{b_start.isoformat()}）早于 {a} "
                 f"结束（{a_end.isoformat()}）——按绝对时间比较"])
        if b_start == a_end:
            raise PlanValidationError(
                [f"切分相接：{b} 开始 == {a} 结束（闭区间语义下拒绝相等端点）"])
    return {"splits": out, "order": list(REQUIRED_SPLITS), "embargo_days": embargo}


def assign_split(feature: Dict[str, Any], splits: Dict[str, Any]) -> Optional[str]:
    pred = _parse_time_strict(feature["prediction_at"])
    for name in splits["order"]:
        s = _parse_time_strict(splits["splits"][name]["start"])[0]
        e = _parse_time_strict(splits["splits"][name]["end"])[0]
        if s <= pred[0] <= e:
            return name
    return None


def purge_overlaps(features: List[Dict[str, Any]],
                   split_of: Dict[str, str],
                   splits: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """All cross-split closed-interval purge, same instrument+type grouping.

    Deterministic: computed over the FULL original set (result independent of
    deletion order); earlier split loses the overlapping record; embargo
    extends the later sample's window start by N calendar days (Shanghai).
    Returns (kept, purged_records_with_both_ids).
    """
    order_idx = {name: i for i, name in enumerate(splits["order"])}
    # Group by (instrument_type, ticker) — cross-group never purges
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for f in features:
        groups.setdefault((f["instrument_type"], f["ticker"]), []).append(f)

    purged: List[Dict[str, Any]] = []
    removed_ids = set()

    for key, members in sorted(groups.items()):
        # O(n²) within a group — bounded by MAX_FEATURES; deterministic
        # because we iterate sorted stable identities and only mark removal
        # on the EARLIER split, computed over the full original set.
        for early in sorted(members, key=lambda f: f["feature_id"]):
            if early["feature_id"] in removed_ids:
                continue
            es = split_of.get(early["feature_id"])
            if es is None:
                continue
            e_ws = _parse_date_strict(early["target_window"]["start"])
            e_we = _parse_date_strict(early["target_window"]["end"])
            for late in sorted(members, key=lambda f: f["feature_id"]):
                if late["feature_id"] == early["feature_id"]:
                    continue
                ls = split_of.get(late["feature_id"])
                if ls is None or ls == es:
                    continue
                if order_idx[ls] <= order_idx[es]:
                    continue  # only later split's overlap removes from earlier
                l_ws = _parse_date_strict(late["target_window"]["start"])
                l_we = _parse_date_strict(late["target_window"]["end"])
                if l_ws is None or l_we is None or e_ws is None or e_we is None:
                    continue
                # embargo: extend later window start forward N calendar days
                eff_l_ws = l_ws - timedelta(days=splits["embargo_days"])
                # closed interval: equal boundary counts as overlap
                if not (e_we < eff_l_ws or e_ws > l_we):
                    removed_ids.add(early["feature_id"])
                    purged.append({
                        "removed_feature_id": early["feature_id"],
                        "conflicts_with_feature_id": late["feature_id"],
                        "earlier_split": es, "later_split": ls,
                        "reason": "purged_overlap",
                    })
                    break

    kept = [f for f in features if f["feature_id"] not in removed_ids]
    return kept, purged


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------


def build_plan(features: List[Dict[str, Any]], trials: Dict[str, Any],
               splits: Dict[str, Any], embargo: int = 0,
               input_digests: Any = None) -> Dict[str, Any]:
    """Build the immutable plan (Codex F3a R1 #4/#5/#6 hardening).

    - FULL-INPUT identity first: duplicate feature_id (any ticker/split)
      rejects BEFORE any snapshot work — not discovered during entry build;
    - budget pre-check: eligible-features × Σ repeats × 2 computed BEFORE
      snapshot expansion (and MAX_FEATURES enforced in the pure API too);
    - all exclusion/conflict lists STABLY SORTED by identity — reversed
      inputs produce byte-identical plans (digest included);
    - excluded/purged objects keep reviewable IDs + reasons;
    - the final digest is computed over the FINAL serialized plan content
      (including ``input_digests`` when provided — the CLI passes them in
      BEFORE hashing so the persisted file verifies against its own digest).
    """
    if len(features) > MAX_FEATURES:
        raise PlanValidationError(
            [f"特征条目 {len(features)} 超过 {MAX_FEATURES} 上限（纯 API 入口）"])
    features = [validate_feature(f, i) for i, f in enumerate(features)]
    validated_splits = validate_splits(splits, embargo)
    validated_trials = validate_arms(trials)

    # —— 全输入唯一身份（在任何快照展开之前）——
    seen_ids = set()
    for f in features:
        fid = f["feature_id"]
        if fid in seen_ids:
            raise PlanValidationError(
                [f"重复 feature_id {fid!r}——全输入身份必须唯一（即使都不入切分）"])
        seen_ids.add(fid)

    split_of: Dict[str, str] = {}
    unassigned: List[Dict[str, str]] = []
    for f in sorted(features, key=lambda x: x["feature_id"]):
        name = assign_split(f, validated_splits)
        if name is None:
            unassigned.append({"feature_id": f["feature_id"],
                               "ticker": f["ticker"],
                               "instrument_type": f["instrument_type"],
                               "reason": "prediction_at_outside_splits"})
        else:
            split_of[f["feature_id"]] = name

    kept, purged = purge_overlaps(features, split_of, validated_splits)
    assigned_kept = sorted((f for f in kept if f["feature_id"] in split_of),
                           key=lambda x: x["feature_id"])

    # —— 预算预检：合格特征 × Σ repeats × 2（展开前）——
    total_repeats = sum(t["repeat_count"] for t in validated_trials.values())
    projected_arm_runs = len(assigned_kept) * total_repeats * 2
    if projected_arm_runs > MAX_PLANNED_ARM_RUNS:
        raise PlanValidationError(
            [f"预计 arm-run 数 {projected_arm_runs}（{len(assigned_kept)} 合格特征 × "
             f"{total_repeats} 重复 × 2 臂）超过 {MAX_PLANNED_ARM_RUNS} 上限"
             "——在快照展开前拒绝"])

    entries: List[Dict[str, Any]] = []
    for pair_id, trial in sorted(validated_trials.items()):
        for f in assigned_kept:
            snap = feature_snapshot(f)
            snap_digest = _digest(snap)
            for repeat in range(1, trial["repeat_count"] + 1):
                entries.append({
                    "pair_id": pair_id,
                    "feature_id": f["feature_id"],
                    "repeat_index": repeat,
                    "split": split_of[f["feature_id"]],
                    "feature_snapshot": snap,
                    "feature_snapshot_digest": snap_digest,
                    "baseline_config_digest": trial["baseline_config_digest"],
                    "candidate_config_digest": trial["candidate_config_digest"],
                })

    # —— 排除/冲突清单稳定排序 ——
    unassigned.sort(key=lambda u: u["feature_id"])
    purged.sort(key=lambda p: (p["removed_feature_id"],
                               p["conflicts_with_feature_id"]))
    entries.sort(key=lambda e: (e["pair_id"], e["feature_id"], e["repeat_index"]))

    plan: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "stage": "prepare",
        "plan_entries": entries,
        "planned_arm_runs": len(entries) * 2,
        "splits": validated_splits["splits"],
        "split_order": validated_splits["order"],
        "embargo_days": validated_splits["embargo_days"],
        "excluded_unassigned": unassigned,
        "purged_overlaps": purged,
        "trials": {pid: {
            "pair_id": t["pair_id"], "repeat_count": t["repeat_count"],
            "baseline_config": t["baseline_config"],
            "baseline_config_digest": t["baseline_config_digest"],
            "candidate_config": t["candidate_config"],
            "candidate_config_digest": t["candidate_config_digest"],
        } for pid, t in sorted(validated_trials.items())},
    }
    if input_digests is not None:
        plan["input_digests"] = {k: input_digests[k] for k in sorted(input_digests)}
    plan["plan_digest"] = _digest(
        {k: v for k, v in plan.items() if k != "plan_digest"})
    return plan


# ---------------------------------------------------------------------------
# CLI (stage A)
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    # Two-stage dispatch (contract #1): prepare | score
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "score":
        return score_main(argv[1:])
    if argv and argv[0] == "prepare":
        argv = argv[1:]
    parser = argparse.ArgumentParser(
        prog="python -m tradingagents.evaluation.paired_eval",
        description="F3 stage A (prepare): build an immutable evaluation plan. "
                    "No labels, no models, no network.")
    parser.add_argument("--features", required=True)
    parser.add_argument("--trials", required=True)
    parser.add_argument("--splits", required=True)
    parser.add_argument("--embargo-days", type=int, default=0)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    def _fail(msg: str) -> int:
        print(msg, file=sys.stderr)
        return 2

    import os
    payloads = {}
    for label, path in (("features", args.features), ("trials", args.trials),
                        ("splits", args.splits)):
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            return _fail(f"{label} 文件读取失败: {exc}")
        if len(raw) > MAX_FILE_BYTES:
            return _fail(f"{label} 文件超过 {MAX_FILE_BYTES} 字节上限")
        try:
            payloads[label] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _fail(f"{label} 文件不是 UTF-8: {exc}")
    input_digests = {
        label: "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        for label, text in payloads.items()}
    try:
        features = load_features(payloads["features"])
        trials = json.loads(payloads["trials"])
        splits = json.loads(payloads["splits"])
        plan = build_plan(features, trials, splits, args.embargo_days,
                          input_digests=input_digests)
    except (PlanValidationError, json.JSONDecodeError) as exc:
        return _fail(f"prepare 拒绝: {exc}")
    os.makedirs(args.output_dir, exist_ok=True)
    out = os.path.join(args.output_dir, "eval_plan.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"plan_entries={len(plan['plan_entries'])} "
          f"planned_arm_runs={plan['planned_arm_runs']} "
          f"purged={len(plan['purged_overlaps'])} unassigned={len(plan['excluded_unassigned'])} "
          f"-> {out}")
    return 0





# ===========================================================================
# Stage B (score): frozen-plan validation, prediction/label identity,
# metrics with fixed denominators, all-repeat population summaries,
# Decimal currency buckets, report rendering + CLI.
# Codex F3 contract rules 5-9. F3a functions above stay stable.
# ===========================================================================

SCORE_SCHEMA_VERSION = 1
MAX_PREDICTION_CLAIMS = 100
MAX_PREDICTIONS_PER_ARM = MAX_PLANNED_ARM_RUNS

_STATUS_VALUES = frozenset({"completed", "limited", "refused", "failed"})
_LABEL_KINDS = frozenset({"fact_support", "numeric_verification",
                          "temporal_audit", "direction", "binary_event"})


class ScoreValidationError(ValueError):
    """Score-stage rejection. CLI exit 2."""


def _load_plan(plan: Any) -> Dict[str, Any]:
    """Frozen-plan validation BEFORE any scoring (rule 5)."""
    if not isinstance(plan, dict):
        raise ScoreValidationError(["plan 必须是对象"])
    errors: List[str] = []
    _check_json_legal(plan, "plan", errors)
    if errors:
        raise ScoreValidationError(errors)
    pv = plan.get("schema_version")
    if isinstance(pv, bool) or not isinstance(pv, int) or pv != SCHEMA_VERSION \
            or plan.get("stage") != "prepare":
        raise ScoreValidationError(
            [f"plan 非法（schema={plan.get('schema_version')!r}, "
             f"stage={plan.get('stage')!r}）——schema_version 必须已知整数非 bool"])
    entries = plan.get("plan_entries")
    if not isinstance(entries, list):
        raise ScoreValidationError(["plan_entries 缺失/损坏"])
    claimed = plan.get("plan_digest")
    body = {k: v for k, v in plan.items() if k != "plan_digest"}
    recomputed = _digest(body)  # 完整序列化内容（含 input_digests）
    if claimed != recomputed:
        raise ScoreValidationError(
            [f"plan_digest 失配（声称 {claimed!r}，复算 {recomputed!r}）——"
             "冻结计划被篡改或非本模块产物（完整文件摘要，含输入摘要字段）"])
    # 复验试验配置（唯一 bool 差异等——prepare 校验在 score 再执行一次）
    try:
        revalidated = validate_arms({pid: {
            "repeat_count": t["repeat_count"],
            "arms": {
                "baseline": {"normalized_config": t["baseline_config"],
                             "config_digest": t.get("baseline_config_digest")},
                "candidate": {"normalized_config": t["candidate_config"],
                              "config_digest": t.get("candidate_config_digest")},
            }} for pid, t in (plan.get("trials") or {}).items()})
    except PlanValidationError as exc:
        raise ScoreValidationError([f"计划试验配置复验失败: {exc}"]) from exc
    # —— 逐 entry 复验（Codex F3b R1 #5）：摘要自洽 ≠ schema 正确 ——
    entries = plan.get("plan_entries") or []
    seen = set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise ScoreValidationError([f"plan_entries[{i}]: 必须是对象"])
        ident = (e.get("pair_id"), e.get("feature_id"), e.get("repeat_index"))
        if ident in seen:
            raise ScoreValidationError([f"计划 entry 重复身份 {ident}"])
        seen.add(ident)
        snap = e.get("feature_snapshot")
        if not isinstance(snap, dict) or snap.get("feature_id") != ident[1]:
            raise ScoreValidationError(
                [f"plan_entries[{i}] 快照 feature_id 与 entry 身份不符"
                 f"（snapshot={snap.get('feature_id') if isinstance(snap, dict) else None!r}"
                 f" vs entry={ident[1]!r}）"])
        # 用 prepare 的特征 schema 复验快照（含嵌套白名单/时间原语）
        try:
            validate_feature(snap, i)
        except PlanValidationError as exc:
            raise ScoreValidationError(
                [f"plan_entries[{i}] 快照复验失败（摘要自洽≠schema 正确）: {exc}"]) from exc
        if e.get("feature_snapshot_digest") != _digest(snap):
            raise ScoreValidationError([f"plan_entries[{i}] 快照摘要失配"])
        t = revalidated.get(ident[0])
        if t is None:
            raise ScoreValidationError([f"plan_entries[{i}] pair 不在 trials: {ident[0]}"])
        if not isinstance(ident[2], int) or isinstance(ident[2], bool) \
                or not 1 <= ident[2] <= t["repeat_count"]:
            raise ScoreValidationError([f"plan_entries[{i}] repeat_index 越界"])
        if e.get("baseline_config_digest") != t["baseline_config_digest"] \
                or e.get("candidate_config_digest") != t["candidate_config_digest"]:
            raise ScoreValidationError(
                [f"plan_entries[{i}] 臂 config 摘要与 trials 复验值不符"])
    expected_pairs = sum(t["repeat_count"] * len({e["feature_id"] for e in entries
                                                  if e["pair_id"] == pid})
                         for pid, t in revalidated.items())
    if len(entries) != expected_pairs:
        raise ScoreValidationError(
            [f"plan_entries 数 {len(entries)} 与 trials×features 期望 "
             f"{expected_pairs} 不符"])
    if plan.get("planned_arm_runs") != len(entries) * 2:
        raise ScoreValidationError(["planned_arm_runs 与 entries×2 不符"])
    return plan


def _plan_entry_index(plan: Dict[str, Any]) -> Dict[Tuple[str, str, int], Dict[str, Any]]:
    return {(e["pair_id"], e["feature_id"], e["repeat_index"]): e
            for e in plan["plan_entries"]}


def _validate_prediction(pred: Any, index: int) -> Dict[str, Any]:
    """Frozen prediction schema (rule 5)."""
    where = f"predictions[{index}]"
    if not isinstance(pred, dict):
        raise ScoreValidationError([f"{where}: 必须是对象"])
    errors: List[str] = []
    _check_json_legal(pred, where, errors)
    if errors:
        raise ScoreValidationError(errors)
    allowed = {"plan_digest", "pair_id", "feature_id", "arm", "repeat_index",
               "feature_snapshot_digest", "config_digest", "generated_at",
               "status", "claims", "numeric_references", "disagreements",
               "direction", "usage", "costs"}
    extra = set(pred) - allowed
    if extra:
        raise ScoreValidationError([f"{where}: 非白名单键 {sorted(extra)}"])
    if pred.get("status") not in _STATUS_VALUES:
        raise ScoreValidationError([f"{where}.status: 必须 completed/limited/refused/failed"])
    for key in ("plan_digest", "pair_id", "feature_id", "arm"):
        if not isinstance(pred.get(key), str) or not pred[key].strip():
            raise ScoreValidationError([f"{where}.{key}: 必须是非空字符串"])
    if pred.get("arm") not in ("baseline", "candidate"):
        raise ScoreValidationError([f"{where}.arm: 必须 baseline/candidate"])
    ri = pred.get("repeat_index")
    if isinstance(ri, bool) or not isinstance(ri, int) or ri < 1:
        raise ScoreValidationError([f"{where}.repeat_index: 必须是正整数（非 bool）"])
    gen = _parse_time_strict(pred.get("generated_at"))
    if gen is None:
        raise ScoreValidationError([f"{where}.generated_at: 非法时间"])
    claims = pred.get("claims")
    if not isinstance(claims, list) or len(claims) > MAX_PREDICTION_CLAIMS:
        raise ScoreValidationError(
            [f"{where}.claims: 必须是列表且 ≤{MAX_PREDICTION_CLAIMS}"])
    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            raise ScoreValidationError([f"{where}.claims[{i}]: 必须是对象"])
        text = c.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ScoreValidationError([f"{where}.claims[{i}].text: 非空字符串"])
        cid = c.get("claim_id")
        expect = "claim-" + _digest({"text": text})[:16]
        if cid != expect:
            raise ScoreValidationError(
                [f"{where}.claims[{i}].claim_id: 必须与文本摘要绑定"
                 f"（期望 {expect!r}，得到 {cid!r}）——不靠数组下标"])
    if pred.get("direction") is not None and pred.get("direction") not in (
            "long", "flat", "short"):
        raise ScoreValidationError([f"{where}.direction: 必须 long/flat/short 或 null"])
    dis = pred.get("disagreements") or []
    if not isinstance(dis, list):
        raise ScoreValidationError([f"{where}.disagreements: 必须是列表"])
    for i, d in enumerate(dis):
        if not isinstance(d, dict):
            raise ScoreValidationError([f"{where}.disagreements[{i}]: 必须是对象"])
        st = d.get("status")
        if st not in ("unresolved", "inconclusive", "resolved"):
            raise ScoreValidationError(
                [f"{where}.disagreements[{i}].status: 未知 {st!r}"
                 "（不允许默认 resolved）"])
    usage = pred.get("usage") or {}
    if not isinstance(usage, dict):
        raise ScoreValidationError([f"{where}.usage: 必须是对象"])
    for key in ("llm_calls", "http_requests", "tool_invocations", "tokens"):
        if key in usage:
            v = usage[key]
            if v != "unknown" and (isinstance(v, bool)
                                   or not isinstance(v, int) or v < 0):
                raise ScoreValidationError(
                    [f"{where}.usage.{key}: 非负整数或 'unknown'"])
    costs = pred.get("costs") or {}
    if not isinstance(costs, dict):
        raise ScoreValidationError([f"{where}.costs: 必须是对象"])
    for cur, amount in costs.items():
        if not isinstance(cur, str) or len(cur) == 0:
            raise ScoreValidationError([f"{where}.costs: 币种键必须是非空字符串"])
        if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
            raise ScoreValidationError(
                [f"{where}.costs[{cur!r}]: 必须 Decimal 兼容值（str/int）"])
        try:
            from decimal import Decimal as _D
            dec = _D(str(amount))
        except Exception:
            raise ScoreValidationError([f"{where}.costs[{cur!r}]: 非法金额"])
        if not dec.is_finite():
            raise ScoreValidationError(
                [f"{where}.costs[{cur!r}]: 非有限金额（NaN/Infinity 拒绝）"])
        if dec < 0:
            raise ScoreValidationError([f"{where}.costs[{cur!r}]: 负金额拒绝"])
    return json.loads(json.dumps(pred, ensure_ascii=False))


def _validate_label(label: Any, index: int) -> Dict[str, Any]:
    """Independent label schema (rule 6)."""
    where = f"labels[{index}]"
    if not isinstance(label, dict):
        raise ScoreValidationError([f"{where}: 必须是对象"])
    errors: List[str] = []
    _check_json_legal(label, where, errors)
    if errors:
        raise ScoreValidationError(errors)
    allowed = {"label_id", "pair_id", "feature_id", "arm", "repeat_index",
               "label_kind", "target_claim_id", "target_claim_digest",
               "value", "labeled_by", "label_available_at",
               "outcome_maturity", "outcome_observed_at",
               "outcome_publication_time"}
    extra = set(label) - allowed
    if extra:
        raise ScoreValidationError([f"{where}: 非白名单键 {sorted(extra)}"])
    for key in ("label_id", "pair_id", "feature_id", "arm", "labeled_by"):
        if not isinstance(label.get(key), str) or not label[key].strip():
            raise ScoreValidationError([f"{where}.{key}: 必须是非空字符串"])
    if label.get("arm") not in ("baseline", "candidate"):
        raise ScoreValidationError([f"{where}.arm: 必须 baseline/candidate"])
    ri = label.get("repeat_index")
    if isinstance(ri, bool) or not isinstance(ri, int) or ri < 1:
        raise ScoreValidationError([f"{where}.repeat_index: 正整数（非 bool）"])
    if label.get("label_kind") not in _LABEL_KINDS:
        raise ScoreValidationError(
            [f"{where}.label_kind: 必须 {'/'.join(sorted(_LABEL_KINDS))}"])
    avail = _parse_time_strict(label.get("label_available_at"))
    if avail is None:
        raise ScoreValidationError([f"{where}.label_available_at: 非法时间"])
    for key in ("outcome_maturity", "outcome_observed_at",
                "outcome_publication_time"):
        if label.get(key) is not None and _parse_time_strict(label[key]) is None:
            raise ScoreValidationError([f"{where}.{key}: 非法时间"])
    if label.get("value") is None:
        raise ScoreValidationError([f"{where}.value: 不得为 null"])
    return json.loads(json.dumps(label, ensure_ascii=False))


def _claim_digest(text: str) -> str:
    return _digest({"text": text})


def load_predictions(source: Any) -> List[Dict[str, Any]]:
    if isinstance(source, str):
        if len(source.encode("utf-8")) > MAX_FILE_BYTES:
            raise ScoreValidationError([f"预测文件超过 {MAX_FILE_BYTES} 字节"])
        raws = []
        for i, line in enumerate(source.splitlines()):
            if line.strip():
                try:
                    raws.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ScoreValidationError(
                        [f"predictions line {i+1}: JSON 失败（{exc}）"]) from exc
    elif isinstance(source, list):
        raws = source
    else:
        raise ScoreValidationError(["predictions 必须是 JSONL 或列表"])
    out = []
    for i, raw in enumerate(raws):
        out.append(_validate_prediction(raw, i))
    return out


def load_labels(source: Any) -> List[Dict[str, Any]]:
    if isinstance(source, str):
        if len(source.encode("utf-8")) > MAX_FILE_BYTES:
            raise ScoreValidationError([f"标签文件超过 {MAX_FILE_BYTES} 字节"])
        raws = []
        for i, line in enumerate(source.splitlines()):
            if line.strip():
                try:
                    raws.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ScoreValidationError(
                        [f"labels line {i+1}: JSON 失败（{exc}）"]) from exc
    elif isinstance(source, list):
        raws = source
    else:
        raise ScoreValidationError(["labels 必须是 JSONL 或列表"])
    out = []
    for i, raw in enumerate(raws):
        out.append(_validate_label(raw, i))
    return out


_KIND_REQUIRES_TARGET = {
    "fact_support": "claim",
    "numeric_verification": "reference",
    "temporal_audit": None,   # run-scope: 无 per-claim 目标要求
    "direction": None,        # run-scope
    "binary_event": None,     # run-scope
}
_FACT_VALUES = frozenset({"supported", "unsupported"})
_NUMERIC_VALUES = frozenset({"correct", "incorrect"})
_TEMPORAL_VALUES = frozenset({"violation", "clean"})
_DIRECTION_VALUES = frozenset({"long", "flat", "short"})


def _match_labels_to_predictions(plan, predictions, labels, as_of_dt):
    """Exact identity matching (rule 6, Codex F3b R1 #2 hardening).

    Per-kind target binding: fact_support MUST bind a real claim (id+digest);
    numeric_verification MUST bind a real numeric reference (id+digest)——
    pointing at a plain claim does not count as a verified reference. Same
    (prediction, kind, target) with conflicting values rejects (not two
    observations); unknown value enumerations reject (never silent negatives).
    """
    pred_by_id = {(p["pair_id"], p["feature_id"], p["arm"], p["repeat_index"]): p
                  for p in predictions}
    matched: List[Dict[str, Any]] = []
    invalid: List[Dict[str, str]] = []
    seen_label_ids = set()
    seen_targets: Dict[Tuple, str] = {}
    for lab in labels:
        lid = lab["label_id"]
        if lid in seen_label_ids:
            raise ScoreValidationError([f"重复 label_id {lid!r}——拒绝"])
        seen_label_ids.add(lid)
        key = (lab["pair_id"], lab["feature_id"], lab["arm"], lab["repeat_index"])
        pred = pred_by_id.get(key)
        reason = None
        target_key = None
        if pred is None:
            reason = "no_matching_prediction"
        else:
            kind = lab["label_kind"]
            claims = pred.get("claims") or []
            refs = pred.get("numeric_references") or []
            if kind == "fact_support":
                tc, dig = lab.get("target_claim_id"), lab.get("target_claim_digest")
                if not tc or not dig:
                    reason = "missing_target_claim_identity"
                else:
                    hit = next((c for c in claims
                                if c["claim_id"] == tc
                                and _claim_digest(c["text"]) == dig), None)
                    if hit is None:
                        reason = "claim_digest_mismatch"
                    else:
                        target_key = (key, kind, tc)
            elif kind == "numeric_verification":
                tc, dig = lab.get("target_claim_id"), lab.get("target_claim_digest")
                if not tc or not dig:
                    reason = "missing_target_reference_identity"
                else:
                    hit = next((r for r in refs
                                if isinstance(r, dict) and r.get("reference_id") == tc
                                and _digest({"content": r.get("content", "")}) == dig),
                               None)
                    if hit is None:
                        reason = "reference_not_in_prediction"
                    else:
                        target_key = (key, kind, tc)
            elif kind == "direction" and (lab.get("target_claim_id")
                                          or lab.get("target_claim_digest")):
                # Codex F3b R4: 方向标签若提供目标则 ID+digest 必须成对绑定
                # 同一个实际 claim（非"某个 claim 有该 digest"）。只提供
                # 一半或目标不匹配 → 隔离。无目标 = run scope（合法）。
                tc = lab.get("target_claim_id")
                dig = lab.get("target_claim_digest")
                if not tc or not dig:
                    reason = "direction_target_partial"
                else:
                    bound = next((c for c in claims
                                  if c["claim_id"] == tc
                                  and _claim_digest(c["text"]) == dig), None)
                    if bound is None:
                        reason = "direction_target_mismatch"
            elif pred["status"] in ("failed", "missing"):
                reason = "labels_cannot_fabricate_output"
        # 值枚举校验（未知值拒绝，不静默当负例）
        if reason is None:
            kind = lab["label_kind"]
            allowed = {"fact_support": _FACT_VALUES,
                       "numeric_verification": _NUMERIC_VALUES,
                       "temporal_audit": _TEMPORAL_VALUES,
                       "direction": _DIRECTION_VALUES}.get(kind)
            if allowed and lab["value"] not in allowed:
                raise ScoreValidationError(
                    [f"标签 {lid!r} {kind} 值非法: {lab['value']!r}"
                     f"（允许 {sorted(allowed)}）"])
        # 同目标同 kind 冲突拒绝；完全相同标签（重复）也拒绝刷分母
        if reason is None and target_key is not None:
            prior = seen_targets.get(target_key)
            if prior is not None:
                if prior != lab["value"]:
                    raise ScoreValidationError(
                        [f"同目标冲突标签（{target_key}）：{prior!r} vs {lab['value']!r}"
                         "——不是两条独立观测"])
                raise ScoreValidationError(
                    [f"同目标重复标签（{target_key}, value={prior!r}）——"
                     "不能刷分母"])
            seen_targets[target_key] = lab["value"]
        if reason:
            invalid.append({"label_id": lid, "reason": reason})
            continue
        # 可知性（rule 6）
        avail = _parse_time_strict(lab["label_available_at"])[0]
        if avail > as_of_dt:
            invalid.append({"label_id": lid, "reason": "label_not_yet_available"})
            continue
        if lab["label_kind"] in ("direction", "binary_event"):
            ok = True
            for gkey in ("outcome_maturity", "outcome_observed_at",
                         "outcome_publication_time"):
                v = lab.get(gkey)
                if v is None:
                    invalid.append({"label_id": lid,
                                    "reason": f"unverifiable_{gkey}"})
                    ok = False
                    break
                gdt = _parse_time_strict(v)[0]
                if gdt > as_of_dt:
                    state = "pending_maturity" if gkey == "outcome_maturity" else \
                        f"future_{gkey}"
                    invalid.append({"label_id": lid, "reason": state})
                    ok = False
                    break
            if not ok:
                continue
        matched.append({"label": lab, "prediction": pred})
    return matched, invalid


def _population_stats(values: List[float]) -> Dict[str, Any]:
    """Mean + population stddev (÷N). No CI, no best-repeat selection."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "population_stddev": None}
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return {"n": n, "mean": mean,
            "population_stddev": var ** 0.5}


def _metric(num: int, den: int) -> Dict[str, Any]:
    return {"numerator": num, "denominator": den,
            "value": (num / den) if den > 0 else None}


# Object status codes (single classification, assigned once)
S_SCORED = "scored"
S_UNLABELED = "unlabeled"
S_PENDING = "pending"
S_UNVERIFIABLE = "unverifiable"
S_UNASSESSABLE = "unassessable"

ALL_REPEAT_METRICS = ("fact_support", "numeric_error", "temporal_violation",
                      "e_unresolved", "e_inconclusive", "direction_hit")


def _classify_objects(plan, entries, by_identity, matched, invalid_label_pairs,
                      arm_enabled):
    """Canonical assessed-object classification (Codex F3b R3).

    Builds the FULL universe of scorable objects per (pair, arm, repeat,
    feature, metric, object_id) from predictions first, then binds labels
    and assigns exactly ONE status per object:

    - scored: valid label bound, prediction scorable (numerator candidate)
    - unlabeled: prediction scorable but no label reached it
    - pending: label exists but outcome gates are future (e.g. maturity 2099)
    - unverifiable: label exists but outcome gates missing
    - unassessable: prediction has no scorable content (e.g. direction=None)

    Overall metrics, per-repeat/per-pair summaries, and Markdown all consume
    THIS classified data. Denominator = count(scored objects) directly.
    E metrics only produce objects when the arm's config has
    evidence_debate_enabled=True (disabled arm with disagreements → reject).
    """
    # label lookup: (arm, pair, feature, repeat) → matched labels
    matched_by = {}
    for m in matched:
        mp = m["prediction"]
        matched_by.setdefault(
            (mp["arm"], mp["pair_id"], mp["feature_id"], mp["repeat_index"]),
            []).append(m["label"])
    # invalid labels by identity AND kind
    invalid_by = {}
    for lab, reason in invalid_label_pairs:
        if lab is None:
            continue
        invalid_by.setdefault(
            (lab["arm"], lab["pair_id"], lab["feature_id"], lab["repeat_index"]),
            []).append({"label_id": lab["label_id"], "reason": reason,
                       "label_kind": lab.get("label_kind", "")})

    objects = {}  # (pair, arm, repeat, feature, metric, object_id) → record
    for plan_key, e in entries.items():
        pair_id, feature_id, repeat = plan_key
        for arm in ("baseline", "candidate"):
            pred = by_identity.get((pair_id, feature_id, arm, repeat))
            key4 = (arm, pair_id, feature_id, repeat)
            mlabs = matched_by.get(key4, [])
            ilabs = invalid_by.get(key4, [])
            scorable = (pred is not None
                        and pred["status"] in ("completed", "limited"))
            e_enabled = arm_enabled.get((pair_id, arm), False)

            def _add(metric, obj_id, status, hit=None, gate=""):
                ok = (pair_id, arm, repeat, feature_id, metric, obj_id)
                objects[ok] = {"status": status, "hit": hit, "gate": gate}

            # ---- fact_support: one object per claim
            if scorable:
                for c in (pred.get("claims") or []):
                    lab = next((lb for lb in mlabs
                                if lb["label_kind"] == "fact_support"
                                and lb.get("target_claim_id") == c["claim_id"]), None)
                    if lab is not None:
                        _add("fact_support", c["claim_id"], S_SCORED,
                             hit=lab["value"] == "supported")
                    else:
                        inv = next((iv for iv in ilabs
                                    if iv["label_kind"] == "fact_support"
                                    and iv["reason"].startswith(("pending", "unverifiable"))), None)
                        if inv:
                            _add("fact_support", c["claim_id"],
                                 S_PENDING if inv["reason"].startswith("pending")
                                 else S_UNVERIFIABLE)
                        else:
                            _add("fact_support", c["claim_id"], S_UNLABELED)

            # ---- numeric_error: one object per reference
            if scorable:
                for r in (pred.get("numeric_references") or []):
                    if not isinstance(r, dict):
                        continue
                    rid = r.get("reference_id", "")
                    lab = next((lb for lb in mlabs
                                if lb["label_kind"] == "numeric_verification"
                                and lb.get("target_claim_id") == rid), None)
                    if lab is not None:
                        _add("numeric_error", rid, S_SCORED,
                             hit=lab["value"] == "incorrect")
                    else:
                        _add("numeric_error", rid, S_UNLABELED)

            # ---- temporal_violation: object = run (object_id = "run")
            if scorable:
                tlabs = [lb for lb in mlabs
                         if lb["label_kind"] == "temporal_audit"]
                if tlabs:
                    _add("temporal_violation", "run", S_SCORED,
                         hit=any(lb["value"] == "violation" for lb in tlabs))
                else:
                    _add("temporal_violation", "run", S_UNLABELED)

            # ---- direction_hit: object = run with non-null direction
            if scorable:
                dlabs = [lb for lb in mlabs
                         if lb["label_kind"] == "direction"]
                dinv = [iv for iv in ilabs
                        if iv["label_kind"] == "direction"]
                if pred.get("direction") is None:
                    if dlabs or dinv:
                        _add("direction_hit", "run", S_UNASSESSABLE)
                    # no labels + no direction → no object (not in universe)
                elif dlabs:
                    # 有标签但可能全部被隔离（pending/unverifiable/坏目标）
                    valid = [lb for lb in dlabs
                             if not any(iv["label_id"] == lb["label_id"]
                                        for iv in dinv)]
                    if valid:
                        _add("direction_hit", "run", S_SCORED,
                             hit=any(pred["direction"] == lb["value"]
                                     for lb in valid))
                    elif dinv:
                        # 所有标签都被隔离：按隔离原因分类（pending/unverifiable/
                        # 其他原因如 target digest 不符 → 不入 scored 分母）
                        reason = dinv[0]["reason"]
                        if reason.startswith("pending"):
                            _add("direction_hit", "run", S_PENDING)
                        elif reason.startswith("unverifiable"):
                            _add("direction_hit", "run", S_UNVERIFIABLE)
                        else:
                            _add("direction_hit", "run", S_UNASSESSABLE)
                elif dinv:
                    # 标签全部隔离（无 matched），与上同规则
                    reason = dinv[0]["reason"]
                    if reason.startswith("pending"):
                        _add("direction_hit", "run", S_PENDING)
                    elif reason.startswith("unverifiable"):
                        _add("direction_hit", "run", S_UNVERIFIABLE)
                    else:
                        _add("direction_hit", "run", S_UNASSESSABLE)
                else:
                    _add("direction_hit", "run", S_UNLABELED)

            # ---- E disagreements: only when arm's E is enabled.
            # Codex F3b R4: every listed valid disagreement produces a SCORED
            # observation in BOTH E metrics — hit=(status matches that
            # metric's named status). Known other statuses are observed
            # FALSE, not unknown. Denominator = all listed disagreements.
            if scorable and e_enabled:
                for i, d in enumerate(pred.get("disagreements") or []):
                    if not isinstance(d, dict):
                        continue
                    did = d.get("disagreement_id", f"dis-{i}")
                    st = d.get("status", "")
                    _add("e_unresolved", did, S_SCORED,
                         hit=(st == "unresolved"))
                    _add("e_inconclusive", did, S_SCORED,
                         hit=(st == "inconclusive"))
            elif scorable and not e_enabled and (pred.get("disagreements") or []):
                raise ScoreValidationError(
                    [f"E 禁用臂（{pair_id}/{arm} config evidence_debate_enabled="
                     f"False）携带 disagreements——矛盾输出拒绝: {key4}"])

    return objects


def score_plan(plan: Dict[str, Any], predictions: List[Dict[str, Any]],
               labels: List[Dict[str, Any]], as_of: str) -> Dict[str, Any]:
    """Score frozen plan (rules 5-8). Public API REVALIDATES predictions and
    labels itself (Codex F3b R1 #5)——不依赖调用方先 load；坏输入一律
    ScoreValidationError（无裸 KeyError）。"""
    as_of_dt = _parse_time_strict(as_of)
    if as_of_dt is None:
        raise ScoreValidationError([f"as_of 非法: {as_of!r}"])
    plan = _load_plan(plan)
    predictions = [_validate_prediction(p, i) for i, p in enumerate(predictions)]
    labels = [_validate_label(lab, i) for i, lab in enumerate(labels)]
    entries = _plan_entry_index(plan)

    # Predictions: exact plan identity; conflict duplicate → reject; missing →
    # planned-missing (denominator never shrinks).
    by_identity: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
    extra: List[Dict[str, str]] = []
    for p in predictions:
        key = (p["pair_id"], p["feature_id"], p["arm"], p["repeat_index"])
        plan_key = (key[0], key[1], key[3])
        if plan_key not in entries:
            extra.append({"identity": list(key), "reason": "outside_plan"})
            continue
        if key in by_identity:
            raise ScoreValidationError(
                [f"冲突的重复预测身份 {key}——不得选最后/最好"])
        e = entries[plan_key]
        if p.get("plan_digest") != plan["plan_digest"]:
            raise ScoreValidationError([f"预测 plan_digest 与计划不符: {key}"])
        expected_cfg = e[f"{key[2]}_config_digest"]
        if p.get("config_digest") != expected_cfg:
            raise ScoreValidationError(
                [f"预测 config_digest 与其声明的 {key[2]} 臂不符: {key}"
                 f"（期望 {expected_cfg!r}，得到 {p.get('config_digest')!r}）"])
        if p.get("feature_snapshot_digest") != e["feature_snapshot_digest"]:
            raise ScoreValidationError([f"预测 feature_snapshot_digest 不符: {key}"])
        gen = _parse_time_strict(p["generated_at"])[0]
        snap = e["feature_snapshot"]
        # 统一时点约定（Codex F3b R1 #8）：date 精度一律按 F1 上海日终锚点
        # （不做 00:00 重解释）；generated_at 不得早于 prediction_at、
        # feature_available_at、decided_at 三者的解析后锚点。
        for anchor_name in ("prediction_at", "feature_available_at"):
            anchor_raw = snap.get(anchor_name)
            if anchor_raw is None:
                continue
            anchor = _f1_parse_dt(anchor_raw)
            if anchor is not None and gen < anchor[0]:
                raise ScoreValidationError(
                    [f"generated_at 早于 {anchor_name}（{gen.isoformat()} < "
                     f"{anchor[0].isoformat()}）: {key}"])
        dec_raw = (snap.get("decision") or {}).get("decided_at")
        dec = _f1_parse_dt(dec_raw) if dec_raw else None
        if dec is not None and gen < dec[0]:
            raise ScoreValidationError(
                [f"generated_at 早于 decided_at: {key}"])
        by_identity[key] = p
    if extra:
        raise ScoreValidationError(
            [f"计划外预测拒绝: {extra[:3]}"])

    matched, invalid_labels = _match_labels_to_predictions(
        plan, list(by_identity.values()), labels, as_of_dt[0])
    # 覆盖统计需要 invalid 标签的完整归属：由 id 反查原始标签。
    _lab_by_id = {lab["label_id"]: lab for lab in labels}
    invalid_label_pairs = [(_lab_by_id.get(e["label_id"]), e["reason"])
                           for e in invalid_labels if e["label_id"] in _lab_by_id]

    trials_of_plan = {pid: t.get("repeat_count", 0)
                      for pid, t in (plan.get("trials") or {}).items()}

    # ---- 每臂 E 开关（从计划 trials 的 normalized_config 读取）----
    arm_enabled: Dict[Tuple[str, str], bool] = {}
    for pid, t in (plan.get("trials") or {}).items():
        base_on = (t.get("baseline_config") or {}).get(SWITCH_KEY) is True
        cand_on = (t.get("candidate_config") or {}).get(SWITCH_KEY) is True
        arm_enabled[(pid, "baseline")] = base_on
        arm_enabled[(pid, "candidate")] = cand_on

    # ---- 统一对象分类（唯一分类来源）----
    objects = _classify_objects(plan, entries, by_identity, matched,
                               invalid_label_pairs, arm_enabled)

    # ---- Per-arm overall metrics FROM classified objects ----
    from decimal import Decimal
    arm_results: Dict[str, Any] = {}
    arm_usage: Dict[str, Any] = {}
    arm_costs: Dict[str, Any] = {}

    for arm in ("baseline", "candidate"):
        # counts from planned denominator (unchanged logic)
        counts = {"planned": 0, "missing": 0, "failed": 0, "refused": 0,
                  "limited": 0, "completed": 0, "completed_state_mismatch": 0}
        arm_preds: Dict[Tuple[str, str, int], Optional[Dict[str, Any]]] = {}
        for (pair_id, feature_id, repeat), _e in sorted(entries.items()):
            counts["planned"] += 1
            pr = by_identity.get((pair_id, feature_id, arm, repeat))
            if pr is None:
                counts["missing"] += 1
                arm_preds[(pair_id, feature_id, repeat)] = None
            else:
                status = pr["status"]
                if status == "completed" and not (pr.get("claims")
                                                  or pr.get("direction") is not None
                                                  or pr.get("disagreements")):
                    counts["completed_state_mismatch"] += 1
                    status = "limited"
                counts[status] += 1
                arm_preds[(pair_id, feature_id, repeat)] = pr

        def _arm_metric(metric):
            scored = [o for (k, o) in objects.items()
                      if k[1] == arm and k[4] == metric and o["status"] == S_SCORED]
            num = sum(1 for o in scored if o["hit"])
            den = len(scored)
            all_objs = [o for (k, o) in objects.items()
                        if k[1] == arm and k[4] == metric]
            return {
                **_metric(num, den),
                "eligible": len(all_objs),
                "unlabeled": sum(1 for o in all_objs if o["status"] == S_UNLABELED),
                "unassessable": sum(1 for o in all_objs if o["status"] == S_UNASSESSABLE),
                "pending": sum(1 for o in all_objs if o["status"] == S_PENDING),
                "unverifiable": sum(1 for o in all_objs if o["status"] == S_UNVERIFIABLE),
            }

        e_applicable = any(arm_enabled.get((pid, arm))
                           for pid in (plan.get("trials") or {}))
        arm_results[arm] = {
            "counts": counts,
            "fact_support": _arm_metric("fact_support"),
            "numeric_error": _arm_metric("numeric_error"),
            "temporal_violation": _arm_metric("temporal_violation"),
            "e_unresolved": _arm_metric("e_unresolved"),
            "e_inconclusive": _arm_metric("e_inconclusive"),
            "direction_hit": _arm_metric("direction_hit"),
            "brier": {"status": "not_applicable",
                      "reason": "Brier 未实现——按契约 #7 明确不支持，"
                                "不声称 F3 完整包含校准"},
        }
        if not e_applicable:
            for ek in ("e_unresolved", "e_inconclusive"):
                arm_results[arm][ek] = {
                    **arm_results[arm][ek],
                    "applicable": False,
                    "note": "该臂全部配置 E 未启用——not_applicable，不伪装观测值 0",
                }

        # usage / costs per-arm (unchanged logic)
        u_totals = {"llm_calls": 0, "http_requests": 0,
                    "tool_invocations": 0, "tokens": 0}
        u_unknown = {"llm_calls": 0, "http_requests": 0,
                     "tool_invocations": 0, "tokens": 0}
        for k, pr in arm_preds.items():
            usage = (pr or {}).get("usage") or {}
            for ukey in u_totals:
                v = usage.get(ukey)
                if v is None or v == "unknown":
                    u_unknown[ukey] += 1
                else:
                    u_totals[ukey] += v
        arm_usage[arm] = {"totals": u_totals, "unknown_counts": u_unknown,
                          "planned": counts["planned"],
                          "missing": counts["missing"]}
        buckets: Dict[str, Dict[str, Any]] = {}
        known_any = False
        for k, pr in arm_preds.items():
            for cur, amount in ((pr or {}).get("costs") or {}).items():
                b = buckets.setdefault(cur, {"known_total": Decimal("0"),
                                             "entries": 0})
                b["known_total"] += Decimal(str(amount))
                b["entries"] += 1
                known_any = True
        cost_out_arm = {cur: {"known_total": str(b["known_total"]),
                               "entries": b["entries"],
                               "note": "仅已知条目之和；部分已知不冒充完整总成本"}
                        for cur, b in sorted(buckets.items())}
        if not known_any:
            cost_out_arm = {"note": "无已知成本条目"}
        arm_costs[arm] = cost_out_arm

    # All-repeat population summaries FROM the SAME classified objects
    # (Codex F3b R3: keyed by (pair, arm, metric, repeat); features aggregate
    # within each group; planned_n = 该 pair 计划重复数; raw rows preserved;
    # invariant: overall num/den == Σ raw_per_repeat num/den).
    repeat_metrics: Dict[str, Dict[str, Any]] = {}
    pair_ids = sorted({pid for (pid, _f, _r) in entries})
    for pair_id in pair_ids:
        planned_repeats = trials_of_plan.get(pair_id, 0)
        if planned_repeats == 0:
            continue
        for arm in ("baseline", "candidate"):
            for metric in ALL_REPEAT_METRICS:
                raw_rows = []
                values: List[float] = []
                for repeat in range(1, planned_repeats + 1):
                    group = [(k, o) for k, o in objects.items()
                             if k[0] == pair_id and k[1] == arm
                             and k[2] == repeat and k[4] == metric]
                    scored = [o for _k, o in group if o["status"] == S_SCORED]
                    num = sum(1 for o in scored if o["hit"])
                    den = len(scored)
                    value = (num / den) if den > 0 else None
                    raw_rows.append({
                        "repeat_index": repeat,
                        "numerator": num, "denominator": den, "value": value,
                        "feature_count": len({k[3] for k, _o in group}),
                    })
                    if value is not None:
                        values.append(value)
                stats = _population_stats(values)
                stats["effective_n"] = len(values)
                stats["planned_n"] = planned_repeats
                stats["raw_per_repeat"] = raw_rows
                repeat_metrics[f"{pair_id}:{arm}:{metric}"] = stats

    # 全臂 usage/costs 汇总（两臂分开报告保留在 arm_usage/arm_costs 内；
    # 此处总计仅为便利——复核按臂进行，Codex F3b R1 #4）
    usage_totals = {k: arm_usage["baseline"]["totals"][k]
                    + arm_usage["candidate"]["totals"][k]
                    for k in ("llm_calls", "http_requests",
                              "tool_invocations", "tokens")}
    usage_unknown = {k: arm_usage["baseline"]["unknown_counts"][k]
                     + arm_usage["candidate"]["unknown_counts"][k]
                     for k in ("llm_calls", "http_requests",
                               "tool_invocations", "tokens")}
    cost_out = {"per_arm": arm_costs}

    result = {
        "schema_version": SCORE_SCHEMA_VERSION,
        "stage": "score",
        "as_of": as_of,
        "plan_digest": plan["plan_digest"],
        "planned_arm_runs": plan["planned_arm_runs"],
        "arms": arm_results,
        "repeat_summaries": repeat_metrics,
        "invalid_labels": invalid_labels,
        "usage_totals": usage_totals,
        "usage_unknown_counts": usage_unknown,
        "costs_by_currency": cost_out,
        "arm_usage": arm_usage,
        "input_content_digests": {
            "predictions": _digest(predictions),
            "labels": _digest(labels),
        },
        "disclaimer": ("合成契约演示，不代表预测效果提高；准确度与覆盖率同报；"
                        "盈利不等于推理正确。声明 generated_at 与摘要只证明输入"
                        "自洽，不证明过去真的冻结或模型未偷看标签。"),
    }
    result["result_digest"] = _digest(
        {k: v for k, v in result.items() if k != "result_digest"})
    return result


def render_score_md(result: Dict[str, Any]) -> str:
    lines = ["**离线配对评估报告（F3 score）**：",
             f"- as_of={result['as_of']}；plan=`{result['plan_digest']}`；"
             f"planned_arm_runs={result['planned_arm_runs']}"]
    for arm, r in sorted(result.get("arms", {}).items()):
        c = r["counts"]
        lines.append(
            f"- **{arm}**: planned={c['planned']} missing={c['missing']} "
            f"failed={c['failed']} refused={c['refused']} limited={c['limited']} "
            f"completed={c['completed']}（completed=结构化结论产出，≠正确、≠非拒答）")
        for name in ("fact_support", "numeric_error", "temporal_violation",
                     "e_unresolved", "e_inconclusive", "direction_hit"):
            m = r[name]
            val = "null" if m["value"] is None else f"{m['value']:.4f}"
            lines.append(f"  - {name}: {m['numerator']}/{m['denominator']}"
                         f" = {val}（分母0为null；覆盖: eligible={m.get('eligible', 0)}"
                         f" unlabeled={m.get('unlabeled', 0)}"
                         f" unassessable={m.get('unassessable', 0)}"
                         + (f" pending={m['pending']}" if m.get('pending') else "")
                         + (f" unverifiable={m['unverifiable']}" if m.get('unverifiable') else "")
                         + "）")
        lines.append(f"  - Brier: {r['brier']['status']}（{r['brier']['reason']}）")
    rep = result.get("repeat_summaries") or {}
    if rep:
        lines.append("- 重复汇总（每 repeat 先在 repeat 内聚合 features；"
                     "总体标准差 ÷N，无置信区间，不挑最好一次；零效行保留）:")
        for key in sorted(rep):
            s2 = rep[key]
            eff = s2.get("effective_n", 0)
            plan_n = s2.get("planned_n", 0)
            if eff > 0:
                lines.append(f"  - {key}: mean={s2['mean']:.4f} "
                             f"stddev={s2['population_stddev']:.4f} "
                             f"effective_n={eff}/{plan_n}（计划重复数）")
            else:
                lines.append(f"  - {key}: 零有效重复（0/{plan_n}）——"
                             "全部分母为 0，值 null 不造 0")
    inv = result.get("invalid_labels") or []
    if inv:
        by_r: Dict[str, int] = {}
        for e in inv:
            by_r[e["reason"]] = by_r.get(e["reason"], 0) + 1
        lines.append("- 无效/隔离标签: " +
                     "，".join(f"{k}×{v}" for k, v in sorted(by_r.items())))
    usage = result.get("usage_totals") or {}
    unk = result.get("usage_unknown_counts") or {}
    lines.append(f"- usage: llm={usage.get('llm_calls')}（unknown×{unk.get('llm_calls', 0)}）"
                 f" http={usage.get('http_requests')}（unknown×{unk.get('http_requests', 0)}）"
                 f" tools={usage.get('tool_invocations')} tokens={usage.get('tokens')}"
                 "（四类不混算）")
    costs = result.get("costs_by_currency") or {}
    per_arm_costs = costs.get("per_arm") if isinstance(costs, dict) else None
    if isinstance(per_arm_costs, dict):
        lines.append("- 成本（每臂分开，按币种分桶，Decimal 精确；混合币种不相加）:")
        for arm in sorted(per_arm_costs):
            arm_c = per_arm_costs[arm]
            if "note" in arm_c and len(arm_c) == 1:
                lines.append(f"  - {arm}: {arm_c['note']}")
            else:
                for cur in sorted(arm_c):
                    if isinstance(arm_c[cur], dict) and "known_total" in arm_c[cur]:
                        lines.append(f"  - {arm} {cur}: {arm_c[cur]['known_total']}"
                                     f"（{arm_c[cur]['entries']} 条已知）")
    else:
        lines.append(f"- 成本: {costs.get('note', '无已知成本')}")
    lines.append(f"> {result.get('disclaimer', '')}")
    return "\n".join(lines)


def score_main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tradingagents.evaluation.paired_eval",
        description="F3 stage B (score): frozen plan + pre-recorded arm "
                    "predictions + independent labels → JSON+Markdown.")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--predictions", required=True,
                        help="JSONL with BOTH arms (arm field distinguishes)")
    parser.add_argument("--labels", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    def _fail(msg):
        print(msg, file=sys.stderr)
        return 2

    import os
    texts = {}
    for label, path in (("plan", args.plan), ("predictions", args.predictions),
                        ("labels", args.labels)):
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            return _fail(f"{label} 文件读取失败: {exc}")
        if len(raw) > MAX_FILE_BYTES:
            return _fail(f"{label} 超过 {MAX_FILE_BYTES} 字节")
        try:
            texts[label] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _fail(f"{label} 不是 UTF-8: {exc}")
    try:
        plan = json.loads(texts["plan"])
        predictions = load_predictions(texts["predictions"])
        labels = load_labels(texts["labels"])
        result = score_plan(plan, predictions, labels, args.as_of)
    except (ScoreValidationError, PlanValidationError,
            json.JSONDecodeError) as exc:
        return _fail(f"score 拒绝: {exc}")
    # 最终摘要覆盖全部持久字段（含输入摘要）——与 prepare 同规则：
    # 先补齐 input_digests 再重算 result_digest，文件自验通过。
    result["input_digests"] = {
        k: "sha256:" + hashlib.sha256(v.encode("utf-8")).hexdigest()
        for k, v in texts.items()}
    result.pop("result_digest", None)
    result["result_digest"] = _digest(result)
    os.makedirs(args.output_dir, exist_ok=True)
    jp = os.path.join(args.output_dir, "eval_score.json")
    mp = os.path.join(args.output_dir, "eval_score.md")
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)
    with open(mp, "w", encoding="utf-8") as fh:
        fh.write(render_score_md(result) + "\n")
    print(f"arms=2 -> {jp} , {mp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
