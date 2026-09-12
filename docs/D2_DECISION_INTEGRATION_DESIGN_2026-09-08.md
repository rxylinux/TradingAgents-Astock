# D2 决策接入：入口与隔离设计（修订版 R1，待 Codex 审核后才编码）

日期：2026-09-08（深夜修订）。上游：`docs/D_FINANCIAL_PANEL_CONTRACT_2026-09-08.md` Codex 23:28 修正 #10、D1 已交付（`docs/ZCODE_D1_HANDOFF_2026-09-08.md` R2 后状态）。
状态：**仅设计，未编码**。D1 生产与测试保持稳定（本轮仅修复测试文件一处 E731 lambda 风格，断言未变）。

## 0. 范围与红线

**做**：显式 manifest 经**正常运行准备路径**（`prepare_graph_run`）进入运行；既有 RM/Trader/PM 提示词（个股与指数两条路径）引用**代码算出的**面板数值；面板随 state 保存并经 checkpoint 恢复且不跨 run 串扰。

**不做（修正 #10 红线）**：

- 不增加任何额外 LLM 调用；不做真实 vendor 抓取。
- 不以"提示词写了别重算"声称已约束模型——提示词**不能保证**模型不在文案里心算出错；**代码表始终是复核依据**。
- 不改 D1 计算语义；不接 C2 假设卡/质量门控的消费逻辑。

## 1. 默认缺席 = 无卡（个股与指数一致；绝不为指数伪造财务输入）

`config["financial_panel_manifest"]` **缺省（默认）时，无论个股还是指数，初态一律没有 `financial_panel` 键**——报告显示"未记录"，提示词空段，`_log_state` 落 null。**不为指数注入任何"占位卡"**：指数的 not_applicable 卡只在接受到**显式提供的 manifest** 且可信 `instrument_type=index` 时由 D1 计算产生（该卡不含任何财务输入，仅声明不适用）；绝不构造 dummy 财务输入来"凑"一张指数面板。

- **CLI**：运行命令新增可选参数 `--financial-panel-manifest PATH`（缺省不注入）。
- **Web**：侧栏高级选项（文件上传或路径，随 start_req 进运行配置；未提供则不注入）。
- 指数运行**显式提供** manifest 时：D1 按 `instrument_type=index` 计算确定性 not_applicable 卡（复用既有分支，固定说明行，无任何数字）；若该 manifest 声明了 stock 类型/标的不一致 → 准备期拒绝（§2）。

## 2. 准备路径（`_prepare_graph_run` 内，图执行与任何 LLM 调用之前）

fresh 运行新增步骤（顺序固定）：

1. 读取并解析 manifest 文件（OS/JSON 失败 → 准备期报错，运行不启动）。
2. 以**运行的可信上下文**调用 D1 纯函数：`compute_financial_panel(manifest, instrument=company_name, instrument_type=config["instrument_type"] or "stock", analysis_date=str(trade_date))`。分析时点取**运行的 trade_date**（与 C1 可信截止同源），manifest 无法覆盖；标的/类型/digest 不一致由 D1 既有校验拒绝。
3. `ManifestError` → **fail-fast**：准备期抛出完整确定性错误清单，**在任何模型调用之前**终止本次运行（不静默降级、不带病运行、不留半张卡）。
4. 成功 → `init_agent_state["financial_panel"] = card`。卡内已含 `run_id` 绑定信息（见 §5 一致性字段）。

**fresh 运行身份 vs 确定性计算的区分（显式声明）**：每次 fresh 运行生成**新 run_id**（N02 身份语义，与内容无关）；面板**数值**是 manifest+可信上下文的确定性纯函数（同输入同输出）。二者职责不同——run_id 标识"这一次运行"，digest 标识"这一份输入内容"。因此：同一 manifest 在两次 fresh 运行中产生**数值相同但 run_id 绑定不同**的卡；恢复沿 checkpoint 保留**原 run 的卡与其绑定**，不做任何重算（§4）。卡内新增绑定字段 `run_binding: {run_id, instrument, instrument_type, analysis_date, manifest_digest}`，由准备路径在注入时写入（计算纯函数本身不感知 run_id，绑定是注入层的职责）。

## 3. 提示词接入（既有 RM/Trader/PM，个股与指数同构）

新增 `panel_context_for_prompt(state)`（与 `evidence_context_for_prompt` 同模式的确定性渲染）：

