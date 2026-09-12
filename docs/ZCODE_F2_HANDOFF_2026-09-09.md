# F2 交接：只读历史复盘投影接入决策提示词（ZCode → Codex）

日期：2026-09-09。权威契约：`docs/F2_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`（十条，未改动）。F1 已独立验收（1432 全量）。不改生产记忆（memory.py hash 不变断言）、无新模型调用（计数断言）、默认关闭零差异。

## 交付物

**`tradingagents/evaluation/review_projection.py`**（纯函数 + 确定性渲染）：

- **分层（契约 #2）**：`build_review_projection` 固定顺序——F1 `load_records` 全量校验（字节/条目上限、JSON 边界、digest、去重载体）→ **同 ticker+instrument_type 过滤（在 limit 之前）** → 匹配集上 `retrieve_as_of`（AND 五门）→ ≤5 条快照。异标的/类型单列计数、内容不进提示词；as_of/ticker/type/ranking 全取可信上下文；不传 query_window（not_requested）。
- **payload（契约 #3）**：不持久自由 text——selected 经校验快照 + excluded_stats + limitations + 双 digest（input_digest 原始字节 / payload_digest 覆盖全部规范 payload 除自身，**任何进提示词的字节都在摘要内**——文本由 payload 确定性渲染）。
- **入口自验证（契约 #6）**：`projection_for_prompt(state)` 先跑全量归属校验（run_id/trusted_context/policy/ranking/payload_digest 复算/嵌套）——无效返回受控说明零数值；有效渲染预算内文本（单行 160/总 1200 **含标题限制截断说明**；限制优先保留；null→unknown；declared_only 不升级；盈利≠推理正确固定文案）。
- **三态渲染（契约 #7/#9）**：`render_projection_md`——未启用（未记录）/ 零合格（非空限制块"无合格≠历史无事件"）/ 有记录（≤5 行 + 摘要 + 排除统计）。

**准备层（`trading_graph.py`）**：

- fresh `_prepare_review_projection`（模型调用前）：读显式文件（2MB/UTF-8/全量校验）→ payload → `init_state["review_projection"]` + `run_metadata["review_projection"]` 独立锚（enabled/schema/input_digest/payload_digest/ticker/type/as_of）。坏输入 `RecordValidationError` fail-fast 零模型调用。
- **恢复纯读（契约 #5）**：`_validate_resumed_review_projection`——绝不重读文件；state 与 run_metadata 双侧比对（run 绑定/可信上下文/双 digest/嵌套）；无锚、异 run、篡改、矛盾锚 → RuntimeError 断点保留（不清空不伪装缺失）；无投影旧断点合法（恢复时才提供文件也不补投影）。
- `_log_state` 落 payload（旧 JSON → null）。

**五工厂（契约 #6）**：RM、个股 Trader/PM、指数 Trader/PM 注入 `projection_for_prompt(state)`——直接调用工厂也无法拼接未验证 text；调用计数与原流程一致（断言）。**独立通道**：不碰 past_context（刷新互不影响，断言）。

**入口（契约 #1/#9）**：CLI `--review-records`（含裸跑默认路径透传）；Web 侧栏「高级：历史经验投影」expander（本机路径 + 上传临时目录）。**三出口**：MD/PDF section + Web expander 共用 `render_projection_md`。

## 验证（定向，未跑全量）

```bash
.venv/bin/python -m pytest tests/test_review_projection_integration.py   # 23 passed
.venv/bin/python -m pytest -q docs/audit_*.py（八份独立审计）              # 97 passed（无回退）
.venv/bin/python -m pytest tests/（触达模块定向回归）                      # 283 passed
.venv/bin/python -m ruff check（全部 F2 触达文件）                        # All checks passed
```

契约 #10 矩阵覆盖：默认 stock/index 关闭零块零调用；混合标的超 limit 时先过滤再限量（同标的 5 条满额 + 截断报告 + 异标的计数不占额）；异标的标题不进提示词；未来 publication 不可见；零合格非空限制块（排除分类可见）；payload 篡改（rating/selected/run 绑定）→ invalid-projection 零数值；两个 fresh 同文件 selected 相同、payload_digest 随 run 绑定不同；fresh 坏文件零模型调用；**真实 SQLite**：删输入文件 → prepare 恢复 + `invoke(None)` 完成续跑（node b 执行）、payload 逐字节保留；篡改/无锚/异 run 拒绝且断点保留；past_context 刷新不覆盖投影；五工厂提示词含投影文本、调用计数 1==1；三出口三态；MD 导出 + `_log_state` 落 payload；生产 memory.py hash 不变。

## 限制（如实）

