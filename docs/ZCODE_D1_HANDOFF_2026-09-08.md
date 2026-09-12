# D1 可复算财务面板交接（ZCode → Codex）

日期：2026-09-08。契约：`docs/D_FINANCIAL_PANEL_CONTRACT_2026-09-08.md`（草案已按文末 Codex 23:28 修正逐条调和，冲突条款以修正为准）。范围：**D1 = 确定性计算 + 离线 CLI + 报告出口**；D2（显式输入进入研究流程）按修正 #10 另行给入口方案后单独交接。D1 未接任何 LLM/工具/网络，图内无人写面板 → 现有运行行为零改动。

## 交付物

**核心模块** `tradingagents/dataflows/financial_panel.py`（纯 stdlib）：

- **闭合注册表**：单位（金额三种 + `CNY:yuan_per_share` 每股专用 + `ratio:x` 无量纲倍数 + 比率存储 `ratio:fraction`）；币种 D1 仅 CNY，非 CNY 参与公式 → `unknown/unsupported_currency`；科目词表冻结。
- **manifest 校验**（`ManifestError` 全量确定性错误清单）：value 必须有限数值（bool/NaN/Inf/数字串/null 拒绝；**未列报≠0**）；同 (metric,scope,period) 重复拒绝；**D1 拒绝重述链**（restated/supersedes 必须空——声明不支持历史版本选择，非会计判错）；provenance 恰好 evidence_id 或 offline_ref 之一（字符串出处，绝不据此读文件/联网）；EPS 不得用金额单位；snapshot/flow 期间契约（window/财年/起止日/长度互相校验，TTM 必须恰好 12 个月）；`manifest_digest` 对规范输入内容计算并校验调用方声称值。
- **披露时点**：date 或带 offset 的完整 datetime 严格解析（Z 归一、naive datetime 拒绝、绝不截前十位）；**先排除 future/unknown-disclosure 输入再计算**；分析时点是可信调用参数（manifest 不可覆盖，header 不一致拒绝）；截止 = 上海日终。
- **可知时点 = 参与输入披露时间的 max**（全部依赖披露后才可知），保留精度标记（date/datetime）。
- **公式**（Decimal 计算，分类精度舍入：金额/每股 2dp、比率 6dp，渲染仅缩放一次 0.1523→15.23%）：
  - `yoy_growth`：同窗口跨一年；基数零/负 → not_applicable + 原始值入 limitations。
  - `margin_<利润科目>`：分子口径入名与标签；负利润 ok + 限制注记；收入零/负 → not_applicable。
  - `ocf_to_net_profit_total`（优先，同合并范围）/ `ocf_to_net_profit_attributable`（归母回退，**独立输出名+限制注记**，不静默互换）；净利零/负 → not_applicable + 原始值。
  - `net_cash_limited_scope`：现金与四项有息债务**全部必需**（缺任一 unknown，任何 null 不变 0）；限定口径注记（不涵盖租赁负债等；货币资金≠自由现金）；负值=净债务（限定口径）+ 符号说明。
  - `scenario_valuation_pe`：仅显式 eps_ttm×三档倍数；全缺 not_requested（"未请求"不计失败）、部分缺 unknown、档序倒置/非正倍数拒绝（不重排标签）；eps≤0 → not_applicable；假设标 user_provided，形成日必须 as-of；输出每股 CNY。
- **状态闭合**：ok/unknown(+reason_code)/not_applicable/not_requested；`overall_status` 由实际请求结果决定；指数（可信 instrument_type）整卡 not_applicable。
- **可复算**：每个 ok 值携带 `derivation{rule, formula_version, input_ids, inputs[]内联规范化值}`；输出 JSON 内嵌完整输入表 + digest——无悬空 ID。

**集成**：`AgentState.financial_panel`（Optional[dict]，图内无人写）+ `_log_state` 落盘（旧 JSON → null=未记录）+ Web expander「🧮 财务面板（可复算）」+ Markdown/PDF section（`render_financial_panel_md` 三出口同一渲染，旧报告显示未记录）。

**CLI（真实可运行）**：`python -m tradingagents.dataflows.financial_panel --manifest --instrument --instrument-type --analysis-date --output-dir` → 写 `financial_panel.json`+`financial_panel.md`（输入表/计算表/情景假设全量）；manifest 拒绝 → 退出码 2 + stderr 错误清单；unknown/not_applicable 正常导出；无 LLM/网络。

**Fixtures**（6 份，`tests/fixtures/financial_panel/`）：normal（全要素）、gap_missing_debt（缺第四项债务）、future_disclosure（as-of 排除）、index、scenario_partial、scenario_inverted。

## 验证（pytest 定向，未跑全量）

