# D 可复算财务面板：实施契约（ZCode 起草，待 Codex 审核）

日期：2026-09-08。上游任务书：`docs/DECISION_ACCURACY_PLAN_2026-09-08.md` D 节。
状态：**D1 编码中**。草案已按文末 Codex 23:28 审核修正完成调和（冲突条款以修正为准）；D1 = 计算与离线报告出口，D2（决策流程接入）另行交接。

## 0. 目标与非目标

**目标**：用确定性纯函数 + 显式输入清单（manifest）实现五类计算——同比增长、利润率、经营现金流/净利润、净现金/净债务、情景估值区间——每个输出数值携带单位、币种、合并口径、报告期间、披露时间与 evidence_id（或显式离线输入引用），可从最终报告逐值回溯到输入。

**非目标（v1 明确不做）**：

- 不接真实抓取：输入只来自离线 fixture / 用户显式提供的 manifest JSON。真实 vendor 字段映射（`a_stock.get_balance_sheet/get_cashflow/get_income_statement` 等）在 C1 artifact 模式稳定后单独契约界定。
- 不做 DCF（现金流/折现率/资本结构输入不可靠前单列后续）。
- 不默认任何估值倍数、不构造债务/现金缺项。
- 指数不套个股财报公式（整个面板 not_applicable，见 §7）。
- 不用事后修订的财报做历史回测（见 §10 披露时点规则）。
- 无任何 LLM 调用；LLM 不得在报告文案中重算/覆盖面板数值（见 §8）。

## 1. 输入清单（manifest）契约

一个面板计算只接受一份 manifest。每个输入项（`FinancialInput`）必须完整声明，缺一即该项不可用（不推断、不默认）：

```json
{
  "input_id": "inp-001",              // manifest 内唯一，供追溯与 derivation 引用
  "metric": "revenue",                // 受控词表（§2.3），不允许自由文本
  "value": 12345000000.0,             // 数值（float），绝不携带在字符串里
  "unit": "CNY:yuan",                 // §2.1 注册表内的规范单位
  "currency": "CNY",                  // §2.2 注册表内币种
  "scope": "consolidated",            // consolidated | parent_only，禁止混用比较
  "period": {                         // §2.4 期间分类学
    "kind": "cumulative",             // cumulative | single_quarter | ttm | annual_snapshot
    "window": "Q3",                   // Q1 | H1 | Q3 | FY | Q1s|Q2s|Q3s|Q4s（单季）| TTM
    "fiscal_year": 2024,
    "period_end": "2024-09-30"
  },
  "disclosed_at": "2024-10-29",       // 披露日期（YYYY-MM-DD）；未知必须显式 "unknown"
  "restated": false,                  // 是否重述/修订版
  "supersedes": null,                 // 被本项取代的 input_id（重述链）
  "provenance": {                     // 恰好一个键（二选一）
    "offline_ref": "fixture:q3_2024_report.json#revenue"
  }
}
```

**规则**：

- **任何 null/未列报都不能变 0（Codex 修正）**：`value` 必须是有限数值（bool/NaN/Infinity/数字字符串一律拒绝）；"未提供该输入"与"0"是两回事——显式数值 0 且出处齐备才是 0。§5.4 四项定义内债务全部必需，缺任一 → unknown。
- `disclosed_at` 接受 `YYYY-MM-DD`（日期精度）或**带 offset 的完整 datetime**（严格解析、归一化后比较，绝不截取前十位）；缺失或显式 `"unknown"` 的输入**不能参与任何 ok 数值**（公式按未知披露排除）。
- 同一 `(metric, scope, period)` 出现多个输入 → manifest 无效，整体拒绝（确定性报错，不取最新/最大）。
- **D1 只接单一显式版本**：`restated` 必须为 false、`supersedes` 必须为 null；任何重述链（含未知父节点/自环/环）直接拒绝 manifest——D1 不声称支持历史版本选择（缩减已在 CLI 与本契约声明，Codex 修正 #2 允许）。
- manifest 顶层带 `manifest_digest`（对排序后规范 JSON 的 SHA-256），落盘进卡片，保证可复算同一结果。

## 2. 单位、币种、词表、期间