- F2 只证明工程契约（只读投影接入），不代表预测效果改善（渲染固定免责）。
- 上传临时文件仅供 fresh 注入；恢复不依赖（契约 #5）。
- F3 配对评测未开始（设计待 Codex 终契约——预测特征不得含 F1 记录的 outcome/annotation 标签已在 F3 草案标注，待终契约确认）。

## 文件清单（SHA-256）

```
b19ef2c8cc85c811edae0ba6e21f65f86d123042911a1072c6f780a47a3f70ce  tradingagents/evaluation/review_projection.py
c36fb09a8ed16e69511d5c469b389ca441c023b2369d4082fd6a5fbd5488e000  tradingagents/graph/trading_graph.py
0736077bbfc1effd110ca43f2bce4306c2d0f09c7dd5a063abf64a47ce683774  tradingagents/agents/utils/agent_states.py
4e7983ac2bb71f78aabbdca44b59aa344def2aaaba1152261517427ee2cdb41b  tradingagents/agents/managers/research_manager.py
c1659e46290c92867757b9bc171492c2dd7a263f55ca741f3bd9067d02e94229  tradingagents/agents/trader/trader.py
dd8d53d5dce3dcb98fecafd3bb2b5944b23b30dd869deb9041f5458eb4e87dd5  tradingagents/agents/managers/portfolio_manager.py
34545abc715387ef2e00335ae4cceb6a889ba104f4066d0ffbef0365118b4fda  tradingagents/agents/index_agents.py
bff5baa3554bc3f0257325a60ef19839dff4d1056740473a06c8d97607e795a3  web/pdf_export.py
8baa7d0258ee1c5c9ca8e833dc06f17854e2a22d00b35aa090894e6d283181b8  web/components/report_viewer.py
4e4832eb7934c7eff6c6389047bd9b73c36e8e789dbaf2ae62a9ed4d0668a5a9  web/components/sidebar.py
b6b9ee15a7fb09cdc0fe311e41a4a1a3172b24e10eb52892cfdac89dac0f0896  web/app.py
1cce9f191f9e21449a0549e9e771bda6a12d4a90acb2351f331140bb5413f927  cli/main.py
8d709c3cc75a41d2b2255bd8dfadeca770fab69520fb880f0b7a68071b110302  tests/test_review_projection_integration.py
ce272c8f47ef73a9bc0f35018ec55d585bbc870d55efca79de934f1c7a1b945a  tests/test_financial_panel_integration.py
```

---

## R1 修正附录（2026-09-09 03:1x，对应 docs/F2_REVIEW_2026-09-09.md R1）

Codex 独立审计 13 failed / 1 passed → 修复后 **14/14**（脚本未改动）。五组修复按"统一完整校验"而非逐字段补洞：

1. **提示词要求独立元数据锚（4 例）**：`projection_for_prompt` 重写——必须存在**非空可信 run_id + enabled 的 run_metadata.review_projection 锚**；统一校验器新增 `anchor` 参数比对锚的全部声明字段（enabled/两 digest/schema/ticker/type/as_of），payload 自身摘要自洽不再等于运行归属；缺失/不符 → 受控 invalid-projection **零数值**。**真实 RM 工厂**（捕获 LLM 单次调用）在锚 payload_digest 不符时只收到 invalid-projection、无 AUDIT 值/收益（4 例 + 工厂例全过）。
2. **受控损坏（2 例）**：统一校验器前置**递归 shape/JSON 合法性**（NaN/Infinity/坏类型/坏 Unicode → 校验问题，摘要复算永不见非法值）；提示词入口整体 try 包裹——任何异常转受控说明，不裸崩。
3. **恢复完整性与模式（5 例 + 相邻）**：`_validate_resumed_review_projection` 重写——先**模式/schema 锚**与当前配置比对（**fresh 两种模式都持久化 enabled 锚**，双向切换拒绝），再 payload 有无（有锚无 payload ≠ 旧态 → 拒）；真正旧断点（无锚）关闭模式兼容、开启模式拒绝；**缺失可信 run_id 即拒绝**（"有才比较"语义废除）；锚全部字段比对。真实 SQLite 控制例：同模式恢复正常（删文件零读取、payload 保留）、开启断点+关闭配置 prepare 拒绝、篡改断点保留。
4. **摘要自洽的非法快照（1 例）**：完整 schema 校验（selected 数量/结构/有限非 bool 数值/固定单位/来源状态、统计非负整数、版本非 bool 整数、limitations 字符串列表）——哈希不替代字段验证。
5. **真实 past-context 刷新控制（相邻项）**：新增用真实 `_safe_resume_checkpoint` 的控制——断点存 STALE 值、恢复用一致替身 → past_context 被原位刷新为替身且投影逐字节保留（两通道独立的生产路径证明，非字典赋值）。