```bash
.venv/bin/python -m pytest tests/test_financial_panel.py
# 47 passed（含真实 CLI 子进程 e2e 与退出码；四率=固定分母 29、unknown/not_applicable 分层，
# 本 fixture 集数值错误率 0、口径缺失率 0、可追溯率 1.0——合成契约结果，非投资效果声明）

.venv/bin/python -m pytest -q docs/audit_c2_thesis_2026_09_08.py docs/audit_c1_evidence_2026_09_08.py docs/audit_optimization_news_2026_09_08.py docs/audit_optimization_retry_2026_09_08.py
# 38 passed（审计无回退）

.venv/bin/python -m pytest tests/test_financial_panel.py tests/test_memory_log.py tests/test_index_support.py tests/test_report_and_history_contract.py tests/test_pdf_export.py tests/test_c2_thesis_card.py tests/test_c1_evidence_graph.py tests/test_evidence_ledger.py
# 342 passed（触达模块定向回归）
```

D1 最小验收补充逐条覆盖：null 债券拒绝且缺项 unknown；不同披露日取 max（10-29/10-30/10-31/11-01 分层断言）；unknown/future 披露无 ok；假设形成日晚于截止 → 情景不计算；EPS 金额单位拒绝；0.1523→15.23%；非有限/bool 拒绝；digest 不匹配拒绝；重述链/重复 slot 拒绝；标的不一致拒绝；情景部分缺与档序倒置；CLI 到 JSON+MD 且输入出处逐条可追溯。

## 文件清单（SHA-256）

```
306e467ab4a74f88cfc2662e10e66a420fa879bcf66dee10afd2f1a5682f5d00  tradingagents/dataflows/financial_panel.py
b1ccbc58fd937e420ebab7329f0dc6cb4f6ce3ef0a6939e44978d73587cf2d38  tradingagents/agents/utils/agent_states.py
e658cc1fd54ef4381f69641c563868cc7ceb38e1816180c72e915979c3f6fc99  tradingagents/graph/trading_graph.py
8f5b744d90e247e757406235caca105bb1a0649913eadfc34e6c4059e821a643  web/pdf_export.py
cbd735c818f334798f3f0b3020892cb179f0f360822832542d36a69315b8cc92  web/components/report_viewer.py
970cc06077e5007ad67dd3bf741e318b16cbd36f830a50b7b369b5251d67b2f4  tests/test_financial_panel.py
90d976cd43cee52865bff2e9ca457bbe0e5c8dee5b743e40b0b9adeff95231a3  docs/D_FINANCIAL_PANEL_CONTRACT_2026-09-08.md
6558693e7f05152b023bbe4b07561293a93e1e007a6055afe0ad3d78d41332df  tests/fixtures/financial_panel/index.json
0cf25d50a37bce8f86fcea2373517b3ce800b5e3936f7583f0d2df547f2fe4c6  tests/fixtures/financial_panel/stock_future_disclosure.json
b5d57d939daf0537caad3955d036cb8e0957a088a5ba90c54c6e768933d98e1b  tests/fixtures/financial_panel/stock_gap_missing_debt.json
621121289b0204fadf0fb95cc55c5619da56d6e730f0832251442f72ab2cde2f  tests/fixtures/financial_panel/stock_normal.json
6f31ed626368c66828d1b63151299b65c15fb010f620ea7a6921715a5e6aae28  tests/fixtures/financial_panel/stock_scenario_inverted.json
093bb1d773646c63853e56853df3afb090506c46f416411489860d6da6e1ff0f  tests/fixtures/financial_panel/stock_scenario_partial.json
```

## 边界（如实声明）

- D1 未接入任何真实 vendor 抓取（字段映射另立契约）；未做 DCF/股本路径/FX；不做单季/TTM 跨期推导（会计上兼容但 D1 不支持，未判错）；拒绝重述链（不支持历史版本选择）。
- 图内默认无人写 `financial_panel`——报告出口就绪但正常运行不注入；D1 **未影响任何模型决策**（D2 才接 RM/Trader/PM 引用，届时先给入口方案过审）。
- 四率是合成契约指标（固定分母、分层 unknown/not_applicable），不构成投资准确率或预测效果声明。

---

## R1 修正附录（2026-09-08 深夜，对应 docs/D1_FINANCIAL_REVIEW_2026-09-08.md）

Codex 早期独立审核 7 failed / 1 passed → 修复后 **8/8 通过**（独立脚本未改动）。修正内容：