**2.1 单位注册表**（封闭集，换算因子显式内置，禁止隐式换算）：
金额 `CNY:yuan=1`、`CNY:wanyuan=1e4`、`CNY:yi_yuan=1e8`；每股 `CNY:yuan_per_share`（EPS 与情景估值输出专用，金额类科目不得使用）；倍数 `ratio:x`（无量纲，PE 专用）；比率存储 `ratio:fraction`（如 0.1523，渲染为 15.23%——**渲染只缩放一次**，存储单位写明）。换算仅发生在注册表内。

**2.2 币种（D1 闭合为 CNY）**：D1 仅支持 `CNY`；任何非 CNY 输入参与的公式 → `unknown`（reason_code=`unsupported_currency`），不做 FX 换算、无 HKD/USD 单位与 schema。若以后加 FX，另给方向/期间/披露时点和证据的独立契约。

**2.3 科目词表（受控）**：`revenue`、`net_profit`（归母净利润）、`net_profit_total`（含少数股东）、`ocf`（经营活动现金流净额）、`cash`（货币资金）、`trading_financial_assets`（交易性金融资产，仅当披露才可用）、`short_term_borrowing`、`long_term_borrowing`、`bonds_payable`、`non_current_debt_due_within_1y`（一年内到期非流动负债中有息部分）、`eps_ttm`、`fx_rate`。v1 词表冻结；新增科目需改本契约并重新过审。

**2.4 期间契约（统一，Codex 修正 #3）**：**存量科目**（cash/借款/债券/到期债务）用 `kind="snapshot"` + `period_end`（时点值，不混期间语义）；**流量科目**（revenue/净利/OCF/EPS）用 `kind ∈ {annual, cumulative, single_quarter, ttm}` + `window` + `fiscal_year` + `period_start` + `period_end`，四者与起止日长度语义一致（H1=6 个月、Q3=9 个月、FY=12 个月；单季为该财年对应季度；TTM 必须显式声明并校验恰好 12 个月——不由字段名自动证明完整）。A股财年按日历年。同比同窗口跨一年；利润率/现金流比同起止期；存量同日。

**兼容性总则**：两个输入"可兼容"当且仅当 `scope`、`currency` 相同（或提供 fx_rate）、单位可经注册表换算、`period.kind` 与 `period.window` 语义匹配（各公式的具体匹配规则见 §5），且二者都非 `null` 值（除非公式显式允许）。

## 3. 输出卡片 schema（`FinancialPanelCard`）

```json
{
  "schema_version": 1,
  "instrument": "600519",
  "instrument_type": "stock",
  "analysis_date": "2026-01-15",
  "run_id": "...",
  "manifest_digest": "sha256:...",
  "metrics": { "<metric_key>": <ComputedValue> },
  "scenarios": { ... },               // §5.5；无情景输入 → {"status": "not_requested"}
  "limitations": ["..."],
  "overall_status": "computed | partial | not_applicable | unknown"
}
```

每个 `ComputedValue`：

```json
{
  "status": "ok | unknown | not_applicable | not_requested",
  "reason_code": "",                  // 结构化原因（unknown/not_applicable/not_requested 必填）
  "reason": "",                       // 人读说明
  "value": 0.1523,                    // 仅 ok 时存在；Decimal 计算后按类别精度序列化
  "unit": "ratio:fraction",           // 存储单位（渲染只缩放一次）
  "currency": "CNY",
  "scope": "consolidated",
  "period": { ... },                  // 结果归属期间
  "derivation": {                     // 公式依赖链（Codex 修正 #7：可复算≠derivation 为 null）
    "rule": "yoy_growth", "formula_version": "d1",
    "input_ids": ["inp-001", "inp-002"],
    "inputs": [ {input_id, metric, normalized_value, unit, ...} ]  // 规范化输入值随结果内联
  },
  "available_date": "2024-10-29",     // 参与输入披露时点的 **最大值**（全部依赖已披露后才可知）
  "available_date_precision": "date", // date | datetime
  "provenance_refs": ["fixture:..."]  // 各输入的 provenance 汇总（字符串出处，不据此读文件/联网）
}
```

**状态语义（固定）**：

