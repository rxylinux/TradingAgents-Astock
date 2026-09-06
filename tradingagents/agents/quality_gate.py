from typing import Annotated, Optional

REPORT_FIELDS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
    "policy": "policy_report",
    "hot_money": "hot_money_report",
    "lockup": "lockup_report",
}

ANALYST_NAMES = {
    "market": "技术分析师",
    "social": "情绪分析师",
    "news": "新闻分析师",
    "fundamentals": "基本面分析师",
    "policy": "政策分析师",
    "hot_money": "游资追踪师",
    "lockup": "解禁监控师",
}

# 默认（个股）全集：state 未声明 selected_analysts 时按七位检查。
DEFAULT_ANALYSTS = list(REPORT_FIELDS.keys())

MIN_REPORT_LENGTH = 200

FAILURE_MARKERS = [
    "无法获取",
    "I cannot retrieve",
    "I don't have access",
    "unable to fetch",
    "工具调用失败",
]


def _active_analysts(state, fallback=None) -> list:
    """A10: 本次启用且适用的分析师集合。

    优先级：``state.selected_analysts``（新断点/初始 state 随图写入）→
    **工厂闭包 fallback**（旧断点无集合元数据时取当前实际图的启用集合
    ——不能把未知旧 state 一律当七位：单 market 图恢复会凭空多出 6 个
    F，指数图多出 2 个）→ 全集七位（独立调用 ``create_quality_gate(llm)``
    无任何上下文时的兼容默认）。

    主动未启用的角色不参与硬检查、不计入失败比例、不出现在 LLM 复审
    提示词中。
    """
    selected = None
    if isinstance(state, dict):
        selected = state.get("selected_analysts")
    if not selected and fallback is not None:
        try:
            selected = fallback()
        except Exception:  # noqa: BLE001 — 闭包失效时落到下一级
            selected = None
    if not selected:
        return list(DEFAULT_ANALYSTS)
    return [a for a in DEFAULT_ANALYSTS if a in set(selected)]


def _hard_check_report(analyst_type: str, report: str) -> tuple:
    """Run hard checks on a single report. Returns (grade, detail)."""
    if not report or not report.strip():
        return ("F", "报告为空")

    length = len(report.strip())
    if length < MIN_REPORT_LENGTH:
        return ("D", f"报告过短 ({length} chars < {MIN_REPORT_LENGTH})")

    failure_count = sum(1 for m in FAILURE_MARKERS if m in report)
    stripped = report
    for m in FAILURE_MARKERS:
        stripped = stripped.replace(m, "")
    if failure_count > 0 and len(stripped.strip()) < MIN_REPORT_LENGTH:
        return ("D", f"报告主要由失败信息构成 ({failure_count} 处)")

    has_table = "|" in report and "---" in report
    missing_count = report.count("[数据缺失")

    issues = []
    if not has_table:
        issues.append("缺少汇总表格")
    if missing_count > 0:
        issues.append(f"{missing_count} 处数据缺失")

    if missing_count >= 3:
        return ("C", "；".join(issues))
    if not has_table or missing_count > 0:
        return ("B", "；".join(issues) if issues else "基本合格")

    return ("A", f"完整 ({length} chars)")


def _build_review_prompt(
    reports: dict, trade_date: str, ticker: str, active: list
) -> str:
    """Build the LLM review prompt — only for the active analyst team (A10)."""
    report_sections = []
    for analyst_type in active:
        field = REPORT_FIELDS[analyst_type]
        name = ANALYST_NAMES[analyst_type]
        content = reports.get(field, "（未运行）")
        if not content:
            content = "（报告为空）"
        if len(content) > 3000:
            content = content[:3000] + "\n... (truncated for review)"
        report_sections.append(f"### {name} ({analyst_type})\n{content}")

    all_reports = "\n\n".join(report_sections)
    review_rows = "\n".join(
        f"| {ANALYST_NAMES[a]} | A/B/C/D/F | 是否匹配交易日 | 列出缺失的必采项 | 简要说明 |"
        for a in active
    )

    return f"""你是数据质量审核员。以下是 {len(active)} 位分析师对 {ticker} 在 {trade_date} 的研究报告。请逐一审核。

{all_reports}

---

请按以下格式输出审核结果（不要输出其他内容）：

## 数据质量审核报告

**标的**: {ticker} | **日期**: {trade_date}

| 分析师 | 评级 | 数据时效 | 缺失项 | 备注 |
|--------|------|----------|--------|------|
{review_rows}

**整体评级**: A/B/C/D/F
**数据可信度**: 高/中/低
**建议**: （如有数据缺失，提醒辩论阶段谨慎使用该报告）

评级标准：
- A: 必采清单全部覆盖，数据时效匹配，有汇总表格
- B: 缺少 1-2 项非关键数据，整体可用
- C: 缺少 3+ 项或有数据时效问题，需谨慎使用
- D: 大量缺失或主要为失败信息，可信度低
- F: 报告为空或完全无效
"""


