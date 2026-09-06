"""历史报告对比（N03）：纯计算、零模型调用、零网络。

比较同一标的与同一 ``instrument_type`` 的两份已保存报告（不同分析日期允
许）；不同标的、或个股/指数混用**明确拒绝**（``MismatchedReportsError``）。
本轮不做跨股评分排名。

证据边界（任务书红线）：

- 旧报告缺配置档案/质量卡时**标为未知**，不默认成「相同」或「零」；
- **不输出**「新模型更准确」「收益提升」之类没有实证的判断——文本变化比
  例不是模型优劣分数；
- 配置不同或元数据缺失时，明确写出「比较条件差异/无法确认」；
- 交易员字段 canonical（``trader_investment_plan``）优先、legacy
  （``trader_investment_decision``）回退且**只比较一次**；
- 「角色未运行」（不在启用集合）与「已启用但报告空白」可区分；
- 评级缺失**不能默认 Hold**——原值原样展示，缺失即「未记录」。

CLI::

    python -m tradingagents.report_comparison LEFT.json RIGHT.json [--output FILE.md]
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from tradingagents.agents.quality_gate import ANALYST_NAMES, REPORT_FIELDS
from tradingagents.agents.report_quality import is_structured, status_label
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.run_records import is_run_metadata
from web.report_fields import trader_plan

SCHEMA_VERSION = 1

_UNKNOWN = "未知（未记录）"
_NOT_RUN = "未运行"
_EMPTY = "已启用但报告空白"


class MismatchedReportsError(ValueError):
    """两份报告不可比较（不同标的或个股/指数混用）。"""


def _infer_type_from_ticker(ticker: str) -> str:
    """旧报告无 type 时的保守推断——指数注册表可靠判定；不确定时写
    ``unknown``，不凭空默认 stock（Codex 退回 #3）。"""
    from tradingagents.dataflows.index_registry import is_index_symbol

    if is_index_symbol(ticker):
        return "index"
    return "unknown"


def _rating_of(state: dict) -> str:
    """评级原值：解析失败/缺失不默认 Hold，原样标「未记录」。"""
    text = (state.get("final_trade_decision") or "").strip()
    if not text:
        return "未记录（无最终决策文本）"
    rating = parse_rating(text, default="")
    return rating if rating else "未记录（无法解析评级）"


def _active_roles(state: dict) -> Optional[List[str]]:
    """启用集合：优先质量卡，回退 run_metadata 快照，旧报告 None=未知。"""
    dq = state.get("data_quality")
    if is_structured(dq) and dq.get("selected_analysts"):
        return list(dq["selected_analysts"])
    meta = state.get("run_metadata")
    if is_run_metadata(meta):
        snap = meta.get("config_snapshot") or {}
        roles = snap.get("selected_analysts")
        if roles:
            return list(roles)
    return None


def _role_report_text(state: dict, role: str) -> str:
    return str(state.get(REPORT_FIELDS[role]) or "")


def _role_label(state: dict, role: str, active: Optional[List[str]]) -> str:
    """区分「未运行」「已启用但空白」「团队未知」。"""
    if active is None:
        # 旧报告无启用集合：不能断言「已启用」也不能断言「未运行」
        if not _role_report_text(state, role).strip():
            return "团队未知（报告空白）"
        return "团队未知（有报告文本）"
    if role not in active:
        return _NOT_RUN
    if not _role_report_text(state, role).strip():
        return _EMPTY
    return "已运行"


def _config_of(state: dict) -> Optional[Dict[str, Any]]:
    meta = state.get("run_metadata")
    if not is_run_metadata(meta):
        return None
    snap = meta.get("config_snapshot")
    return dict(snap) if isinstance(snap, dict) else None


def _config_diff(left_cfg: Optional[dict], right_cfg: Optional[dict]) -> Dict[str, Any]:
    """公开配置差异；任一侧缺失 → 无法比较（不默认相同）。"""
    if left_cfg is None or right_cfg is None:
        return {"comparable": False,
                "left_known": left_cfg is not None,
                "right_known": right_cfg is not None,
                "changed_keys": None,
                "details": []}
    keys = sorted(set(left_cfg) | set(right_cfg))
    changed = [k for k in keys if left_cfg.get(k) != right_cfg.get(k)]
    details = []
    for k in changed:
        details.append({
            "key": k,
            "left": left_cfg.get(k, "<缺失>"),
            "right": right_cfg.get(k, "<缺失>"),
        })
    return {"comparable": True, "changed_keys": changed, "details": details}