- `ok`：全部输入满足，公式算出。
- `unknown`：缺输入/期间不匹配/缺汇率/披露时间未知导致**不可验证**——是"我们算不出"，可随输入补齐转 ok。
- `not_applicable`：**该公式对该对象在构造上不适用**（指数之于全部公式；负净利润之于 OCF/净利润比与 PE 情景；分母恒零）。不适用不会被补输入修复。
- 任何情况不静默补零、不默认。

## 4. 通用禁止项（全公式）

- **不同报告期不得直接相减冒充季度值**：`cumulative Q3 − cumulative H1` 之类跨期相减在 v1 一律禁止；`single_quarter`/`ttm` 只接受 (a) 直接披露的输入，或 (b) §5.6 声明的唯一例外推导（v1 默认**无**例外；若 Codex 批准"同年相邻累计相减"再入契约）。
- TTM 必须有完整可兼容输入：直接披露的 `ttm` 输入，或（v1 禁止）拼接。缺任一环节 → `unknown`。
- 负分母、零分母 → `not_applicable`（比率类），同时把分子分母原始值列入 limitations 供人工判断。
- 重述链：若存在 `restated: true` 且 `supersedes` 指向另一输入，只有链末端（最新重述）参与计算；计算结果 derivation 记录所用版本并在 limitations 标注"存在重述版本"。
- 未披露数据 → `unknown`；绝不用行业均值/历史值/0 填充。

## 5. 公式规格

### 5.1 同比增长 `yoy_growth`（逐科目）

- **输入**：`X(t)` 与 `X(t−1y)`——同 metric、同 scope、同币种（或 fx）、`period.kind` 与 `window` **完全相同**、`fiscal_year` 恰差 1。
- **公式**：`(X_t / X_{t-1}) − 1`，输出 `ratio:percent`。
- **unknown**：任一输入缺失；期间不匹配（如 Q3 累计 vs H1 累计、累计 vs 单季）；币种不同且无 fx；任一输入 `value: null`。
- **not_applicable**：上年基数为负或零（增长率无经济含义）——分母原始值入 limitations。
- **禁止**：任何跨期相减；用快报/预告 mixed 正式报告（输入 `restated`/来源不声明正式报告 → 按 unknown）。

### 5.2 利润率 `margin_<base>`

- **输入**：分子（`net_profit` 等）与 `revenue`，同一 `period`（kind/window/fiscal_year 全同）、同 scope、同币种。
- **公式**：分子 / revenue，输出 `ratio:percent`。
- **unknown**：缺任一输入；期间不一致；币种无 fx。
- **not_applicable**：revenue ≤ 0；负利润时仍可计算但**必须**在 limitations 标"负利润下的利润率仅作记录，不构成质量判断"（不隐藏、不判废）。

### 5.3 经营现金流 / 净利润 `ocf_to_net_profit`

- **输入**：`ocf`、`net_profit`，同期间/口径/币种。
- **公式**：ocf / net_profit。
- **unknown**：缺输入/期间不匹配。
- **not_applicable**：net_profit ≤ 0（任务书明示负利润 → 不适用）；两原始值列入 limitations。

### 5.4 净现金 / 净债务 `net_cash` / `net_debt`

- **口径（限定范围，写入卡片 limitations，Codex 修正 #4）**：输出名为 `net_cash_limited_scope` = `cash − (short_term_borrowing + long_term_borrowing + bonds_payable + non_current_debt_due_within_1y)`。明确**不**声称涵盖租赁负债、应付票据等全部债务；货币资金也不称为可自由支配现金。允许为负（即净债务，限定口径），符号含义在输出与渲染中解释。
- **输入**：`cash` 与**四项**有息负债科目全部必需——缺任一 → `unknown`（**任何 null/未列报都不变 0**）。
- **unknown**：任一科目缺失；任一科目披露时点未知/晚于分析时点；币种非 CNY。
- **period**：这是 `annual_snapshot | report_snapshot` 类时点值——所有输入必须同一 `period_end`（同季末），跨 period_end → unknown。
- **不造债务**：绝不从"行业典型杠杆"或资产规模推断负债。

### 5.5 情景估值区间 `scenario_valuation`