def create_quality_gate(llm, active_analysts_fallback=None):
    """Factory for the data quality gate node.

    Sits between the last analyst Msg Clear and Bull Researcher.
    Layer 1: hard checks (code). Layer 2: LLM review (one call).
    Writes data_quality_summary to state for downstream consumers.

    A10: only the analysts active in THIS run are checked — disabled roles
    are neither graded nor shown to the LLM; an *enabled* analyst with an
    empty report still fails hard. Active set resolution:
    ``state.selected_analysts`` → ``active_analysts_fallback``（图构建方
    传入的当前启用集合闭包，供旧断点无元数据时使用）→ 全集七位。

    ``active_analysts_fallback`` 可为 ``() -> list[str]`` 闭包或 list。
    """

    if not callable(active_analysts_fallback):
        _fallback_list = active_analysts_fallback
        active_analysts_fallback = (lambda: _fallback_list) if _fallback_list else None

    def quality_gate_node(state) -> dict:
        trade_date = state["trade_date"]
        ticker = state["company_of_interest"]

        active = _active_analysts(state, fallback=active_analysts_fallback)

        reports = {}
        for field in REPORT_FIELDS.values():
            reports[field] = state.get(field, "")

        hard_results = {}
        for analyst_type in active:
            field = REPORT_FIELDS[analyst_type]
            grade, detail = _hard_check_report(analyst_type, reports[field])
            hard_results[analyst_type] = (grade, detail)

        hard_summary_lines = []
        for analyst_type, (grade, detail) in hard_results.items():
            name = ANALYST_NAMES[analyst_type]
            hard_summary_lines.append(f"- {name}: [{grade}] {detail}")
        hard_summary = "\n".join(hard_summary_lines)

        fail_count = sum(
            1 for _, (g, _) in hard_results.items() if g in ("F", "D")
        )
        # **严格多数**未通过硬检查才跳过 LLM 复审：n//2+1。7 位时 =4
        # （与原行为一致）、1 位时 =1；偶数团队恰好一半失败（如 2 位中
        # 1 位、4 位中 2 位）不算多数，复审仍执行（Codex 退回修正）。
        skip_threshold = len(active) // 2 + 1

        llm_review = ""
        if fail_count < skip_threshold:
            try:
                review_prompt = _build_review_prompt(
                    reports, trade_date, ticker, active
                )
                response = llm.invoke(review_prompt)
                llm_review = response.content
            except Exception as e:
                llm_review = f"（LLM 复审失败: {type(e).__name__}: {e}）"

        summary = (
            f"## 数据质量门控结果\n\n"
            f"**标的**: {ticker} | **交易日**: {trade_date} | "
            f"**参与分析师**: {len(active)} 位\n\n"
            f"### 硬检查结果\n{hard_summary}\n\n"
            f"### LLM 复审\n"
            f"{llm_review if llm_review else '（跳过 — 多数报告未通过硬检查）'}\n"
        )

        # N01: 同一硬检查结果的结构化质量卡（单一评级来源，无额外模型调用；
        # LLM 自述评级只出现在文字 summary，不覆盖代码硬检查）。旧文字接口
        # data_quality_summary 保持不变。
        from tradingagents.agents.report_quality import assess_reports

        data_quality = assess_reports(state, active)

        return {"data_quality_summary": summary, "data_quality": data_quality}

    return quality_gate_node