def _quality_of(state: dict) -> Dict[str, Any]:
    dq = state.get("data_quality")
    if not is_structured(dq):
        return {"known": False, "status": None, "limitations": None,
                "fail_count": None, "active_count": None,
                "status_label": "未记录结构化质量检查"}
    status = dq.get("status")
    # Codex 退回 #4：status=unknown 表示「有结构化记录但状态未知」——
    # 不能当作已知状态参与差异比较。
    known = status is not None and status != "unknown"
    return {
        "known": known,
        "status": status if known else None,
        "status_label": status_label(status),
        "limitations": list(dq.get("limitations") or []) if known else None,
        "fail_count": dq.get("fail_count") if known else None,
        "active_count": dq.get("active_count") if known else None,
    }


def _quality_diff(left_q: dict, right_q: dict) -> Dict[str, Any]:
    # Codex 退回 #4：status=unknown 也不能作已知状态比较——结构化记录存在
    # 但状态未知时，差异同样无法确认。
    left_usable = left_q["known"] and left_q["status"] is not None
    right_usable = right_q["known"] and right_q["status"] is not None
    if not left_usable or not right_usable:
        return {"comparable": False,
                "left_known": left_usable, "right_known": right_usable}
    lim_l, lim_r = set(left_q["limitations"]), set(right_q["limitations"])
    return {
        "comparable": True,
        "status_changed": left_q["status"] != right_q["status"],
        "left_status": left_q["status"],
        "right_status": right_q["status"],
        "new_limitations": sorted(lim_r - lim_l),
        "resolved_limitations": sorted(lim_l - lim_r),
    }


def _text_delta(left: str, right: str) -> List[str]:
    """unified diff 行（可展开的文本差异），空输入友好。"""
    if left == right:
        return []
    diff = difflib.unified_diff(
        (left or "").splitlines(), (right or "").splitlines(),
        fromfile="left", tofile="right", lineterm="",
    )
    return list(diff)


def _field_block(state: dict, active: Optional[List[str]]) -> Dict[str, Any]:
    """各报告字段的运行状态与文本（含交易员 canonical 优先、只取一次）。"""
    roles = [r for r in REPORT_FIELDS if active is None or r in (active or [])]
    fields = {}
    for role in roles:
        fields[role] = {
            "label": _role_label(state, role, active),
            "text": _role_report_text(state, role),
        }
    return fields


def compare_reports(left: dict, right: dict) -> dict:
    """纯计算比较两份已保存的报告；不可比较时抛 ``MismatchedReportsError``。"""
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise MismatchedReportsError("报告必须是可解析的 JSON 对象")

    def _ticker(s):
        t = str(s.get("company_of_interest") or s.get("ticker") or "").strip()
        return t.upper()

    def _type(s):
        meta = s.get("run_metadata")
        if is_run_metadata(meta):
            return meta.get("instrument_type")
        return s.get("instrument_type")

    lt, rt = _ticker(left), _ticker(right)
    if not lt or not rt:
        raise MismatchedReportsError("至少一侧缺少标的标识，无法比较")
    if lt != rt:
        raise MismatchedReportsError(f"标的不同（{lt} vs {rt}），拒绝比较")
    ltype, rtype = _type(left), _type(right)
    resolved_type = ltype or rtype or _infer_type_from_ticker(lt)
    if ltype and rtype and ltype != rtype:
        raise MismatchedReportsError(
            f"instrument_type 不同（{ltype} vs {rtype}）：个股与指数报告不可比较"
        )

    left_active = _active_roles(left)
    right_active = _active_roles(right)

    left_cfg, right_cfg = _config_of(left), _config_of(right)
    left_q, right_q = _quality_of(left), _quality_of(right)

    left_fields = _field_block(left, left_active)
    right_fields = _field_block(right, right_active)

    # 分析师正文变化（含交易员与最终决策）
    analyst_changes = []
    for role in REPORT_FIELDS:
        in_l = role in left_fields
        in_r = role in right_fields
        if not in_l and not in_r:
            continue
        lf = left_fields.get(role, {"label": _NOT_RUN, "text": ""})
        rf = right_fields.get(role, {"label": _NOT_RUN, "text": ""})
        if lf["label"] != rf["label"] or lf["text"] != rf["text"]:
            analyst_changes.append({
                "role": role,
                "name": ANALYST_NAMES[role],
                "left_label": lf["label"],
                "right_label": rf["label"],
                "text_changed": lf["text"] != rf["text"],
                "delta": _text_delta(lf["text"], rf["text"]) if lf["text"] != rf["text"] else [],
            })

    trader_l = trader_plan(left) or ""
    trader_r = trader_plan(right) or ""
    trader_changed = trader_l != trader_r

    decision_l = str(left.get("final_trade_decision") or "")
    decision_r = str(right.get("final_trade_decision") or "")
    decision_changed = decision_l != decision_r

    result = {
        "schema_version": SCHEMA_VERSION,
        "ticker": lt,
        "instrument_type": resolved_type,
        "left": {
            "date": str(left.get("trade_date") or ""),
            "run_id": (
                (left.get("run_metadata") or {}).get("run_id")
                if is_run_metadata(left.get("run_metadata")) else None
            ),
            "rating": _rating_of(left),
        },
        "right": {
            "date": str(right.get("trade_date") or ""),
            "run_id": (
                (right.get("run_metadata") or {}).get("run_id")
                if is_run_metadata(right.get("run_metadata")) else None
            ),
            "rating": _rating_of(right),
        },
        "rating_changed": _rating_of(left) != _rating_of(right),
        "config_diff": _config_diff(left_cfg, right_cfg),
        "quality": {
            "left": left_q,
            "right": right_q,
            "diff": _quality_diff(left_q, right_q),
        },
        "analyst_changes": analyst_changes,
        "trader": {
            "changed": trader_changed,
            "left_known": bool(trader_l),
            "right_known": bool(trader_r),
            "delta": _text_delta(trader_l, trader_r) if trader_changed else [],
        },
        "final_decision": {
            "changed": decision_changed,
            "left_known": bool(decision_l),
            "right_known": bool(decision_r),
            "delta": _text_delta(decision_l, decision_r) if decision_changed else [],
        },
        "same_date": str(left.get("trade_date") or "") == str(right.get("trade_date") or ""),
    }
    return result


