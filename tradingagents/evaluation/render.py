"""N04 evaluation and comparison Markdown rendering."""

from __future__ import annotations

from typing import Any


def _fmt(v: Any, pct: bool = False) -> str:
    if v is None:
        return "未记录/无法计算"
    if pct and isinstance(v, (int, float)):
        return f"{v:.1%}"
    return str(v)


def render_evaluation_markdown(result: dict) -> str:
    m = result["metrics"]
    lines = [
        f"# 评测结果 · {result['submission_id']}",
        "",
        f"- 案例集: {result['suite_id']} v{result['suite_version']}",
        f"- 评分器版本: {result['grader_version']}",
        f"- Suite digest: `{result['suite_digest'][:16]}…`",
        "",
        "---",
        "",
        "## 汇总",
        "",
        f"| 指标 | 值 | 分母 |",
        f"|---|---|---|",
        f"| 预期 trial 数 | {m['expected']} | — |",
        f"| 已提交 | {m['provided']} | — |",
        f"| 完成 | {m['completed']} | {m['expected']} |",
        f"| 失败 | {m['failed']} | {m['expected']} |",
        f"| 无效 | {m['invalid']} | {m['expected']} |",
        f"| 遗漏 | {m['missing']} | {m['expected']} |",
        f"| 契约通过 | {m['contract_passed']} | {m['expected']} |",
        f"| 完成率 | {_fmt(m['completion_rate'], True)} | {m['expected']} |",
        f"| 失败率 | {_fmt(m['failure_rate'], True)} | {m['expected']} |",
        f"| 契约通过率 | {_fmt(m['contract_pass_rate'], True)} | {m['expected']} |",
        f"| unknown 检查数 | {m['unknown_checks']} | — |",
        "",
        "---",
        "",
        "## 事实标注（人工抽查，非整篇报告事实准确率）",
        "",
        f"- 已人工标注 trial: {m['reviewed_trials']}",
        f"- 标注无效 trial: {m['invalid_reviews']}",
        f"- 未标注 trial: {m['unreviewed_trials']}",
        f"- 已标注声明总数: {m['reviewed_claims']}",
        f"- 引用了证据的声明: {m['evidence_backed_claims']}（含 contradicted 和未来证据，不等于得到支持）",
        f"- 事实支持率: {_fmt(m['factual_support_rate'], True)}（时点内受支持的声明 / 全部已标注声明）",
        f"- 时点违规率: {_fmt(m['temporal_violation_rate'], True)}（仅覆盖有证据声明）",
        "",
        "> 事实支持率仅反映人工抽查声明中受时点内证据支持的比例，",
        "> 不代表整篇报告的事实准确率，也不预测投资收益。",
        "",
        "---",
        "",
        "## 导入测量值",
        "",
    ]
    obs = m.get("observations", {})
    for field in ("latency_ms", "input_tokens", "output_tokens"):
        info = obs.get(field, {})
        lines.append(
            f"- {field}: 观测 {info.get('observed_count', 0)} 次，"
            f"均值 {info.get('mean') if info.get('mean') is not None else 'null'}"
        )
    costs = obs.get("cost_by_currency", {})
    if costs:
        lines.append("- 成本（按币种分组，不跨币相加）:")
        for cur, info in sorted(costs.items()):
            lines.append(f"  - {cur}: 观测 {info['observed_count']} 次，均值 {info['mean']:.4f}")
    else:
        lines.append("- 成本: 无数据")
    lines.append(f"\n> {obs.get('_note', '导入测量值')}，非评分器实测。")

    lines += ["", "---", "", "## 逐 Trial 结果", ""]
    lines += ["| 案例 | Trial | 状态 | 契约 | 标注 |", "|---|---|---|---|---|"]
    for t in result["trials"]:
        rev = t.get("review", {})
        rev_status = rev.get("review_status", "—")
        lines.append(
            f"| {t['case_id']} | {t['trial_id']} | {t['status']} | "
            f"{'✅' if t.get('contract_pass') else '❌'} | {rev_status} |"
        )

    # Degraded detail
    lines += ["", "---", "", "## 未通过 trial 明细", ""]
    for t in result["trials"]:
        if t.get("contract_pass"):
            continue
        lines.append(f"### {t['case_id']} / trial {t['trial_id']}（{t['status']}）")
        if t.get("detail"):
            lines.append(f"- {t['detail']}")
        for c in t.get("checks", []):
            icon = {"pass": "✅", "fail": "❌", "unknown": "❓", "not_applicable": "—"}.get(c["status"], "?")
            lines.append(f"  {icon} {c['check_id']}: {c['status']} — {c['detail']}")
        lines.append("")

    lines += [
        "---",
        "",
        "> 契约通过表示报告完整性检查全部通过，不代表事实准确率或投资胜率。",
        "",
    ]
    return "\n".join(lines)


def render_comparison_markdown(result: dict) -> str:
    s = result["summary"]
    lines = [
        f"# 评测对比 · {result['baseline_submission']} → {result['candidate_submission']}",
        "",
        f"- Suite: `{result['suite_digest'][:16]}…`",
        f"- 评分器: {result['grader_version']}",
        "",
        "---",
        "",
        "## 总体变化",
        "",
        f"| 指标 | 基线 | 候选 |",
        f"|---|---|---|",
    ]
    for k, label in [
        ("contract_pass_rate", "契约通过率"),
        ("completion_rate", "完成率"),
        ("failure_rate", "失败率"),
    ]:
        b = result["baseline_metrics"].get(k)
        c = result["candidate_metrics"].get(k)
        lines.append(f"| {label} | {_fmt(b, True)} | {_fmt(c, True)} |")

    lines += [
        "",
        f"配对结果: **改善 {s['improved']}** · **退化 {s['degraded']}** · "
        f"不变 {s['unchanged']} · 双方不通过 {s['both_fail']}",
        "",
        "---",
        "",
        "## 具体退化检查项",
        "",
    ]
    dc = result.get("degraded_checks", [])
    if not dc:
        lines.append("无退化检查项。")
    else:
        lines += ["| 案例 | Trial | 检查 | 基线 | 候选 |", "|---|---|---|---|---|"]
        for d in dc:
            lines.append(
                f"| {d['case_id']} | {d['trial_id']} | {d['check_id']} | "
                f"{d['baseline_status']} | {d['candidate_status']} |"
            )

    pr = result.get("paired_review", {})
    lines += [
        "",
        "---",
        "",
        "## 配对事实标注比较",
        "",
        f"- 共同覆盖 trial 数: {pr.get('covered_trials', 0)}",
        f"- 支持率改善: {pr.get('support_improved', 0)}",
        f"- 支持率退化: {pr.get('support_degraded', 0)}",
        "",
        "> 配对比较仅在双方均有有效人工标注的 trial 上进行；",
        "> 覆盖范围不同的总体比率不能直接称提升或退化。",
        "",
        "---",
        "",
        "> 本对比仅展示差异，不构成模型能力评测或收益预测。",
        "",
    ]
    return "\n".join(lines)
