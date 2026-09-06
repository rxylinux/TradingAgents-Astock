"""报告状态字段契约（A14）：实时 state、统一 JSON、Web 展示、Markdown 与
PDF 导出共用一致的取值规则。

图内交易员产出字段是 ``trader_investment_plan``；旧版统一 JSON 曾把它改
名成 ``trader_investment_decision`` 落盘，Web 展示/导出又只读后者——于是
实时页面缺交易员内容、历史重载才出现，质量结论则反向只在实时可见。本模块
给出唯一取值口：

- ``trader_plan(state)``：canonical 优先（``trader_investment_plan``），
  旧 JSON 的 ``trader_investment_decision`` 作为回退；两者同时存在时只取
  canonical，展示端绝不重复渲染。
- ``quality_summary(state)``：``data_quality_summary``（实时与落盘一致，
  由 ``_log_state`` 双写保障）。
"""

from __future__ import annotations

from typing import Any, Optional

# canonical 与 legacy 字段名（读取优先级：先 canonical）
TRADER_PLAN_FIELD = "trader_investment_plan"
TRADER_PLAN_LEGACY_FIELD = "trader_investment_decision"
QUALITY_SUMMARY_FIELD = "data_quality_summary"


def trader_plan(state: dict[str, Any]) -> Optional[str]:
    """交易员计划：canonical 优先，legacy 兼容，只取一次。"""
    if not isinstance(state, dict):
        return None
    plan = state.get(TRADER_PLAN_FIELD)
    if plan:
        return plan
    return state.get(TRADER_PLAN_LEGACY_FIELD) or None


def quality_summary(state: dict[str, Any]) -> Optional[str]:
    """数据质量结论：实时与历史重载共用同一字段。"""
    if not isinstance(state, dict):
        return None
    return state.get(QUALITY_SUMMARY_FIELD) or None