- **输入（全部必须显式给出，缺任一 → 整块 `not_requested` 或 `unknown`，绝不默认）**：
  - 盈利基数：`eps_ttm`（或 manifest 明确的盈利总额 + 股本，v1 只做 eps 路径）；
  - 三档倍数：`multiple_bear / multiple_base / multiple_bull`（`ratio:x`，>0）；
  - 可选 `fx_rate`（盈利币种 ≠ 报价币种时必需）。
- **公式**：`low = min(multiples) × eps`、`high = max(multiples) × eps`、`base = base_multiple × eps`；输出 `CNY:yuan`（每股）。
- **假设全量披露**：卡片 `scenarios` 内逐档列出所用 eps/倍数/汇率及其 input_id 与 provenance；`sensitivity` 表 = 每档倍数 ±（仅当 manifest 显式给出扰动档，缺省不自动造敏感性）。
- **not_applicable**：eps ≤ 0（亏损期 PE 情景无意义）；instrument_type = index。
- **不默认倍数**：四项（eps + 三档）全无 → `not_requested`；只给一部分 → `unknown`；齐全且 bear≤base≤bull、全部有限正数才计算——顺序不合规/非正倍数 → 拒绝计算（`unknown` + reason，不用 min/max 偷换档位标签）。假设输入的"形成日"（其 disclosed_at）必须不晚于分析时点，且标为**用户给定假设**，绝不称为财报事实。
- **明确声明**：区间是给定假设下的算术映射，不是预测；文案模板固定写入"区间更精细不等于预测更准"。

### 5.6 推导（D1：无跨期推导，但依赖链必填）

不做任何跨期相减/拼接（单季/TTM 只接显式输入；"相邻累计相减"会计上兼容但 D1 不支持，不判其为错误）。但**每个 ok 值必须携带完整公式依赖链**（rule/formula_version/input_ids + 规范化输入值与单位）；输出 JSON 内嵌完整规范输入表——不能只有指向输出中不存在输入的 ID。

## 6. 指数路径（not_applicable）

- `instrument_type == "index"` → 卡片 `overall_status: "not_applicable"`，`metrics` 各项 `not_applicable`（reason: "指数无个股财务报表"），`scenarios: not_applicable`（reason: "指数不套个股倍数；指数估值另立契约"）。
- 报告渲染（§8）：指数报告显示固定一行"财务面板：不适用（指数无个股财报，未套用个股公式）"——**不显示空表、不显示 0、不显示任何倍数**。
- 指数自身估值（PE-band 等）不在本契约，防混入个股公式。

## 7. 个股报告集成（真实出口）

- **计算时机**：v1 纯离线——`compute_financial_panel(manifest, context) -> FinancialPanelCard` 为纯函数；不进图、不加 LLM 调用、不加工具。生产接入路径：`AgentState.financial_panel`（Optional[dict]，仅显式注入方写入；图内无人写 → 兼容旧行为）。
- **注入方式（v1）**：`prepare_graph_run` 前由调用方（CLI/Web 的离线 fixture 入口，独立小命令或参数）读 manifest → 纯函数计算 → 随 `initial_state` 注入。**默认不注入**（不改变现有任何运行行为）。
- **落盘**：`_log_state` 增加 `financial_panel` 键（旧 JSON 无此键 → null = 未记录，加载端不得凭空生成——与 data_quality/evidence_bundle/thesis_card 同模式）。
- **三出口共用渲染**：`render_financial_panel_md(card)`（新模块 `tradingagents/dataflows/financial_panel.py` 内）→ Web expander + Markdown/PDF section「财务面板（可复算）」。每个数值行内联显示：值+单位、期间、同比/口径标注、`input_id`/`evidence_id` 追溯串；`unknown/not_applicable` 逐项显示 reason，绝不显示 "0" 或空白占位。
- **LLM 不得覆盖**：面板 section 由代码确定性渲染，不经过任何模型；分析师/PM 的提示词不注入面板数值（v1），防止模型在文案里"重新心算"与面板冲突；最终决策引用数值必须引用面板展示值（后续 E 批再接引用校验）。

## 8. 效果评估指标（离线 fixture 先行）

以带标准答案的 fixture 集（正常/缺口/不适用/重述/跨币种/指数）度量四率，写入测试报告（非投资准确率声明）：