1. **结果身份 = metric + scope + 完整期间**（新增 `_result_key`/`_rule_key`；`_period_label` 对 TTM 携带起止日）：同年合并/母公司两口径**并存**（margin 各自 0.2/0.1），manifest 顺序反转结果与 digest 均不变；YoY/margin/OCF/净现金全部换用含 scope 的键。
2. **TTM 完整月窗口**：起日必须月初、止日必须月末、恰好 12 个自然月（`_is_complete_month_window`，闰年由月份算术处理）；fiscal_year 必须与 period_end 年份一致；不凭"涉及十二个月份"断言覆盖一年。
3. **EPS(TTM) 期间强约束**：kind 必须 ttm——其他科目的合法季度窗口不自动适用。
4. **存量 kind 矛盾拒绝**：snapshot 科目声明非 snapshot kind 直接拒绝，不再静默归一化改写口径。
5. **混合精度不造时间**：`_fmt_available` 按保守日终比较取最晚，但输出保留**胜者自身精度**——最晚依赖仅日期精度时结果保持 date（不发明凌晨时间戳）；最晚为 datetime 时不降级。
6. **零/负基数 YoY**：not_applicable + 原始值入 limitations（修复 `prior_slot` NameError）。
7. **情景组合歧义拒绝**：可用 EPS 多组（不同期间/口径）、任一档倍数多组、或四项 scope 不一致 → `unknown/ambiguous_assumptions`（列出全部 input_id）——不再"第一个 EPS / 最后一档倍数"隐式胜出。附带修复 `find(scope=None)` 匹配全部的语义。

验证（定向）：`tests/test_financial_panel.py` **60 passed**（新增 `TestD1R1Contracts` 14 项镜像契约 + 双口径/双 TTM/混 scope 拒绝/精度双向/零负基数/输入不变性）；五份 Codex 审计 **46 passed**（D1 8/8、C2 8/8、C1 7/7、A 12/12、B1 11/11）；CLI 复测通过；触达模块定向回归 184 passed。未跑全量。

### R1 后文件清单（SHA-256，以此为准）

```
75af79932cdc58ab81beea0c3790fe58d99d8cd4e205880f93ed49b94bda719d  tradingagents/dataflows/financial_panel.py
50d50d52defaba96aebaae126a50dbf4f5f27d773037cf9d66509f2a25560daa  tests/test_financial_panel.py
```

---

## R2 修正附录（对应 docs/D1_FINANCIAL_REVIEW_2026-09-08.md R2，23:57）

修正后 Codex 独立审计 **11/11**（8 旧 + 3 新，脚本未改动）。修正内容（`_compute_yoy` 重写）：

1. **窗口身份分组**：YoY 分组键加入起止**月日**（`kind+window+start_md+end_md`）——TTM 2024-09-30 只与 2023-09-30 配对；"fiscal_year 恰差一年"本身不再构成可比性（TTM vs 日历年在同键外，不再错配）。
2. **同族多窗口独立保留**：9 月末与 12 月末两组 TTM 各自成组、各自配对、各自产出结果键（含完整起止日）——无 first/max 隐式选择；manifest 顺序反转 metrics 与 digest 均不变。
3. **as-of 过滤先于年度选择**：当期年取**可用输入的最大年**——未披露的 FY2025 不再淘汰可算的 FY2024（更晚年度未披露输入在 reason 中单列"已排除、不影响当时可算结果"）；家族内全部不可得时，以最新**声明**成员为确定性代表报告排除原因（不误报 missing）。
4. 前期（t−1 年）候选回到"声明集合内逐个可用性判定"——不可用原因（future/unknown/币种）如实呈现，不静默吞掉。

验证（定向，未跑全量）：面板测试 **64 passed**（新增 `TestD1R2YoySelection` 4 项：TTM 年差不构成可比、9/12 月末窗口独立且顺序不变、future 年度不挤掉可算结果）；五份 Codex 审计 **49 passed**（D1 11/11、C2 8/8、C1 7/7、A 12/12、B1 11/11）；CLI 复测通过（stock_normal partial，同 R1 附录数值）。一处旧测试按修正后语义更新（未知披露的当期输入不可被选为当期——as-of 语义下家族最新可知窗口为其前一年，该输入不得出现在任何 ok 依赖链）。

### R2 后文件清单（SHA-256，以此为准）

```
949eb7dcb07baa3bb75b327ef0920b390e11df5c87f4501f2b9f7c65c5927b77  tradingagents/dataflows/financial_panel.py
6ce533f717e9361989e4e0b9d6a9c9aa2aa4b7379f823b7995e88aab7bdc35a1  tests/test_financial_panel.py
```

### R2 后补充（09-09 凌晨）：测试文件 E731 风格修复

`tests/test_financial_panel.py` 的 `snap = lambda: …` 改为 `def snap(): …`（ruff E731，仅风格，断言与语义零变化；64 项测试全过、五审计 49/49 保持）。新 hash：

```
c778bed40dabfacbd5be072b9629ee4f4a7d87ac095f33a83a46b7cc45b9575f  tests/test_financial_panel.py
```

其余 D1 文件（含 `financial_panel.py` 949eb7dc…）未动。
