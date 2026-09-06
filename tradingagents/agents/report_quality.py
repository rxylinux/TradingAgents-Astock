"""结构化报告质量卡与确定性结论限制（N01）。

复用 ``quality_gate`` 的硬检查评级（单一事实来源，绝不出现第二套评级算
法），把结果整理为可 JSON 序列化的 ``data_quality`` 结构，并渲染确定性的
质量限制提示。

状态语义（固定，勿改）：

- ``complete``    全部启用报告 A；
- ``insufficient`` D/F 占**严格多数**（``n//2+1``）；
- ``limited``     其余存在 B/C/D/F 的情况;
- ``unknown``     没有可检查集合，或旧报告没有结构化记录。

状态只表示**报告完整性检查**（长度/表格/失败标记等硬信号），不是事实
准确率、投资胜率或校准后的置信度——UI 与导出不得把它表述成「可信度」。
"""

from __future__ import annotations

from typing import Any, Optional

from tradingagents.agents.quality_gate import (
    ANALYST_NAMES,
    DEFAULT_ANALYSTS,
    REPORT_FIELDS,
    _hard_check_report,
)

SCHEMA_VERSION = 1

# 状态常量（对外字符串，保持稳定）
STATUS_COMPLETE = "complete"
STATUS_LIMITED = "limited"
STATUS_INSUFFICIENT = "insufficient"
STATUS_UNKNOWN = "unknown"

# 确定性提示的稳定前缀——幂等追加的判定依据（提示只出现一次）
_LIMITATION_NOTICE_TAG = "⚠️ 数据质量限制（报告完整性检查）"

# 硬检查失败等级
FAILING_GRADES = ("D", "F")


def _derive_status(active: list, grades: list) -> str:
    """按任务书固定规则推导完整性状态。"""
    if not active or not grades:
        return STATUS_UNKNOWN
    n = len(grades)
    fails = sum(1 for g in grades if g in FAILING_GRADES)
    if fails >= n // 2 + 1:
        return STATUS_INSUFFICIENT
    if any(g != "A" for g in grades):
        return STATUS_LIMITED
    return STATUS_COMPLETE


def assess_reports(state: dict, active_analysts: list) -> dict:
    """对启用角色运行硬检查，产出结构化 ``data_quality``。

    ``state`` 缺少报告字段按空报告处理（该角色启用但未产出 → F）；主动
    未启用的角色不出现、不计失败。只复用 ``_hard_check_report``，不引入
    新评级，也不做任何模型调用。
    """
    active = [a for a in DEFAULT_ANALYSTS if a in set(active_analysts or [])]

    reports = {}
    grades = []
    per_analyst = []
    limitations = []

    for role in active:
        field = REPORT_FIELDS[role]
        report = (state.get(field) or "") if isinstance(state, dict) else ""
        reports[field] = report
        grade, detail = _hard_check_report(role, report)
        grades.append(grade)
        per_analyst.append(
            {
                "role": role,
                "name": ANALYST_NAMES[role],
                "grade": grade,
                "detail": detail,
                "chars": len(report or ""),
            }
        )
        if grade in FAILING_GRADES:
            limitations.append(f"{ANALYST_NAMES[role]}（{role}）: {detail}")
        elif grade in ("B", "C"):
            limitations.append(f"{ANALYST_NAMES[role]}（{role}）: {detail}")

    fail_count = sum(1 for g in grades if g in FAILING_GRADES)
    status = _derive_status(active, grades)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "selected_analysts": list(active),
        "analysts": per_analyst,
        "active_count": len(active),
        "fail_count": fail_count,
        "limitations": limitations,
    }


def is_structured(dq: Any) -> bool:
    """``data_quality`` 是否为本模块产出的结构化记录（旧 state 兼容判定）。"""
    return (
        isinstance(dq, dict)
        and dq.get("schema_version") == SCHEMA_VERSION
        and dq.get("status") in
        (STATUS_COMPLETE, STATUS_LIMITED, STATUS_INSUFFICIENT, STATUS_UNKNOWN)
    )


def has_limitations(dq: Any) -> bool:
    """状态为 limited / insufficient 时需要确定性限制提示。"""
    return is_structured(dq) and dq.get("status") in (STATUS_LIMITED, STATUS_INSUFFICIENT)


def render_limitation_notice(dq: Any) -> str:
    """确定性质量提示（无模型调用；幂等标记由调用方用 tag 判定）。

    任务书要求：limited/insufficient 时最终组合决策必须附带本提示，即便
    模型不遵循提示词它也不能消失；提示只出现一次（幂等）。complete 不附
    提示；unknown / 缺失（旧 state）返回空串——**不凭空为旧报告生成
    「检查通过」或限制**。
    """
    if not has_limitations(dq):
        return ""
    lines = [
        f"{_LIMITATION_NOTICE_TAG}：本次结论为受限研判。",
        f"完整性状态: {dq['status']}（{dq.get('active_count', 0)} 位启用分析师中 "
        f"{dq.get('fail_count', 0)} 位报告未通过完整性检查）。",
        "限制来源（报告完整性，不代表事实准确率）:",
    ]
    for item in dq.get("limitations") or []:
        lines.append(f"- {item}")
    lines.append(
        "该提示由代码硬检查确定性生成并随决策保存；使用本结论时请结合上述缺失评估影响。"
    )
    return "\n".join(lines)