- **数值错误率** = 计算值 ≠ 期望值（相对误差 > 1e-9）的 (指标×用例) 占比；
- **口径缺失率** = 输出缺 unit/currency/scope/period/disclosed_at/provenance 任一字段占比（目标 0）；
- **无法计算率** = unknown+not_applicable 占比（**按原因分层报告**：缺输入 vs 不匹配 vs 构造不适用；不把"如实不适用"当缺陷，也不把"缺数据"藏进不适用）；
- **可追溯率** = ok 值可经 input_id/evidence_id 回溯到 manifest 项的占比（目标 100%）。

## 9. 测试矩阵（验收前置）

1. 每公式 happy path（含单位换算：亿元输入 → 元输出）。
2. 期间不匹配（Q3 vs H1、单季 vs 累计、FY vs TTM）→ unknown + reason。
3. 零/负分母 → not_applicable + 原始值入 limitations。
4. 跨币种无 fx → unknown；有 fx → 换算并记录汇率 input_id。
5. 禁止项红线：构造"Q3 累计 − H1 累计"输入组合 → 面板不存在任何 single_quarter 推导输出（schema 断言 derivation 恒 null）。
6. 重述链：旧值被 restated 取代 → 用链末值 + limitation 标注。
7. disclosed_at unknown 传染与 §10 时点校验。
8. 指数 manifest → 整卡 not_applicable；渲染输出固定声明行、无数字。
9. 情景：无倍数 → not_requested（无区间）；负 eps → not_applicable；三档齐全 → 区间+假设全量列出+固定免责行。
10. manifest 冲突（重复科目期间）→ 整体拒绝（异常，非静默）。
11. `_log_state` 新旧 JSON（有/无 financial_panel）；三出口渲染新旧两态。
12. 纯函数性：同 manifest 两次计算结果逐字节一致（含 manifest_digest）。
13. 指数/个股注入开关：默认不注入 → 现有全部行为不变（回归零改动证明）。
14. **D1 补充验收（Codex 23:28）**：null 债券不能零债务；第四项债务缺失 → unknown；两个输入不同披露日取 **max**；未知/future 披露无 ok 数值；假设形成日在 cutoff 之后 → 情景不计算；EPS 与金额单位混用被拒；0.1523 渲染 15.23%；非有限数/bool 被拒；manifest digest 不匹配被拒；重述链/重复 input_id/循环 → manifest 拒绝（D1 声明不支持版本选择）；manifest 与可信标的/类型不一致被拒；情景部分缺失与档位倒置；**真实 CLI**（子进程）产出 JSON+Markdown 且输入出处逐条可追溯；退出码区分 manifest 拒绝。

## 10. 披露时点规则（防未来函数，承接 R4/C1 精神）

- 每个输入的 `disclosed_at` 参与校验：`disclosed_at > analysis_date`（面板归属的分析时点）→ 该输入**不得**参与计算（视同缺失，reason 注明"披露晚于分析时点"）。
- 全部输入 `disclosed_at` 未知 → 卡片可生成但 `overall_status: partial`，limitations 注明"披露时点未验证，不可用于历史回测"。
- 因此，事后修订/重述财报天然无法进入历史分析（其 disclosed_at 晚于当时分析点），满足"不能以事后修订财报做历史回测"。

## 11. 实施顺序（获批后）

1. `tradingagents/dataflows/financial_panel.py`：注册表 + manifest 校验 + 五公式纯函数 + 卡片构建 + `render_financial_panel_md`（全部 stdlib，无网络无 LLM）。
2. `AgentState.financial_panel` 字段 + `_log_state` 落盘（默认 absent 不改现有行为）。
3. Web/MD/PDF 出口（与质量卡/证据/假设卡并列 expander/section）。
4. `tests/test_financial_panel.py`（§9 矩阵）+ fixture 集（含指数与四率评估脚本）。
5. **离线 CLI**（`python -m tradingagents.dataflows.financial_panel --manifest --instrument --instrument-type --analysis-date --output-dir`）：临时目录产出含输入表/计算表/情景假设的 JSON+Markdown；manifest 拒绝非零退出码，unknown 结果正常导出；不启动 LLM/市场请求。Web 上传页不强制。
6. 真实 vendor 字段映射 → 单独后续契约（C1 artifact 模式成熟后），不在本批。