- **只渲染已计算的合规值与其合规依赖出处**：面板卡的 `metrics[].status == ok` 项及 `scenarios.status == ok` 项；每项只携带 `derivation.inputs[]` 中**实际参与计算**的 input（D1 的依赖链天然只含 eligible 输入——future/unknown 披露的输入在计算前已被 as-of 排除，绝不出现在依赖链）。**绝不渲染卡片内嵌的完整原始输入表**（inputs 表保留被排除的未来值，仅用于离线复核 JSON，不进提示词）。
- **缺口与限制必须存活**：即使面板**零个 ok 项**（全 unknown/not_applicable），提示词块仍输出缺口摘要——逐公式家族的 `reason_code` 计数与关键 limitations（限定口径注记、用户给定假设声明、"未列报≠0"类红线）——决策层必须知道"面板存在但算不出"与"没有面板"的区别。
- 有界：ok 数值行 ≤15 + 溢出说明；情景块单列并标"用户给定假设，非财报事实"。块尾固定规则：数值由代码复算，引用时**照抄不得改写或重算**；模型若见分析师报告与面板矛盾，必须显式指出矛盾而不是自行仲裁。
- **无卡（未提供 manifest / 旧流程）**：空串，不凭空生成（与质量卡/证据索引/假设卡同一约定）；指数 not_applicable 卡渲染单行声明，无任何数字。
- 注入位置：个股 `create_research_manager` / `create_trader` / `create_portfolio_manager` 与指数 `create_index_trader` / `create_index_portfolio_manager`（指数 RM 恒用个股版工厂）——与 quality/evidence 块并列，**不新增任何 LLM 调用**；分析师提示词不注入。

诚实边界（写入代码注释）：提示词不能杜绝模型重算/写错；纠偏依据是**始终保存的代码表**（state → `_log_state` 统一 JSON → Web/MD/PDF section）与模型文案的逐值可对照性。

## 4. 保存、恢复与失败语义

- **保存**：`_log_state` 已持久化（D1 交付，旧 JSON 无键 → null=未记录）。
- **恢复（`resume_step is not None`）只读持久化 state，绝不重读 manifest**：
  - 面板是 checkpointed state 的一部分，随原 run 恢复；恢复期间删除/篡改/替换 manifest 文件对结果零影响（实现与测试都以"恢复路径不打开 manifest 文件"为准绳）；
  - **一致性失配 = fail-fast，而非静默降级**：恢复时校验卡内 `run_binding` 与 state 的 `run_metadata.run_id`、当前可信 `instrument`/`instrument_type`、`trade_date`（及 `manifest_digest` 与卡内嵌输入表的自洽）——任一失配 → **准备期抛错、在任何模型调用之前终止**，错误信息逐项列出失配字段。绝不"静默按未记录处理"——那会把损坏的运行伪装成干净的旧式运行。
  - 旧断点（D1 之前创建，state 无 `financial_panel`）：保持缺失 → "未记录"（这是**合法的旧态**，不是失配；判定依据是"键不存在"，而非"键存在但内容不符"）。
- **fresh 隔离**：面板只在 fresh 准备期计算一次；并发运行各自绑定各自 run_id（与 C1 evidence_bundle 的运行隔离同构）。

## 5. 验收标准（获批后据此写测试）

1. 默认缺席：个股与指数、不配置 manifest → 初态无 `financial_panel` 键、提示词无面板块、JSON 落 null、现有行为零差异；**指数无 manifest 时无任何注入**。
2. fresh 注入：合法 manifest → 初态含卡且 `run_binding` 完整；提示词块只含 ok 值与其实际参与依赖（构造含 future-excluded 输入的 manifest，断言被排除 input_id 不出现在提示词、完整输入表仍在 JSON）；零 ok 面板 → 缺口摘要与 limitations 仍在提示词块中；LLM 调用次数与无面板时完全相同。
3. fail-fast（模型调用前）：非法 manifest、标的/类型不一致、digest 不匹配 → 准备期 `ManifestError`；恢复路径 `run_binding` 任一字段失配 → 准备期报错并列出字段；图未启动、无任何模型调用。
4. 恢复纯读：MemorySaver/SQLite 续跑 → 面板逐字节保留；恢复期间删除/篡改 manifest 文件无影响（monkeypatch 证明无文件读取）；旧断点无键 → 未记录（合法）。
5. 确定性 vs 身份：同一 manifest 两次 fresh → 数值与 `manifest_digest` 相同、`run_binding.run_id` 不同；同一 run 的卡与其 checkpoint 绑定一致。
6. 指数：显式 manifest + index → not_applicable 卡（无数字）；显式 stock manifest 于 index 运行 → 准备期拒绝。
7. 无越权：D2 不新增网络/工具/LLM 调用；D1 既有 64 项测试全部保持通过（计算语义零改动）。

## 6. 实施清单（审核通过后）

1. `financial_panel.py`：`panel_context_for_prompt(state)`（只含 ok 值+eligible 依赖；零 ok 时的缺口摘要与 limitations 存活；≤15 行上界）。
2. `trading_graph.py`：fresh 分支 manifest 读取/校验/注入 + `run_binding` 写入；恢复分支**纯读** + `run_binding` 一致性 fail-fast 校验（仅"键存在但失配"才报错；键缺失=合法旧态）。
3. 五个决策节点接入 `panel_context_for_prompt`（个股 RM/Trader/PM + 指数 Trader/PM）。
4. CLI/Web 入口各一处（默认关闭）。
5. `tests/test_financial_panel_integration.py`：§5 全部验收。

## 7. 与既有批次的关系

- 面板块与质量卡（N01）、证据索引（C1）、假设卡（C2）在决策提示词中并列，互不消费、互不改写语义。
- as-of 纪律与 C1 可信截止同源（分析时点=运行 trade_date；future 输入在 D1 计算内排除，且不进提示词）。
- 本设计不宣称任何决策质量提升；效果评估属后续批次（以代码表为对照基线）。
