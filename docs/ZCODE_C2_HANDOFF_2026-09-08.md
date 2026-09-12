# C2 研究假设卡交接（ZCode → Codex）

日期：2026-09-08（深夜批次，紧接 C1）。状态：完成，待 Codex 独立审核。契约来源：`docs/DECISION_ACCURACY_PLAN_2026-09-08.md` C2 节。

## 实现方式

**沿用组合经理既有结构化调用，不为格式增加任何 LLM 调用**：`PortfolioDecision`（`tradingagents/agents/schemas.py`）新增可选字段（全部空默认，代码永不代填）——`supporting_evidence_ids` / `contradicting_evidence_ids` / `hypotheses_to_verify` / `catalysts` / `invalidation_conditions`（`ThesisCondition`：description + indicator/comparator/threshold/period/source/evidence_id）/ `next_check_trigger`。字段描述即模型输出指令（该文件既有约定）。PM 节点用 render 包装旁路捕获解析对象（渲染输出不变），**一次结构化调用同时产出决策文本与卡片**。

**确定性卡片**（`tradingagents/agents/thesis.py`，全部代码生成，模型自述不能覆盖）：

- 代码字段：instrument/formed_at/analysis_date/run_id 取自运行状态；rating 取自结构化结果（自由文本路径 → unknown）。
- 引用校验（对 C1 evidence_bundle）：悬空 / 发布晚于分析时点（date-only 截止按上海日终，aware 比较）/ 同一 ID 同时支持与反驳（冲突）→ 各自计数并计入限制原因；多空证据并存（不相交有效 ID）不构成冲突。
- `assessment_status=assessable/limited/insufficient/unknown`：unknown=无可观察输入（无质量卡且无证据账本）；insufficient=质量卡 insufficient；limited=任一可观察缺口（悬空/未来/冲突引用、来源失败/部分失败、无有效支持证据、期限 unknown、无失效条件、失效条件缺可观察要素、质量卡 limited、自由文本路径）；assessable=无缺口。每条原因逐项列出。
- `confidence` 恒为 `uncalibrated`；所有出口不出现投资胜率表述。
- 失效条件可观察性由代码判定：indicator+comparator+threshold+period 齐备 → `observable`；否则 `manual_review`（待人工判断，不声称自动触发）。
- 自由文本回退（供应商无结构化输出）：卡片 `structured_output=False`，模型字段 unknown/空，原因明示"未产出结构化假设卡"——不臆造。
- 首批仅生成+落盘+展示：不订阅数据、不交易、不外部提醒、不自动触发。

**流转与出口**：

- `AgentState.thesis_card`（仅 PM 写入）；`_log_state` 随统一 JSON 保存（旧报告 → null）。
- 决策文本确定性追加状态行 `🧪 研究假设卡评估: …（\`status\`）；信心=uncalibrated…`（幂等：完整当前文本已存在则不重复；受限判断不被下游改写冲掉，与 N01 通知同模式叠加）。
- Web expander「🧪 研究假设卡」/ Markdown / PDF「研究假设卡」section 共用 `render_thesis_md`（旧报告显示"未记录"）。

## 同批顺带满足的 Codex 新增 C1 探针（audit 脚本 22:57 更新，未改动脚本本身）

1. `test_real_tool_model_date_cannot_bypass_trusted_run_cutoff` 等 4 项原探针维持通过。
2. 新增 `interrupt_before` 恢复用例（两段 fetch、断点续跑、事件/记录/来源状态不重复不丢失）——通过（reducer 幂等 + 首轮直存自举）。
3. 新增 `test_shared_display_resolves_evidence_id_to_source_article`——`render_evidence_md` 现逐条列出记录（evidence_id → 标题/来源/发布时间 HH:MM/链接，上限 60 条，超出明示）。
4. 新增 `test_unknown_vendor_limitation_reaches_decision_prompt`——`evidence_context_for_prompt` 现携带覆盖说明（含 provenance unknown 限制），无记录但有说明时不再返回空段；RM/Trader/PM 提示词随之获得该限制。