def limitation_notice_tag() -> str:
    """幂等标记：提示是否已存在于决策文本。"""
    return _LIMITATION_NOTICE_TAG


def append_limitation_notice(decision_text: str, dq: Any) -> str:
    """把确定性提示追加到最终决策文本（幂等：**当前完整通知**已存在则原样返回）。

    complete / unknown / 缺失时不追加（保留原文本与原评级——不强制 Hold，
    不把资料不足伪装成中性判断）。

    幂等判定必须匹配**当前这次评估的完整通知文本**，而不是仅匹配标记行：
    模型可能自己复述标记、或文本里残留**上一次**（限制内容不同）的通知
    ——只看标记会把当前的真实限制压掉（Codex 审计）。旧的/伪造的通知不
    阻止追加；当前通知恰好一次。
    """
    notice = render_limitation_notice(dq)
    if not notice:
        return decision_text
    text = decision_text or ""
    if notice in text:
        return decision_text
    if not text:
        return notice
    return f"{text}\n\n{notice}"


def quality_context_for_prompt(state: dict) -> str:
    """供研究经理/交易员/组合经理 prompt 使用的精简质量限制段。

    直接从 state 读取（不依赖多空辩论转述）。旧 state 无结构化记录、或
    记录本身是 unknown（无可检查集合）时返回空串——**不凭空生成「检查
    通过」**（Codex 审计：unknown 永不变成 passed 提示）。
    """
    dq = state.get("data_quality") if isinstance(state, dict) else None
    if not is_structured(dq) or dq.get("status") == STATUS_UNKNOWN:
        return ""
    if not has_limitations(dq):
        # complete：如实告知检查通过（这是本运行的记录，不是为旧报告凭空
        # 生成的「通过」）
        return (
            "数据质量提示（报告完整性检查）: 全部启用分析师报告通过硬检查"
            f"（{dq.get('active_count', 0)} 位）。\n"
        )
    lines = [
        "数据质量限制（报告完整性检查，必须纳入考量）:",
        f"- 完整性状态: {dq['status']}；{dq.get('fail_count', 0)}/"
        f"{dq.get('active_count', 0)} 位启用分析师未通过。",
    ]
    for item in dq.get("limitations") or []:
        lines.append(f"- {item}")
    lines.append("结论需明确说明受上述数据限制的影响。")
    return "\n".join(lines) + "\n"


def render_quality_card_md(dq: Any) -> str:
    """质量卡的 Markdown 渲染（Web / Markdown / PDF 三出口共用，N01）。

    旧报告（无结构化记录）显示「未记录结构化质量检查」——不凭空生成
    「检查通过」；状态是**报告完整性检查**，明确不是事实准确率/可信度。
    """
    if not is_structured(dq):
        return (
            "**报告质量卡**: 未记录结构化质量检查（此报告由旧版本生成）。\n"
            "（状态只反映报告完整性，不代表事实准确率或投资胜率）"
        )
    status = dq.get("status")
    lines = [
        f"**报告质量卡**: {status_label(status)}（`{status}`）",
        f"- 启用分析师: {dq.get('active_count', 0)} 位；"
        f"未通过完整性检查: {dq.get('fail_count', 0)} 位",
        "- 逐角色评级（完整性）:",
    ]
    for a in dq.get("analysts") or []:
        lines.append(
            f"  - {a.get('name', a.get('role'))}: {a.get('grade')} — "
            f"{a.get('detail', '')}（{a.get('chars', 0)} 字符）"
        )
    limitations = dq.get("limitations") or []
    if limitations:
        lines.append("- 限制项:")
        lines.extend(f"  - {item}" for item in limitations)
    lines.append(
        "> 状态仅表示报告完整性检查（长度/表格/失败标记等硬信号），"
        "不是事实准确率、投资胜率或置信度。"
    )
    return "\n".join(lines)


def status_label(status: Optional[str]) -> str:
    """UI/导出用的状态文案（明确是完整性检查，不是可信度）。"""
    return {
        STATUS_COMPLETE: "完整（全部通过硬检查）",
        STATUS_LIMITED: "受限（部分报告未达标）",
        STATUS_INSUFFICIENT: "不足（多数报告未达标）",
        STATUS_UNKNOWN: "未记录结构化质量检查",
    }.get(status, "未记录结构化质量检查")
