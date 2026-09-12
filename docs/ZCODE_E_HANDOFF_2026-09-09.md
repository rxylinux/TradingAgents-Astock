# E 交接（E1 初判/分歧 + E2 有界补查，ZCode → Codex）

日期：2026-09-09。权威契约：`docs/E_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`（十条 + 四决策，未改动）。D2 及以前批次零改动（除 D2 测试 fake 补绑新增校验方法）。全部离线；未跑全量。

## E1：互盲初判 + 分歧规划（≤3 次逻辑调用）

- **默认关闭零改动**：`config["evidence_debate_enabled"]` 缺省 False——`setup.py` 只在开启时注册 E 节点，`Quality Gate → Bull Researcher` 原边保留（测试断言默认拓扑节点/边与无 E 完全一致）。
- **互盲**：`create_initial_view_node("bull"/"bear", llm_for(role))` 并行节点，输入 = 分析师报告 + C1 证据索引**投影**（不整 state 格式化）；测试断言 bull 提示词不含 bear 初判内容。
- **主张校验（规则 3）**：无效引用主张**保留**并附逐引用 verdict（dangling/future/unknown_time），不删除制造合规率；≤10 主张/≤5 引用截断明示；confidence 统一 `uncalibrated`；结构化失败 → `freetext_fallback` 空初判（不伪造）。
- **规划节点（规则 4）**：一次结构化调用；question/disagreement_index/decision_impact/tool_name/**tool_args 直出**（无每问构造 LLM）；代码确定性校验——前 2 个合格问题获得槽位，超出 `not_rechecked`；空 impact/越界 index/非法 schema/未允许工具 → `unresolved(原因)`。
- **调用与预算（规则 2）**：逻辑调用恰 3 次（初判×2+规划×1）如实计数——`_CountingLLM` 代理统计实际请求数（含结构化路径与 B1 有界重试内的每次请求）；2048 输出上限经 `bind(max_tokens)` 施加（不改共享客户端可变配置；不支持绑定的供应商降级为提示词约束并记录）；known_tokens 仅记已知，缺失 → `"unknown"`（不是 0）；上限是输出约束，不声称控制输入 token。

## E2：有界补查（≤2 次实际工具调用，每问题恰 1 次）

- **工具面（规则 4/5）**：仅 `get_news`/`get_global_news`（当前启用且支持 C1 artifact）；不注册新工具。
- **硬校验先于执行**：ticker 走既有 A 股校验；日期严格解析并经 **C1 可信截止收窄**（整个校验+执行都在 `trusted_cutoff` 调用域内——修复了校验在域外导致收窄失效的缺陷）；`limit≤20`、`look_back_days≤30` 硬上限（模型不可提高）。校验失败 → `unresolved` **不发起调用**（测试断言 fetcher 零调用）。
- **单调用结构保证（规则 5）**：代码从校验后的 args 构造**恰好一个 tool_call** 交给真实 ToolNode——同轮批量/多 tool_calls 结构上不可能；稳定 `tool_call_id = recheck-q{slot}-{question哈希}`。
- **C1 通路（规则 6）**：新证据经既有 artifact→delta 通道入 `evidence_bundle`（事件 role=`recheck_q0/q1`，绑定本 run）；**来源特定 ID**——跨来源转载以 `content_digest` 标记 `repost_duplicates`、不投票；未知来源关联保持 unknown。
- **无裁决（规则 7）**：状态机 `retrieved / inconclusive / unresolved / error`（无 resolved_side——输出与渲染均无该字段）；新引用附校验结果；双方数值/观点并存；人工复核标志恒真。
- **预算持久化与恢复（规则 8）**：`usage`（tool_invokes/实际请求/已知 tokens）与已消费槽位随 state 进 checkpoint；MemorySaver interrupt→resume 实测：q1 不重跑（fetcher 计数不变）、q2 续跑、usage 延续累加、q1 补查结果恢复前后逐字节一致。**限制如实声明**：外部调用已发出但未落 checkpoint 的崩溃窗口不宣称 exactly-once（节点返回原子写入 state，LangGraph 节点完成即 checkpoint，窗口=节点执行中）。
- **拓扑失配拒恢复（规则 8）**：断点含 E 字段而当前关闭 → 拒绝；当前开启且辩论已开始但断点无 E 字段 → 拒绝；其余组合合法（断点保留，模型调用前）。

## 下游消费与出口（规则 9）

- `evidence_debate_summary_for_prompt`（有界 ≤2400 字符）注入 RM + 个股 bull/bear + 指数 bull/bear（显式看到未解决分歧与缺口，声明"引用/时点有效≠语义支持"）；空段不冲掉原数据限制。
- `_log_state` 落 `initial_view_bull/bear` + `evidence_debate`（旧 JSON → null）；Web expander「⚖️ 独立初判与分歧核查」+ MD/PDF section 共用 `render_evidence_debate_md`（旧报告"未记录"；无胜率表述）。
- E 不改 C2 评估状态/D 数值/辩论轮数；不自动交易/提醒。

## 验证（pytest 定向，未跑全量）

```bash
.venv/bin/python -m pytest tests/test_evidence_debate.py        # 26 passed
.venv/bin/python -m pytest -q docs/audit_*.py（六份独立审计）      # 55 passed（无回退）
.venv/bin/python -m pytest tests/（E+全部触达模块定向回归）        # 346 passed
.venv/bin/python -m ruff check（全部 E 触达文件）                  # All checks passed
```

契约第 10 条矩阵覆盖：默认图节点/边不变；两初判互盲（真实节点提示词断言）；逻辑调用 3 且替身实际计数一致；5 问题只补 2、未知工具/非法参数/越界日期/多 toolcalls 场景下实际工具 invoke ≤2；两种新闻工具经真实 ToolNode 到 C1 artifact 通路；重复来源（同 content_digest 不同 ID）标记不投票；合法引用不自动 resolved（无 resolved_side 字段断言）；供应商双失败 → error 状态留限制；interrupt→resume 执行完成且已完成节点计数不增加（fetcher 计数对照）；两并发 run 隔离；三出口一致（MD/PDF/Web 渲染 + 旧报告未记录）。

## 文件清单（SHA-256）

```
a6d5fb171e28ea6c4d003847da171ac014cc360183a0403632d8e3d27530be4f  tradingagents/agents/debate_evidence.py
62307c38a0bf6f1022451e36cee21421bbd588ae963debdc6332ae1889d85c49  tradingagents/graph/setup.py
31e8336cbe91fb8ce2508515698825a65930b4146b5ab9c304afedfad2283d5a  tradingagents/graph/trading_graph.py
1d92a6bcc2650aa9b91916919e6ba44d8e647ac7fffc12139526b134668445fa  tradingagents/agents/utils/agent_states.py
ffc65ba0e420b649915f8e4de143df61c97874679d96194bff78fd99831026ca  tradingagents/agents/researchers/bull_researcher.py
5253abcc80774b21c6aa63b5f59a40d88212e899e90809be4d6dd41fe4da6704  tradingagents/agents/researchers/bear_researcher.py
6e6b6b75bc1c6602e0986db947e047b09b2dcd00d14658062d85615456f64839  tradingagents/agents/managers/research_manager.py
00803efd850a445f42de6340fd5df880844d1a9653a838f48992f41f2c293e29  tradingagents/agents/index_agents.py
f83cd6d7c89bcda2afac41ef48894ebc081ac04a6a75dff2a89c151f9456a572  web/pdf_export.py
d564c428f0904077dd655ff718b5c5e9c7bb2d8cd8fef9b3f9fefcb83fbe8118  web/components/report_viewer.py
c1ccba9efebcf525bcca1e6c05fe1a46b689e3d2451b07729b82f7eb8c480d68  tests/test_evidence_debate.py
7489acd277b1b42388730e184348b7ec004edd5d874051b6cdb44e206758b791  tests/test_financial_panel_integration.py
```

## 限制与未做（如实）

- 未提供 CLI/Web 的 E 开关入口（契约只要求 config 键；默认关闭零行为差异已测试）。
- exactly-once 语义按规则 8 的保守口径声明（节点原子写 + 恢复槽位守卫）。
- A/B 配对报告（E-on/off 同预算条件）属 F3 评测层，本批未做契约指标之外的对照运行。
- usage 的 known_tokens 仅在供应商返回 usage_metadata 时记录，否则 unknown（不写 0）。

---

## R1 修正附录（2026-09-09 01:1x，对应 docs/E_DEBATE_REVIEW_2026-09-09.md）

Codex 独立审计 9 failed / 2 passed → 修复后 **11/11**（脚本未改动）。六组修复：

1. **输出上限到达真实 HTTP**：实测发现 `bind(max_tokens).with_structured_output()` 在本技术栈丢失绑定（请求仍带共享客户端的 9000）。改为 `with_structured_output(schema, max_tokens=2048)` kwarg（实测到达请求体）+ plain 路径 `invoke(..., max_tokens=2048)`；供应商拒绝 kwarg 时降级并记录 `cap_applied=False`（不再用宽 except 静默回退无上限）。共享客户端对象零改写（审计断言 `llm.max_tokens == 9000` 保持）。
2. **B1 重试能力转发**：`_CountingLLM` 新增 `_get_retry_budget` 委托——显式 `max_retries=0` 在持续 429 下恰好 1 次 HTTP 请求（真实 MockTransport 验证）；包装器绝不放宽预算。
3. **可信标的绑定**：补查 ticker 必须等于 `state.company_of_interest`——格式合法但异标的（600519 运行请求 000001）在到达供应商前 `unresolved(instrument_mismatch)`（真实 StateGraph→ToolNode→vendor 替身断言 fetched 不含异标的；合法同标的控制例恰好执行一次）。
4. **时间前置校验完整**：可信 `trade_date` 严格解析（缺失/非法 → `unresolved(no_trusted_date)`，不退化为无界调用）；start/end/curr 全部严格解析（非法 → `unresolved(invalid_tool_args_dates)`）；end/curr 收窄到可信截止后**再验证 start ≤ end**（倒序窗口 → `unresolved(reversed_window)`）；个股窗口有限回看（>366 天收窄 start 并记 `hardening_note`）；`limit/look_back_days` 拒绝 bool。四类审计场景全部零 ToolNode invoke（class 级 monkeypatch 计数断言）。
5. **工具白名单取自实际注册**：`GraphSetup._evidence_recheck_tools(selected_analysts)` 从**选中分析师的 tool_nodes.tools_by_name** 取名为 get_news/get_global_news 的真实对象——只注册 get_news 时 E 只带 get_news（审计 monkeypatch create_recheck_node 断言名单不扩大）；无可用工具 → 空白名单（规划提示词如实告知"无可用工具"，不构造新工具）；规划节点 `validate_plan(allowed_tools=...)` 按实际白名单把关（未启用工具的问题不再占用前 2 合格槽位）。
6. **摘要预算含尾注**：截断说明长度预留进 max_chars（2400 上限实测不再越界）。

验证（定向，未跑全量）：`tests/test_evidence_debate.py` **33 passed**（新增 `TestER1Boundaries` 7 项本地镜像：真实 HTTP cap/零重试单请求/异标的前置拦截/白名单门控/bool limit/有限回看收窄/摘要预算含尾注）；**七份独立审计合计 66 passed**（E 11/11、D2 6/6、D1 11/11、C2 8/8、C1 7/7、A 12/12、B1 11/11）；触达模块定向回归 **353 passed**；ruff 全绿。两处既有测试断言按收紧后的正确语义更新（异标的在格式校验前被拦；未来窗口收窄后倒序 → 前置拒绝）。

相邻检查项（review 未计失败，已记录待后续批次）：计数器语义拆分（runnable 调用 vs HTTP 请求）、自由文本总量硬限、None 结构化响应防崩、E state 完整性锚点、下游摘要增加双方主张与决策影响细节。

### R1 后文件清单（SHA-256，以此为准）

```
6f6d7acb873da38f385c18ea3b8c74139a0b5d7e0e4f9a33f90d85025276dc22  tradingagents/agents/debate_evidence.py
380b34fc4176524a86ee793f05326aeb78b3818ec05f35e40fec1783eefbbee5  tradingagents/graph/setup.py
3eb1bdb2d98d6e6068455a39b18f10bf23d799b63e26b2d9d19d5c084ebff0f0  tests/test_evidence_debate.py
```

---

## R2 修正附录（2026-09-09 01:4x，对应 docs/E_DEBATE_REVIEW_2026-09-09.md R2）

Codex 独立审计 7 项新增失败 → 修复后 **18/18**（11 旧 + 7 新，脚本未改动）。七组修复 + 相邻路径：

1. **规划白名单贯通（2 例）**：`allowed_tools=None` 才用文档化默认——**显式空列表保持空**（原 truthiness 回退到全局常量）；结构化成功分支补传 `allowed` 到 `validate_plan`（此前仅后备分支传）。
2. **不可强制上限 → 拒绝而非无界调用**：能力探测在构造期进行——`with_structured_output(schema, max_tokens)` 抛错 → 标记 `cap_enforceable=False` 并发放**拒绝型**委托（invoke 即抛 `_CapUnavailableError`）；plain 路径 TypeError **不再重试**（请求可能已发出）同样转拒绝；节点捕获后生成受限记录（零请求、零主张、`usage.cap_enforceable=False`、limitation 说明）。正常支持 cap 的控制例保持单次带 cap 请求。
3. **空结构化响应防崩**：render 先做 `isinstance` 校验——None/纯文本响应作为解析失败走既有后备；后备产物为受限记录（无主张、有 limitation、usage 已耗）；捕获列表只收真模型对象（`captured[0]` 不再是 None）；规划节点同型防护。
4. **恢复跨 run 拒绝**：`_validate_resumed_evidence_debate` 增加归属锚定——`evidence_debate.run_id` 与**两个初判的 `run_id`**（节点现在写入 run 绑定）都必须等于 `state.run_metadata.run_id`；缺失绑定 = "不能自证归属" 拒绝；异 run 拒绝且断点保留。旧无 E 断点（字段缺失）与初判前 E-on/off 切换的既有语义不变。新增**真实 prepare + SqliteSaver** 复现用例（mini 图 interrupt 于 planner 前保留异 run 状态 → `_prepare_graph_run` 抛"异 run"）。
5. **单主张硬文本限**：`_clip` 硬上限——claim 500/假设 300/问题 500/impact 300/topic 300/ID 120/主张总量 4000 字符；截断明示（truncated）写进 limitations；规划侧与持久化侧同界生效；坏主张仍保留不删。
6. **请求数与 token 独立汇总**：`aggregate_usage` 两个维度分开——已知请求数不因 token unknown 被抹成 unknown（3 次已知请求 + unknown token → `actual_request_count=3, known_tokens="unknown"`）；新增诚实命名字段 `http_request_count="unknown"`（传输层未观测）+ `request_count_semantics="wrapper_observed_runnable_invokes"`（plain 内部重试可能使真实 HTTP 更多——不冒充）。
7. **下游摘要实质化（R1 相邻项）**：`evidence_debate_summary_for_prompt` 现包含双方各前 3 条主张文本（含校验状态）、分歧的 `decision_impact`（截断 140）、补查返回要点（`tool_response_excerpt` 截断 160）——不再只有条数；全部在既有字符预算内。

验证（定向，未跑全量）：`tests/test_evidence_debate.py` **42 passed**（新增 `TestER2Boundaries` 9 项：空/受限白名单、拒绝式 cap、空结构化响应、run 绑定、真实 prepare 跨 run 拒绝、百万字符截断、请求/token 独立、摘要实质内容）；**七份独立审计合计 73 passed**（E 18/18、D2 6/6、D1 11/11、C2 8/8、C1 7/7、A 12/12、B1 11/11）；触达模块定向回归 **362 passed**；ruff 全绿。测试替身补 `**kwargs` 以匹配真实客户端的 cap kwarg 语义（不改变断言意图）。

### R2 后文件清单（SHA-256，以此为准）

```
116724c27159ffdd421f2e07e1e2bb25dcd423e0092712b507b31a35828424ed  tradingagents/agents/debate_evidence.py
38104be7da5f4a615b75d5b17005f0780b0721218edce70db7a022ced1194a24  tradingagents/graph/trading_graph.py
8566a9545c6f67b661c2ff8ad06fdd41ac3897ae7386c0cf1eae58418c4d85d2  tests/test_evidence_debate.py
```

---

## R3 修正附录（2026-09-09 01:5x，对应 docs/E_DEBATE_REVIEW_2026-09-09.md R3）

Codex 独立审计 5 项新增失败 → 修复后 **23/23**（18 旧 + 5 新，脚本未改动）。五组修复：

1. **真实生产图阶段切换拒绝（核心）**：fresh `_prepare_graph_run` 现在**无条件持久化 E 模式/版本锚点** `run_metadata.evidence_debate = {enabled, schema_version}`（两种模式都写）——恢复的每个阶段都与当前配置比较：**双向切换都拒绝**（E-off 断点 + E-on 会静默跳过全部 E 节点——LangGraph 恢复已持久的待执行 Bull Researcher，不会因新图插入 E 边重经 Quality Gate；E-on 断点 + E-off 会截断 E 产物）。无锚旧态（早于 E 的断点）+ 当前 E-on = 无法证明阶段兼容 → 拒绝；**同模式（off↔off / on↔on 含完好锚点与 E 字段）兼容保留**。绝不重跑已完成节点伪装恢复。测试用**真实 GraphSetup + 真实 prepare + SqliteSaver**（update_state as_node='Quality Gate' 后断言 next==Bull Researcher），非小图重建。
2. **缺可信 run 拒绝**：E 字段存在而 `run_metadata.run_id` 缺失 → "缺少可信 run_id——无法证明归属" 拒绝（原 `elif meta_run_id and ...` 在身份缺失时跳过比较的漏洞修复）。
3. **未知 schema_version 拒绝**：evidence_debate 与两个初判的 `schema_version` 必须等于当前实现版本（999 等拒绝，不忽略版本继续使用）；锚点版本同样校验。
4. **计划损坏成 list 拒绝**：形状检查先行——E 字段存在但非 dict（list 等）= 结构损坏 → 拒绝。
5. **初判损坏成字符串拒绝**：同上——"缺失/None（合法旧态）"与"存在但坏类型（损坏）"显式区分，归属有效不代表内部结构有效。

全部失败发生在任何模型/工具调用之前，断点原样保留（不清空、不伪装缺失、不归零）。

验证（定向，未跑全量）：`tests/test_evidence_debate.py` **46 passed**（新增 `TestER3Restoration` 4 项：真实图双向切换拒绝、同模式接受、损坏形状/未知版本/缺 run 三类拒绝；一处旧测试按锚点语义更新——"开启+无字段且未开始"不再是合法态，合法 E-on 恢复要求完好锚点）；**七份独立审计合计 78 passed**（E 23/23、D2 6/6、D1 11/11、C2 8/8、C1 7/7、A 12/12、B1 11/11）；触达模块定向回归 **366 passed**；ruff 全绿。

### R3 后文件清单（SHA-256，以此为准）

```
2ff571a7c23ed934faf10850fc3c9c660d27129ba36a7aee35c8e61cb6d49664  tradingagents/graph/trading_graph.py
46ab0ba864a219b940856bfb6ef3a06add2e7689e547d92cd1a929945d30ac52  tests/test_evidence_debate.py
```