## 12. 与既有批次的关系

- 复用 C1 证据账本的 `evidence_id` 语义（provenance 一类）；复用 N01/C1/C2 的"旧报告未记录不凭空生成"渲染约定与 schema_version 模式。
- 面板不改变 quality gate、thesis card、证据账本的任何现有语义；`assessment_status`（C2）v1 不消费面板（后续 E/F 再议）。
- 全程无新增 LLM 调用、无新增工具、无网络访问；测试全部离线临时目录。

## 待 Codex 决策点（编码前需确认）

1. §5.6 推导例外 v1 置空（含"同年相邻累计相减得单季"亦禁止）是否维持。
2. `bonds_payable` 是否维持"唯一允许 null→0 的科目"；`trading_financial_assets` 只在显式披露时计入净现金口径（当前 §5.4 未计入，是否加入）。
3. 情景估值的盈利基数 v1 仅 `eps_ttm` 路径（不做盈利总额+股本）是否接受。
4. 四率评估脚本是否随本批交付（建议随测试一起，作为 §9.14 用例）。

## Codex 审核修正与执行决定（23:28，优先于上述草案冲突条款）

以下是实施契约，ZCode 据此修改草案并编码；无需再次询问用户。D 按 D1 计算与离线报告、D2 显式输入进入研究流程分两次交接，保持可审核增量。先完成 D1，不把纯展示声称为已影响模型决策。

### 四个待决点已定

- D1 不派生单季/TTM，只接明确期间的已提供值；相邻累计相减并非永远错误，当前暂不支持，不能在说明里将会计上兼容的推导普遍判错。
- **任何 null/未列报都不能变 0**，包括 bonds_payable。显式数值 0 且出处齐备才是 0；四项定义内债务全必需，缺任一 unknown。交易性金融资产不自动计入 cash。
- D1 情景仅显式 eps_ttm × 显式三档 PE 倍数，暂不做股本路径、DCF、FX 换算。
- 提供离线 CLI、正常/缺口/历史/指数 fixture、期望值与覆盖报告；四率只是合成契约结果，必须用固定分母并分开 unknown 和 not_applicable，不称投资效果。

### 必须修正的语义