验证（定向，未跑全量）：`tests/test_review_projection_integration.py` **35 passed**（新增 `TestF2R1Boundaries` 12 项：四类锚损坏、真实工厂、受控损坏、自洽非法快照、双向模式切换/旧态/有锚无 payload、fresh 双模式锚、真实 SQLite 模式切换+同模式恢复+篡改、真实刷新控制）；**九份独立审计合计 111 passed**（F2 14/14 + 其余无回退）；触达回归 **295 passed**；ruff 全绿。

### R1 后文件清单（SHA-256，以此为准）

```
888c55f81b5c80c3956b7eaaf207c78b1610c7a2cd419cba89b74947e63bd7e4  tradingagents/evaluation/review_projection.py
2240fb52d6afaa552229408a0525d0dae62d558f1d0fc115cfbb11fb8a79ef70  tradingagents/graph/trading_graph.py
acd735f297cc6e9a1885f1ab8d255d588becaea335ef7ca1bce73b977f65d507  tests/test_review_projection_integration.py
```

---

## R2 修正附录（2026-09-09 03:4x，对应 docs/F2_REVIEW_2026-09-09.md R2）

Codex 独立审计 8 项新增失败 → 修复后 **22/22**（14 旧 + 8 新，脚本未改动）。五组修复：

1. **可信字段必填（3 例）**：统一校验器严格模式——传入的 ticker/type/as_of/run_id **必须是非空字符串**（缺失/None/空串都是无效身份，`if trusted` 跳过语义废除）；提示词入口对缺失 company_of_interest/instrument_type/trade_date 全部受控拒绝零数值。
2. **enabled 锚 + 缺 payload ≠ 关闭（1 例）**：`projection_for_prompt` 先分辨模式——enabled 锚存在而 payload 缺失 → 受控 invalid（损坏的启用态）；仅无锚或明确 disabled → 空块。
3. **快照时间 schema + AND 可见性（2 例）**：selected 快照**携带五门复核时间**（decided/maturity/observed/publication/record_available——build 补齐后三字段，不再只留收益与到期）；校验用 F1 严格时间原语（naive/垃圾拒绝）+ 对投影 as_of 的 AND 门（未来 maturity 2099、缺任一必需门 → 拒绝）。
4. **浮点版本拒绝（1 例）**：锚 schema_version 必须"已知 int 非 bool"——`isinstance(int)` 显式检查，1.0 拒绝（payload 与 ranking 同规则）。
5. **出口披露（1 例 + 同类）**：`render_projection_md` 保留 `limit 截断`与 `overlap_filter=not_requested` 披露；结构专用校验路径（无运行上下文时不做身份比较、不称归属）；list run_binding/context 等损坏 → 受控说明不裸崩；错误说明不回显未验证历史正文（断言 LEAK 标记不出现）。

验证（定向，未跑全量）：`tests/test_review_projection_integration.py` **45 passed**（新增 `TestF2R2Mirrors` 10 项）；**九份独立审计合计 119 passed**（F2 22/22 + 其余无回退）；触达回归 **305 passed**；ruff 全绿。

### R2 后文件清单（SHA-256，以此为准）

```
fa6937f0d6f836aa420addb23cbf3417f15cf635766ecf4f970974375a2ebe4c  tradingagents/evaluation/review_projection.py
22747128af268ca70af808b213eeca95e078eee53d408f2c4f0127ae9cad85c3  tests/test_review_projection_integration.py
```

---

## R3 修正附录（2026-09-09 04:0x，对应 docs/F2_REVIEW_2026-09-09.md R3）

Codex 独立审计 4 项新增失败 → 修复后 **26/26**（22 旧 + 4 新，脚本未改动）。两组修复 + 完成边界：

1. **真实 fresh 默认关闭零差异（stock/index 2 例）**：`projection_for_prompt` 的 mode 分支补上**显式 disabled 锚 → 立即返回空块**（R2 重构后 disabled 分支漏了 return，继续验证 None payload 产出 invalid 段）。真实 `prepare_graph_run`（两种 instrument_type 默认配置）→ 真实 RM 工厂：有/无 F2 锚的两次提示词**逐字节一致**、模型各调用 1 次——默认关闭真正的零差异（不再拿空字典冒充）；disabled 锚 + payload 并存 = 矛盾拒绝；损坏 enabled 态仍拒绝。
2. **报告坏形状受控（run_binding/trusted_context 为 list 2 例）**：校验器改为**显式双模式**——`mode="structure"`（报告渲染：shape/JSON 合法性短路返回，坏对象**永不被解引用**，schema+摘要复算，不做身份比较、不按错误文案过滤）与 `mode="strict"`（准备/恢复/五工厂：加必填可信身份与独立锚比对）。schema 时间分支对 `trusted_context`/`run_binding` 先做 isinstance 守卫再取键；`render_projection_md` 与提示词入口对坏输入均受控、不回显历史正文。