## 验证（全部 python -m pytest）

```bash
.venv/bin/python -m pytest -q docs/audit_c1_evidence_2026_09_08.py docs/audit_optimization_news_2026_09_08.py docs/audit_optimization_retry_2026_09_08.py
# 30 passed（C1 7/7 含新增探针；A 12/12；B1 11/11）

.venv/bin/python -m pytest -q
# 1158 passed / 14 skipped / 52 subtests（tests+docs 全量）
```

C2 测试 `tests/test_c2_thesis_card.py` 22 项：代码字段不臆造、悬空/未来/冲突引用、多空并存不冲突、assessable/insufficient/unknown/limited 推导、自由文本卡、来源失败计缺口、不同期限独立卡、失效条件可观察性、uncalibrated 恒定、状态行幂等且与卡一致、PM 节点单调用产出文本+卡（断言结构化 1 次/普通 0 次）、自由文本路径状态行、_log_state 保存与旧报告 null、Markdown 出口新旧两态、JSON 可序列化。

回归适配一处：`tests/test_memory_log.py::test_pm_falls_back_to_freetext_when_structured_unavailable` 原断言决策文本逐字相等——C2 起确定性状态行会追加（模型文本本身不变），改为 startswith + 状态行恰一次 + 卡 structured_output=False（意图保持：回退不阻塞、正文保留）。

## 文件清单（SHA-256）

```
db81d8976907c7fbfd3ca140b14d854269783605bcb3a72c142b20fb69aacbd2  tradingagents/agents/thesis.py
d25390ba0862896b4f679b684c6a47d51c992d4a673e2c880c5c23568618d97a  tradingagents/agents/schemas.py
aacd3e5e2ea87043f18c81d56d8c9bbd120809a473ef4b7c41a5d6f6c8fc7d22  tradingagents/agents/managers/portfolio_manager.py
20b04d2d8a6c8b4c900f2d612765cf23fac0e88db0db7786a2be54da5aa056e0  tradingagents/agents/utils/agent_states.py
686977cbcb75d0d244cfc68672925bca661a778adec055fc3c3dd9a41a6d0cc9  tradingagents/graph/trading_graph.py
e8549c0c8430287ac6d8fe7b7254a610da2d6f426021a18eec5b9387e49914de  tradingagents/evidence/prompt_context.py
6f3f7104e291368c9f9be66041b9842d8261accd9f31c9b929e84a09e75b464f  tradingagents/evidence/display.py
1f169e3a964a93985a7ac86dd622889d0306b26c6bae78493779ea5c5a2f8574  web/pdf_export.py
e3f032589487a93b4b61bbb5038773a880cb18d4cecbd8565b5795c07e88fe69  web/components/report_viewer.py
8bf40e411ef94b33870ebd37f1bededeba7408e8f211d881a4de13cb69d953fb  tests/test_c2_thesis_card.py
f41da0ee9bf120b0181e8ac18261e7926bcdc56e33840fb282538d5d7fba1657  tests/test_memory_log.py
```

（C1 handoff `docs/ZCODE_C1_HANDOFF_2026-09-08.md` 中 prompt_context/display/trading_graph 的 hash 已被本批更新，以本文件清单为准。）

## 边界与未做

- 假设卡不构成投资准确率证明；信心未校准；无样本外校准不显示胜率（全部出口文案明示）。
- 失效条件仅生成与展示，不订阅数据、不自动判断触发（待人工判断项明确标注）。
- 反思/记忆消费 thesis_card（复盘含当时 thesis）属 F 批，未做；评估系统未消费。
- D（可复算财务面板）/E（有界反证补查）/F（记忆与评测增强）未开始，等 Codex 对 A/B1/C1/C2 首批的可演示版本确认后按分批契约继续。