1. **有效日期取参与输入的最晚披露时点 max，不能取 min。** 结果只有在全部依赖已披露后才可知。分析时点是调用上下文可信参数，manifest 不能覆盖。D1 日期级按上海日终且标 date precision；如果接收 offset datetime，则严格解析归一化后比较，不能截取前十位。缺失/非法披露时间的输入不能产生 ok 数值；先排除 future/unknown 输入，再选择版本与计算。所有依赖（包括 EPS、情景假设的形成日）必须在截止时点内；假设标为用户给定，绝不称为财报事实。
2. **历史重述选择按 as-of**：版本通过明确 supersedes 链关联；先过滤不可得版本，再选当时可用链末端，未来修订不得淘汰当时原值。input_id 重复、未知父节点、自环/环、多条竞争链末端或跨科目/期间/口径指向都拒绝，不能最后一条胜出。D1 可拒绝重述链并只接单一显式版本作为更小实现，但若选择此缩减必须在 CLI/契约明确，不能声称支持历史版本选择。
3. **统一期间契约**：存量用 snapshot + period_end；流量用 annual/cumulative/single_quarter/ttm + period_start/end 与标准 window，验证年份、窗口、起止日和长度语义。不要使用未定义 report_snapshot。TTM 仅显式给值并声明完整 12 个月及口径，不由字段名自动证明完整。同比同窗口跨一年；利润率与现金流比要求同起止期；存量同日。
4. **现金与利润口径写准确**：D1 净现金是“四项显式有息债务减明确 cash 科目”的限定口径，名称/限制清楚，不声称涵盖租赁负债等所有债务，也不把货币资金全额叫可自由支配现金。合并 OCF 的对照优先使用同合并范围的 net_profit_total；归母利润另名，不能静默与总净利润互换。利润率明确选用哪种利润，词表与输出名一致。
5. **单位和币种必须闭合**：D1 可限制 CNY 并把跨币种公式列 unknown/unsupported_currency，不能一边声明支持 HKD/USD/FX，一边没有对应单位、metric 和 schema。若以后加 FX，另给方向/期间/披露时点和证据契约。本轮增加 eps 的 CNY/share 单位、PE 的 dimensionless multiple，估值输出 CNY/share；不能 EPS 用金额单位。ratio 原始值 0.1523 = 15.23%，渲染仅缩放一次，写明存储单位。
6. **所有数字有限且类型明确**：禁止 bool、NaN、Infinity、未声明数字字符串；支持正常 int/float 时用 Decimal(str(value)) 规范计算，输出采取明确 JSON 序列化/舍入约定，金额/每股/百分比各自精度，不用全局 float 容差掩盖单位错误。0 与 null 不同。schema 例子必须是可解析 JSON，provenance evidence_id/offline_ref 二选一，示例不能同时给两个。
7. **追溯是公式依赖链**：即使不做季度/TTM 推导，ratio/净债务/情景都需要 rule/formula_version、input_ids、规范化输入值与单位；不能把 derivation 恒 null 等同于可复算。digest 对规范输入内容（排除 digest 自身）计算并校验调用者所给值，排序规则明确；输出 JSON 必须带所用 manifest 或完整规范输入表，不能仅 ID 指到输出中不存在的输入。offline_ref 只作为字符串出处，不据此任意读文件/联网。
8. **状态闭合**：零分母/负 PE 盈利可 not_applicable，缺输入/未披露/币种不支持是 unknown。情景全没提供是 not_requested；只给一部分是 unknown；齐全且 bear≤base≤bull、有限正倍数才计算，顺序不合规拒绝，不能 min/max 偷换三档标签。敏感性只用显式扰动档。净现金允许负数但解释符号。overall_status 由实际请求指标的结果确定，不把“没请求”计为失败；指数由可信 instrument_type 判定，不猜代码。
9. **D1 真实离线入口与报告出口**：新增可运行的模块 CLI，显式 --manifest / --instrument / --instrument-type / --analysis-date / --output-dir，在临时目录示例生成含输入表、计算表和情景假设的 JSON+Markdown；退出码反映 manifest 拒绝，unknown 结果仍可正常导出。共用渲染接 Web/Markdown/PDF 及 _log_state，旧报告未记录。fixture 不启动 LLM 或市场请求。不强制为了 D1 添加 Web 上传页。manifest 与可信标的/类型不一致必须拒绝。
10. **D2 再做决策接入**：在 D1 稳定交接后，明确支持显式 manifest 进入正常运行准备路径、已有 RM/Trader/PM 提示词引用代码算出的表、保存与恢复不丢失/不串 run。不能以“不给模型看”声称已约束模型计算，也不能承诺提示词能阻止一切错误；代码表始终保留为复核依据。D2 不增加额外 LLM 调用；不做真实 vendor 抓取。D2 开始前给出具体入口方案并由 Codex 审核。

### D1 最小验收补充

除原矩阵中与上述一致条款，加入：null 债券不能零债务；第四项债务缺失；两个输入不同披露日取 max；未知/future 披露无 ok 数值；未来修订不覆盖当时值（若支持版本选择）；假设形成日在 cutoff 之后；EPS 与货币/每股单位混淆；0.1523 渲染 15.23%；非有限数/bool；manifest digest 不匹配；重复/循环版本；标的不匹配；情景部分缺失与档位倒置；真实 CLI 到 JSON+MD 且输入出处可追溯。不得只测内部纯函数。

依据：财务流量针对期间，存量针对时点，不能混用（[SEC 财务报表指南](https://www.sec.gov/about/reports-publications/beginners-guide-financial-statements)）。现金等价物有明确的高流动性和价值风险要求，不能直接把交易性金融资产全部纳入（[IFRS IAS 7 概述](https://www.ifrs.org/issued-standards/list-of-standards/ias-7-statement-of-cash-flows/)）。其余 max 披露时间、版本 as-of、schema/数值规则是本项目可复算与历史可得性工程契约，不宣称这些自定义指标为统一会计准则。