**完成边界（review 列出，一并落实）**：`trusted_context.as_of` 无法解析 → 明确"时间门拒绝执行（不跳过）"（原先会静默跳过 AND 门）；strict 模式对 run_id/ticker/type/as_of 的 `str(...)` 包装移除——错误类型不再被转成"合法可信身份"（`isinstance(str)` 显式检查）。

验证（定向，未跑全量）：`tests/test_review_projection_integration.py` **53 passed**（新增 `TestF2R3Mirrors` 8 项：真实 prepare→RM 默认零差异 stock/index、报告/提示词坏形状不回显、双模式语义、不可解析 as_of 拒绝时间门、disabled+payload 矛盾）；**九份独立审计合计 123 passed**（F2 26/26 + 其余无回退）；触达回归 **313 passed**；ruff 全绿。

### R3 后文件清单（SHA-256，以此为准）

```
4cefc75cc2df569ab85e575a9df36e5712bb962ba3a37a2aa32d7c441d6c90b4  tradingagents/evaluation/review_projection.py
2c161a3753ffce60de9e1f37de24ad25d85e3bc00721bb5d43dcc8f5f27b49a8  tests/test_review_projection_integration.py
```


---

## R3 精度修正与 CLI 示例附录（全量回归期间补充，仅文档——源码零改动）

### 精度修正（如实限定 R3 声明范围）

R3 附录中"strict 模式移除 str(...) 包装——错误类型不再被转成合法可信身份"的表述**过强**，如实修正为：

- **提示词调用方**（`projection_for_prompt`）仍以 `str(state.get(...))` 读取可信字段后传入——`None` 会被转成字符串 `"None"` 并因与 trusted_context 不符而被拒绝（结果安全，机制是字符串比对而非原始类型检查）；只有**完全缺失键**走"缺失即拒绝"路径。
- **strict 校验器本身**对 `run_id/ticker/instrument_type/as_of` 为 `None` 时按"未提供"跳过比较（可选参数语义）——**并未实现普遍的原始类型拒绝**。
- **当前验收范围**因此是：生产路径中的可信字段均为字符串上下文 + 已测的坏形状（list/NaN/非法时间）与时序字段拒绝；非字符串可信类型的系统性拒绝不在本批声明内。

### F2 实际启用示例（已交付，CLI/Web）

```bash
# CLI（含裸跑默认路径透传）：提供显式 F1 记录文件即启用只读投影
.venv/bin/python -m tradingagents --review-records tests/fixtures/review_records/records.jsonl   600519 2025-06-05   # 实际 CLI 以交互向导为准；--review-records 进入 config

# 坏输入在准备期退出（exit≠0 的受控错误），不产生任何模型调用：
#   review_records 文件缺 schema/digest 失配/超 2MB → RecordValidationError

# Web：侧栏「高级：历史经验投影（只读，可选）」→ 本机路径或上传 JSONL
#   （上传写入会话独立临时目录；断点恢复不依赖该文件仍在）
```

默认（不提供 `--review-records` / Web 不填）＝ 关闭：提示词零块、`_log_state` 落 null、生产行为与 F2 之前逐字节一致（真实 prepare→RM 已测）。

### F3 计划用法（**未实施**——契约已定稿，F2 验收后开发）

```bash
# 两阶段离线（设计目标形态，尚无代码）：
# 阶段 1 prepare：显式特征 + 试验配置 + 时间切分 → 不可变评估计划
python -m tradingagents.evaluation.paired_eval prepare \
  --features features.jsonl --trials trials.json --splits splits.json \
  --output-dir plan/            # 不接受标签路径、不触模型

# 阶段 2 score：冻结计划 + 两臂预录预测 + 独立标签 → JSON+Markdown
python -m tradingagents.evaluation.paired_eval score \
  --plan plan/eval_plan.json \
  --predictions-baseline baseline.jsonl --predictions-candidate candidate.jsonl \
  --labels labels.jsonl --as-of 2025-07-01 \
  --output-dir report/          # 坏输入 exit2；同输入两次输出逐字节一致
```

以上 F3 命令为契约目标的示意——**F3 当前没有任何代码**；两臂差异仅 `evidence_debate_enabled` 布尔，预录输出仅证明工程契约（合成演示），**不代表预测效果提高**。