# ── Markdown 渲染（Web 下载与 CLI 共用，不增加 LLM 费用） ──


def _fmt_val(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def _side_tag(which: str, comp: dict) -> str:
    meta = comp[which]
    run = meta["run_id"][:8] if meta["run_id"] else "未记录"
    return f"{meta['date']}（运行 {run}）"


def render_comparison_markdown(comparison: dict) -> str:
    """比较结果的 Markdown（可下载；纯文本渲染，无模型调用）。"""
    ticker = comparison["ticker"]
    left_tag = _side_tag("left", comparison)
    right_tag = _side_tag("right", comparison)
    same_date = "同一分析日" if comparison["same_date"] else "不同分析日"
    lines = [
        f"# 历史报告对比 · {ticker}（{comparison['instrument_type']}）",
        "",
        f"- 左侧: {left_tag}",
        f"- 右侧: {right_tag}",
        f"- 比较范围: {same_date}的两次运行",
        "",
        "---",
        "",
        "## 评级对比（原值）",
        "",
        f"| | 评级 |",
        f"|---|---|",
        f"| 左侧 | {comparison['left']['rating']} |",
        f"| 右侧 | {comparison['right']['rating']} |",
        "",
    ]
    if comparison["rating_changed"]:
        lines.append("⚠️ 评级发生变化。评级变化反映两次运行的决策文本不同，"
                      "**不能据此判断模型能力或未来收益**。")
    else:
        lines.append("两次评级相同（或同为缺失）。")
    lines += ["", "---", "", "## 公开配置差异", ""]

    cfg = comparison["config_diff"]
    if not cfg["comparable"]:
        known = []
        if not cfg["left_known"]:
            known.append("左侧")
        if not cfg["right_known"]:
            known.append("右侧")
        lines.append(
            f"⚠️ {'与'.join(known)}缺少配置档案（旧版本报告），配置差异**无法确认**"
            "——不能默认为相同。"
        )
    elif cfg["changed_keys"]:
        lines.append(f"两次运行的公开配置存在差异（{len(cfg['changed_keys'])} 项）：")
        lines += ["", "| 配置项 | 左侧 | 右侧 |", "|---|---|---|"]
        for d in cfg["details"]:
            lines.append(f"| {d['key']} | {_fmt_val(d['left'])} | {_fmt_val(d['right'])} |")
        lines += ["", "⚠️ 以下差异是在**配置不同**的条件下比较的；差异不表示任何一方更优。"]
    else:
        lines.append("公开配置完全一致（可确认的范围内）。")

    lines += [
        "", "---", "", "## 结构化质量状态（报告完整性，非事实准确率）", "",
    ]
    q = comparison["quality"]
    for side in ("left", "right"):
        info = q[side]
        if not info["known"]:
            label = "未记录结构化质量检查"
            lines.append(f"- {'左侧' if side == 'left' else '右侧'}: {label}（旧格式）")
        else:
            lines.append(
                f"- {'左侧' if side == 'left' else '右侧'}: {info['status_label']}"
                f"（{info['fail_count']}/{info['active_count']} 未通过）"
            )
    qd = q["diff"]
    if not qd["comparable"]:
        lines += ["", "⚠️ 质量状态差异无法确认（至少一侧为旧格式）。"]
    else:
        if qd["status_changed"]:
            lines.append(f"\n完整性状态变化: {qd['left_status']} → {qd['right_status']}")
        if qd["new_limitations"]:
            lines.append("\n新增限制:")
            lines += [f"- {x}" for x in qd["new_limitations"]]
        if qd["resolved_limitations"]:
            lines.append("\n解除限制:")
            lines += [f"- {x}" for x in qd["resolved_limitations"]]
        if not (qd["status_changed"] or qd["new_limitations"] or qd["resolved_limitations"]):
            lines.append("\n质量状态与限制无变化。")

    lines += ["", "---", "", "## 分析师报告变化", ""]
    changes = comparison["analyst_changes"]
    if not changes:
        lines.append("启用角色的报告文本均无变化。")
    for c in changes:
        label_note = (
            f"（{c['left_label']} → {c['right_label']}）"
            if c["left_label"] != c["right_label"] else ""
        )
        lines.append(f"### {c['name']}（{c['role']}）{label_note}")
        if c["delta"]:
            # Codex 退回 #2：可下载的完整 Markdown 必须保留**所有**差异行
            # （长报告末尾的结论性变化不能静默丢失，也不加截断提示）。
            lines += ["", "```diff"]
            lines += c["delta"]
            lines += ["```"]
        lines.append("")

    lines += ["---", "", "## 交易员计划", ""]
    tr = comparison["trader"]
    if tr["changed"]:
        lines.append("交易员计划文本发生变化。")
        if tr["delta"]:
            lines += ["", "```diff"] + tr["delta"] + ["```"]
    elif tr["left_known"] and tr["right_known"]:
        lines.append("交易员计划无变化。")
    else:
        known = "两侧均有" if tr["left_known"] and tr["right_known"] else "至少一侧缺失"
        lines.append(f"交易员计划比较条件：{known}。")

    lines += ["", "---", "", "## 最终决策正文", ""]
    fd = comparison["final_decision"]
    if fd["changed"]:
        lines.append("最终决策文本发生变化。")
        if fd["delta"]:
            lines += ["", "```diff"] + fd["delta"] + ["```"]
    else:
        lines.append("最终决策文本无变化。")

    lines += [
        "",
        "---",
        "",
        "> 本对比仅展示两次运行之间的**差异**，不构成模型能力评测或收益预测；"
        "配置不同或元数据缺失处的比较条件已在正文标注。",
        "",
    ]
    return "\n".join(lines)


# ── CLI 入口 ──


def _main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    output = None
    paths = []
    i = 0
    while i < len(argv):
        if argv[i] == "--output":
            if i + 1 >= len(argv):
                print("错误: --output 需要一个文件路径", file=sys.stderr)
                return 2
            output = argv[i + 1]
            i += 2
        elif argv[i].startswith("--output="):
            output = argv[i].split("=", 1)[1]
            i += 1
        elif argv[i] in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            paths.append(argv[i])
            i += 1

    if len(paths) != 2:
        print("用法: python -m tradingagents.report_comparison LEFT.json RIGHT.json "
              "[--output FILE.md]", file=sys.stderr)
        return 2

    if output:
        # Codex 退回 #1：输出不得与任一输入指向同一文件——resolve 处理符号
        # 链接/相对路径；同 inode（硬链接）也拒绝；输出路径为目录时同样报错。
        out_p = Path(output)
        if out_p.exists() and out_p.is_dir():
            print(f"错误: --output 是目录而非文件: {output}", file=sys.stderr)
            return 1
        out_abs = out_p.resolve()
        for inp in paths:
            inp_abs = Path(inp).resolve()
            if out_abs == inp_abs:
                print("错误: --output 不能与输入报告路径相同（拒绝覆盖源报告）",
                      file=sys.stderr)
                return 1
            if out_p.exists() and inp_abs.exists() and out_p.samefile(inp_abs):
                print("错误: --output 与输入报告是同一文件（硬链接/符号链接）",
                      file=sys.stderr)
                return 1

    try:
        left = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
        right = json.loads(Path(paths[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"错误: 无法读取报告 — {e}", file=sys.stderr)
        return 2

    # 比较失败时绝不写输出文件（下游 return 1 在 render 之前）

    try:
        comparison = compare_reports(left, right)
    except MismatchedReportsError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    md = render_comparison_markdown(comparison)
    if output:
        try:
            Path(output).write_text(md, encoding="utf-8")
        except OSError as e:
            print(f"错误: 写入输出文件失败 — {e}", file=sys.stderr)
            return 1
        print(f"对比结果已写入 {output}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
