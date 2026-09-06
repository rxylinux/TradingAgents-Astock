"""运行身份与公开配置档案（N02）。

每次 fresh 运行生成稳定身份（``run_id`` + ``created_at`` + 公开配置快照
与指纹）；SQLite 恢复沿用 checkpoint 里已有的同一身份——**不在重试时重建
身份**，也不触碰既有 checkpoint key（LangGraph 线程状态语义：新字段随
state 保存与恢复）。

安全边界（任务书红线）：

- 配置快照是**字段白名单**：绝不保存 API Key、认证/请求头、Cookie、原始
  backend URL、SDK 私有目录、环境变量全集或任意未知嵌套配置；角色覆盖
  （``role_llms``）只保留 provider/model 等明确公开字段。
- 快照深拷贝，不引用调用方可变字典。
- 指纹 = 白名单快照的规范排序 JSON 的 SHA-256；集合顺序不产生无意义差异
  （分析师集合排序后入档），模型/窗口/数据源改变必然改变指纹。
- 该档案**表示配置**，不声称记录了每次请求实际落到的供应商——SDK 后备
  仍可能改变实际调用路径。本指纹用于档案与对比，不新增恢复拒绝机制。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1

# 路由表（data_vendors/tool_vendors）中疑似凭据的键名——即使值是标量也不入档
_SENSITIVE_KEY_MARKERS = (
    "api_key", "apikey", "key", "token", "secret", "authorization",
    "credential", "password", "header", "cookie",
)


# 已知的公开路由键（Codex 边审：未知键一律剔除——路由表只声明已知数据源
# 的选择，任意自定义键可能是私有连接串，不能因为值是字符串就放行）。
KNOWN_DATA_VENDOR_KEYS = frozenset({
    "core_stock_apis", "technical_indicators", "fundamental_data",
    "news_data", "signal_data",
})
KNOWN_TOOL_VENDOR_KEYS = frozenset({
    "get_stock_data", "get_indicators", "get_fundamentals",
    "get_balance_sheet", "get_cashflow", "get_income_statement",
    "get_news", "get_global_news", "get_insider_transactions",
    "get_profit_forecast", "get_hot_stocks", "get_northbound_flow",
    "get_concept_blocks", "get_fund_flow", "get_dragon_tiger_board",
    "get_lockup_expiry", "get_industry_comparison",
})


def _sanitize_routing_map(vendors: Any, known_keys: frozenset) -> Dict[str, str]:
    """路由表只保留「已知公开路由键」且值为字符串的项。

    未知键（可能是私有连接配置）、嵌套 dict、以及任何疑似凭据键名一律
    丢弃——路由表声明的是「用哪家数据源」，不是怎么认证它。
    """
    if not isinstance(vendors, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for name, value in vendors.items():
        if not isinstance(value, str):
            continue
        if str(name) not in known_keys:
            continue
        lowered = str(name).lower()
        if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
            continue
        cleaned[str(name)] = value
    return cleaned


# 公开配置白名单：顶层键 → 处理方式。
#   None 原样收录；callable(item) -> normalized 做归一（如集合排序）。
PUBLIC_CONFIG_WHITELIST: Dict[str, Any] = {
    "selected_analysts": lambda v: sorted(v) if isinstance(v, (list, tuple, set)) else v,
    "llm_provider": None,
    "quick_think_llm": None,
    "deep_think_llm": None,
    "instrument_type": None,
    "output_language": None,
    "market_lookback_days": None,
    "max_debate_rounds": None,
    "max_risk_discuss_rounds": None,
    "max_tokens": None,
    "data_vendors": lambda v: _sanitize_routing_map(v, KNOWN_DATA_VENDOR_KEYS),
    "tool_vendors": lambda v: _sanitize_routing_map(v, KNOWN_TOOL_VENDOR_KEYS),
    # 模型与 provider 公开配置（任务书白名单「模型与 provider 配置」）：
    # 订阅覆盖、SDK 模型与降级模型、推理力度与辩论早停都是影响研究行为
    # 的公开开关。秘密类（key/url/header）永不在此列。
    "deep_think_provider_override": None,
    "quick_think_provider_override": None,
    "agent_sdk_model": None,
    "agent_sdk_quick_model": None,
    "agent_sdk_fallback_provider": None,
    "agent_sdk_fallback_model": None,
    "openai_reasoning_effort": None,
    "google_thinking_level": None,
    "anthropic_effort": None,
    "enable_debate_early_stopping": None,
}

# role_llms 覆盖中允许保存的公开字段（provider/model 公开；其余一律丢弃）
ROLE_LLM_PUBLIC_FIELDS = ("provider", "model")


_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _sanitize_role_llm(role_llms: Any) -> Dict[str, Dict[str, Any]]:
    """角色模型覆盖只保留明确公开的 provider/model 字段。

    值必须是标量字符串——嵌套结构（如私有模型配置对象）整项丢弃，不是
    把它字符串化保存。
    """
    if not isinstance(role_llms, dict):
        return {}
    cleaned: Dict[str, Dict[str, Any]] = {}
    for role, spec in role_llms.items():
        if not isinstance(spec, dict):
            continue
        entry = {
            k: spec[k]
            for k in ROLE_LLM_PUBLIC_FIELDS
            if isinstance(spec.get(k), str)
        }
        if entry:
            cleaned[str(role)] = entry
    return cleaned


def build_config_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    """从运行配置提取**公开白名单**快照（深拷贝，不含任何秘密）。"""
    config = config or {}
    snapshot: Dict[str, Any] = {}
    for key, normalizer in PUBLIC_CONFIG_WHITELIST.items():
        if key not in config or config[key] is None:
            continue
        value = copy.deepcopy(config[key])
        if normalizer is not None:
            try:
                value = normalizer(value)
            except TypeError:
                pass
        snapshot[key] = value
    role_llms = _sanitize_role_llm(config.get("role_llms"))
    if role_llms:
        snapshot["role_llms"] = role_llms
    return snapshot


def config_fingerprint(snapshot: Dict[str, Any]) -> str:
    """白名单快照的稳定指纹：规范排序 JSON 的 SHA-256。"""
    canonical = json.dumps(
        snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def new_run_metadata(
    config: Dict[str, Any],
    ticker: str,
    trade_date: str,
    instrument_type: Optional[str] = None,
    selected_analysts: Optional[list] = None,
) -> Dict[str, Any]:
    """生成一次新运行的完整档案（每次调用必然产生新 run_id）。

    ``selected_analysts`` 是**图实际启用**的分析师集合（构造参数）——
    优先于 config dict 里可能存在/缺失的同名键：档案记录的是本次运行
    真正生效的团队（Codex 冻结审计语义）。
    """
    snapshot = build_config_snapshot(config)
    if selected_analysts:
        snapshot["selected_analysts"] = sorted(selected_analysts)
    resolved_type = instrument_type or config.get("instrument_type") or "stock"
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex,
        "ticker": str(ticker),
        "trade_date": str(trade_date),
        "instrument_type": resolved_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_snapshot": snapshot,
        "config_fingerprint": config_fingerprint(snapshot),
    }


def is_run_metadata(meta: Any) -> bool:
    """是否为本模块产出的运行档案（旧 state/checkpoint 兼容判定）。"""
    return (
        isinstance(meta, dict)
        and meta.get("schema_version") == SCHEMA_VERSION
        and isinstance(meta.get("run_id"), str)
        and bool(_RUN_ID_RE.fullmatch(meta.get("run_id") or ""))
    )


def validate_run_id(run_id: str) -> bool:
    """run_id 必须是 32 位小写 hex——版本目录名据此拒绝路径穿越。"""
    return isinstance(run_id, str) and bool(_RUN_ID_RE.fullmatch(run_id))
