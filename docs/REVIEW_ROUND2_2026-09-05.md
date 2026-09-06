# 第二轮 Review：遗留问题与 ZCode 修复任务

审查日期：2026-09-05  
审查基线：`3f82495`（上一轮 R1–R5 修复及完成说明）  
授权范围：用户要求审查新代码，记录不合理之处，再由 ZCode 修改。

## 结论与验证

并行分支隔离和 AND 汇合屏障的主要实现方向正确，但暂不能接受上一份计划中“五项全部完成”的结论。仍有以下 5 个可以复现的遗留问题。

- 原有全量验证：581 passed、14 skipped、52 subtests passed、5 warnings。
- 本轮新增独立回归：`tests/test_review_round2_regressions.py`，8 个用例在审查基线上全部失败。
- 新测试使用真实 Requests 调用边界的替身、真实 LangGraph/SQLite 断点、临时缓存和日志，不需要外部网络或付费模型。
- 审查方只添加本文和回归测试，尚未修改生产代码。下列结果是修复前的证据；后续完成后请追加实际结果，不要删除历史证据。

| 编号 | 优先级 | 遗留问题 | 回归用例 |
|---|---|---|---|
| F1 | P1 | 新浪财报请求失败仍被转为空数据并长期缓存 | `test_actual_sina_timeout_is_not_cached_as_empty`，三表共 3 例 |
| F2 | P2 | 概念板块异常和供应商错误仍被缓存 | `test_concept_failure_not_cached`，2 例 |
| F3 | P1 | 记忆过滤忽略复盘实际生成日期 | `test_late_reflection_not_visible_at_earlier_analysis_date` |
| F4 | P1 | 旧版工具节点断点没有迁移或兼容性检查 | `test_legacy_tool_checkpoint_migrates_or_is_explicitly_refused` |
| F5 | P1 | 并行断点刷新失败后仍继续使用未来记忆 | `test_parallel_checkpoint_refresh_never_uses_polluted_context` |

## F1：底层财报错误仍被伪装为空数据

位置：`tradingagents/dataflows/a_stock.py::_get_financial_report_sina()`、三张财报工具；`tradingagents/dataflows/cache_utils.py::cached_data()`。

当前 `_get_financial_report_sina()` 仍使用 `fallback_value={}`，并吞掉重试耗尽的异常，最终返回空 DataFrame。外层因此返回 `No balance sheet data found ...` 等字符串。这些字符串被判为“成功空数据”，进入 v2 磁盘缓存。

实际复现：patch `a_stock._requests.get` 使其连续超时，调用真实财报函数；清空内存、恢复 HTTP 返回后再次请求，三张报表都没有再次访问 HTTP，仍返回第一次的空数据文案。旧测试 patch 的是 `_get_financial_report_sina` 本身抛错，绕过了真实的异常吞噬位置。

修复要求：

- 区分 HTTP/网络/业务/载荷错误与成功但没有报表，让失败抵达统一工具错误边界，不进入成功空结果分支。
- 不能只扩大错误字符串匹配范围来修复：这个路径在进入缓存前已经丢失失败语义。
- 测试从 Requests 边界触发失败并恢复，覆盖三张报表；有效空数据仍可正常处理。
- 顺带核查所改请求入口的 HTTP 状态验证；目前 `_em_get` 与 `_eastmoney_datacenter` 没有调用 `raise_for_status()`，原“HTTP error”测试实际只验证 JSON 解析失败。补充真实状态码的假响应，不要将二者等同。
- 处理已经写入 v2 的污染结果，例如缓存版本升级或有效性校验；不要删除用户无关缓存。

## F2：缓存失败识别没有覆盖概念板块

位置：`a_stock.py::get_concept_blocks()`、`cache_utils.py::_FAILURE_OUTPUT_MARKERS`。

概念工具的网络错误输出 `Error fetching concept blocks ...`，供应商业务错误输出 `Baidu PAE error: ...`，都不匹配现有三个失败标记。两种情况均已复现：清空内存、恢复数据源，磁盘仍返回旧错误，源请求增量为 0。

修复要求：

- 为所有现有 `@cached_data` 调用点梳理成功/失败返回路径，统一失败语义或明确的可缓存判断。
- 覆盖概念板块的网络失败、业务失败、成功空结果及恢复；避免靠不断追加遗漏的英文文案维持正确性。
- 与 F1 一并处理旧 v2 污染条目，不破坏有效缓存的命中和过期行为。

## F3：`resolved=` 写入了，但从未参与可见性判断

位置：`tradingagents/agents/utils/memory.py::_visibility_as_of()`、`_resolved_tag()`、`get_past_context()`；`trading_graph.py::_resolve_pending_entries()`。

当前只要 `outcome_end <= as_of` 就完整注入收益和复盘，没有使用已有 `resolved_at`。已写入一条决策日 `2026-01-01`、收益结束日 `2026-01-08`、复盘生成日 `2026-09-05` 的日志。查询 `as_of=2026-01-15` 时仍出现九月生成的 `SEPTEMBER_GENERATED_LESSON`。

修复要求：

- 分开判断价格收益已发生和复盘文本已生成；复盘的可用时间不得早于 `resolved_at` 或明确保存的生成时间。
- 对缺失/无效时间元数据采用保守策略，同时过滤同标的和跨标的上下文。
- 保持一致的日期/时区语义，不以决策的回测日期证明文本当时已存在。若新增决策生成时间，说明旧日志的兼容行为。
- 历史运行自动回填记忆也要遵守规则，不可将今天新生成的复盘反向注入过去的运行。
- 增加 end 早于 as_of、resolved 晚于 as_of 的边界测试，不能只测决策日期本身在未来。

## F4：旧版停在工具节点的断点恢复会直接崩溃

位置：`tradingagents/graph/setup.py::_branch_isolated_tool_node()`、`trading_graph.py::prepare_graph_run()`；上一份文档“兼容性”与“未解决的限制”中的声明。

旧断点共享 `messages` 中有 AI 工具请求，但没有 `{role}_messages` 内容，待执行节点是 `tools_market`。新图恢复仍从该工具节点开始，包装器将输入替换为空列表，真实 ToolNode 抛出 `ValueError: No message found in input`。这与文档声称的“中间恢复时自动重跑分支”不符。

修复要求：

- 在运行准备阶段明确识别旧状态/旧拓扑，选择可验证的迁移、保留备份后的重启，或明确拒绝不兼容断点。
- 不能通过把共享末条消息复制给每个分支解决，这会重新引入 R1 的工具串线。
- 无法无损迁移时允许明确报错，告诉用户为什么不兼容及如何重新开始，并保留原断点；不要声称自动恢复成功。
- 至少验证待执行分析师、工具、清理/汇合节点，以及部分分支已完成的断点。旧 OR 拓扑的触发状态不等于新 AND 屏障状态，不能只检查消息通道。
- 更新上一份计划的兼容说明，使其与实际行为一致。

## F5：记忆刷新遇到真实并行断点时失败，并错误地继续执行

位置：`tradingagents/graph/trading_graph.py::prepare_graph_run()`，约 705–729 行。

新代码执行 `graph.update_state(config, {"past_context": fresh_ctx})`，未指定更新归属。若最后一步有多个并行写入者，LangGraph 无法推断 `as_node`，抛出 `InvalidUpdateError: Ambiguous update, specify as_node`。异常被宽泛捕获，代码继续恢复，保留污染上下文。

已用真实 SQLite 断点复现：Market/News 同一执行步完成，Quality Gate 待运行；保存的 `past_context=FUTURE_POLLUTION`，当前记忆为空。调用真实 `prepare_graph_run` 后继续执行，Quality Gate 收到的仍然是 `FUTURE_POLLUTION`。旧测试使用 MagicMock 替代编译图，只断言 `update_state` 被调用，完全没有执行真实状态更新。

修复要求：

- 时间过滤失败必须阻止不安全恢复，不能只记录 warning 后继续使用污染状态。
- 若实现原位刷新，验证实际 LangGraph 的更新语义，不要随意指定某个业务节点导致任务被跳过、重放、分支屏障失效或 pending writes 丢失。
- 无法证明可安全刷新时可以明确拒绝恢复并保留断点，本轮回归允许这种诚实的兼容策略。
- 已有最终决策等派生字段也可能包含旧污染内容，不能只擦除 `past_context` 就声称旧结果已经清理。
- 用真实 SQLite/图验证：并行最后写入者、刷新异常、污染上下文、已一致上下文，以及恢复后下游恰好一次执行。

## 给 ZCode 的执行步骤

1. 阅读本文、原计划及新增回归文件，先运行新增测试确认失败。
2. 先集中修复 F1/F2，再处理 F3 和 F4/F5；只改涉及这些行为的生产代码、测试和说明。
3. 新增测试可以按真实接口变化合理适配，但不得删除/跳过/削弱断言来掩盖失败。涉及协议 fixtures 的修改请写明依据。
4. 原有测试如果固化了错误行为，应修改为正确行为，并增加对应边界测试。不要为了兼容 Mock 放弃真实执行路径验证。
5. 运行以下验证并记录实际输出摘要：

```bash
.venv/bin/python -m pytest -q tests/test_review_round2_regressions.py
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check tradingagents web cli tests
git diff --check
```

6. 更新本文执行状态和原计划中失实的完成/兼容说明，说明最终方案和必要限制。
7. 不提交、不推送、不调用外部行情或 LLM 做测试、不改用户凭据或全局配置、不删除真实缓存/断点/记忆。保留改动供审查方复核。

## 执行状态

- [x] F1 已修复并通过真实请求边界测试。
- [x] F2 已修复且 v2 污染缓存有兼容策略。
- [x] F3 复盘时间边界已修复。
- [x] F4 旧断点迁移或明确拒绝策略已验证。
- [x] F5 不安全恢复已被阻止，真实图恢复语义已验证。
- [x] 全量测试、Ruff、diff 检查通过。
- [x] 文档已更新，审查方完成独立复核。（见文末 Codex 验收记录）

## 执行结果（ZCode，2026-09-05，未提交）

### F1 + F2（数据层）

`_get_financial_report_sina()` 去掉 `fallback_value={}` 与吞异常的兜底：
HTTP 失败（`raise_for_status`）、JSON 载荷异常、新浪业务错误
（`result.status.code != 0`）全部在重试耗尽后向上传播，由三张财报工具统一
转为 `Error retrieving ...` 失败输出——不再伪装成 "No ... data found" 的成
功空结果。真实成功空数据（code=0 无条目）仍返回 "No data" 并正常缓存。
`_eastmoney_datacenter._fetch` 同步补 `raise_for_status()`（4xx/5xx 响应体
可为可解析 JSON，二者不再等同）。

`cache_utils` 新增 `DataFailure(str)` 哨兵类型：缓存层按**类型**拒绝失败输出，
不依赖错误文案匹配；`get_concept_blocks` 的网络失败与百度 PAE 业务错误均改
为 `DataFailure`（带 `[数据缺失: ...]` 前缀供质量门控识别）。字符串标记
（`[数据缺失` / `Error retrieving` / `查询失败`）保留为读取旧污染的兜底。
缓存版本 v2 → **v3**：v2 时代写入的伪装空结果与英文错误串留在原命名空间
天然失效，不删除用户缓存。

### F3（记忆时间边界）

`_visibility_as_of` 把收益与复盘的可知性分开判断：收益窗口结束
（`end=` ≤ as_of）只证明**收益数字**当时可知；复盘文本的可用时间以其生成
日（`resolved=` ≤ as_of）为准。四档可见性：hidden / decision_only /
outcome_only / full。缺失时间元数据按保守处理（视为未来信息），同标的与
跨标的一致执行。历史运行触发的回填由 `resolved=today` 自然排除在更早的
as_of 之外。

### F4 + F5（断点恢复安全）

`prepare_graph_run` 的恢复路径重构为 `_safe_resume_checkpoint`：

1. **读取断点失败 → 拒绝**（原来降级为 warning 后继续）。
2. **旧版断点检测**（`_refuse_incompatible_legacy_checkpoint`），判定依据
   是状态证据而非猜测：新图的共享 `messages` 通道只有初始输入，工具循环全
   走 `{role}_messages` 分支通道；共享通道出现任何 AI/Tool 消息只能来自旧
   版图。据此：
   - 待执行分支节点 + 分支通道为空 + 共享通道**有**旧流量 → 拒绝（消息无
     法按分支归属迁移，复制共享末条会重新引入 R1 工具串线）；
   - 待执行分支节点 + 两侧通道都**无**流量 → **放行**：新图首次调用在产出
     任何消息前失败（如 LLM 超时），恢复等价于从头执行该分支，是合法重试
     （Codex 复核边界 1，已加回归）；
   - 待执行汇合屏障（Quality Gate）+ 共享通道有旧流量 → 拒绝：旧 OR 拓扑
     的触发状态与新 AND 屏障不等价（Codex 复核边界 2a，已加回归）；
   - Quality Gate 之后的节点：区分新旧——新图（分支通道有内容）正常恢
     复；旧版执行状态（分支通道全空 + 共享通道有流量）拒绝（**最终收口
     补充**，见文末"最终复核收口"）。
3. **past_context 污染终态**：断点上下文与按当前时间边界重算不一致时——
   决策未生成 → 原位刷新，刷新失败（含并行最后写入者的 `Ambiguous update,
   specify as_node`）→ 拒绝并保留断点；**决策已生成 → 直接拒绝**：旧决策
   可能已混入未来记忆，只擦 `past_context` 清理不掉决策文本，不能当正常
   分析结果返回（Codex 复核边界 2b，已加回归；替代上一轮"warning 后取回
   旧结果"的处理）。

所有拒绝路径都保留 SQLite 断点数据（回归有 `has_checkpoint` 断言），错误
信息说明不兼容原因与"清除该日期断点后重新开始"的处置方式。

### 测试与验证

`tests/test_review_round2_regressions.py`：8/8 通过（未改动审查方断言；
`test_data_failure_semantics.py` 的 FakeResp 补充 `raise_for_status()`
方法——依据：F1 起请求入口执行真实 HTTP 状态校验，假响应需具备该协议
方法，成功路径断言未变）。

新增回归（`tests/test_memory_log.py::TestPointInTimeGraphIntegration`，
全部走真实 `GraphSetup`/LangGraph/SQLite，无编译图 Mock）：

- `test_new_graph_first_call_failure_retry_not_refused`：首次 TimeoutError
  后重试成功（不误报不兼容）；
- `test_legacy_or_join_pending_quality_gate_refused`：旧 OR 拓扑停在
  Quality Gate → 拒绝且断点保留；
- `test_polluted_final_decision_refused_not_returned`：污染终态 → 拒绝且
  决策不再流转、断点保留；
- `test_resume_refreshes_stale_past_context` / `test_resume_keeps_context_
  when_already_consistent`：从 MagicMock 断言改写为真实 SQLite 图——单
  writer 刷新成功后恢复，质量检查恰好执行一次且看到干净上下文（替代上一
  轮"只断言 update_state 被调用"的 Mock 验证，该方式会被 `_safe_resume_
  checkpoint` 的 MagicMock 自动属性静默吞掉，测不到真实语义）。

边界补强（F1/F3）：真实 HTTP 502 状态码（响应体含可解析 JSON）按失败处
理、新浪业务错误传播、合法空数据缓存命中、`outcome_only` 档（收益可见/
复盘剔除）、`resolved ≤ as_of` 复盘完整注入、跨标的晚生成复盘剔除。

```
tests/test_review_round2_regressions.py: 8 passed
全量: 598 passed, 14 skipped, 52 subtests passed, 5 warnings
ruff check tradingagents web cli tests: 通过
git diff --check: 干净
```

跳过项仍为未安装的可选依赖（claude-agent-sdk / google Agent SDK）。未提交、
未推送、未删除任何真实缓存/断点/记忆。

## 最终复核收口（ZCode，2026-09-05，未提交）

Codex 最终复核发现 F4 仍遗漏一类旧断点：已**越过**旧 OR Quality Gate 的下
游断点（辩论/交易/风控/终态）此前被放行——仅凭"下游节点不读取 messages 通
道"称兼容是错的：旧 OR 拓扑下质量检查可能在部分分析师未完成时提前运行，
恢复后不完整的报告会直接进入多空辩论。已复现并收口。

### 收口实现

`_refuse_incompatible_legacy_checkpoint` 重构为**统一的拓扑证据规则**，不再
逐节点分类：

- 旧版图把分析师的工具调用与报告**全部**写进共享 `messages` 通道，分支通
  道必然全空；新版图只要分析师阶段真正运行过，至少一个选中分支的
  `{role}_messages` 通道就有内容。
- 因此：**分支通道全空 + 共享通道有 AI/Tool 流量 ⇒ 旧版执行状态**，无论停
  在哪个阶段（待执行分支节点、汇合屏障、已越过汇合点的辩论/交易/风控、执
  行完毕的终态）都拒绝恢复并保留断点——其报告完整性与屏障触发状态都无法
  证明与当前 AND 拓扑兼容。
- 判别不依赖"共享通道有 AI 消息"单独成立：新版图的 Trader/辩论节点同样写
  共享 AIMessage，但此时分支通道必有内容（任一分支通道非空 ⇒ 新版断点），
  不会误拒新图在 Trader 之后的合法断点。
- 新图首次调用失败（两侧通道均无流量）继续放行，合法重试不受影响。

### 收口回归（真实 SQLite 行为验证）

- `test_legacy_post_gate_partial_reports_resume_refused`：复现审查场景——
  旧图 Market Analyst 写共享 messages、QG 返回 "checked partial reports"、
  断点停在 Bull Researcher 前（`news_report=''`、分支通道全空），用当前
  `GraphSetup`（market+news）经真实 `prepare_graph_run` 恢复 → 明确拒绝、
  断点保留、Bull 未执行。
- `test_new_isolated_graph_resumes_after_trader`：当前 `GraphSetup` 完整图
  （真实工厂 + MockChat）跑到 Trader 之后（`interrupt_before` Aggressive，
  Trader 已写共享 AIMessage、分支通道有内容）→ 正常恢复并完成剩余风控与
  最终决策。

### 最终验证

```
tests/test_review_round2_regressions.py: 8 passed（审查方断言未动）
全量: 600 passed, 14 skipped, 52 subtests passed, 5 warnings
ruff check tradingagents web cli tests: 通过
git diff --check: 干净
```

两份文档中"Quality Gate 之后的恢复点不受影响/继续兼容"的失实声明已同步
修正。未提交、未推送、未删除任何真实缓存/断点/记忆。

## Codex 独立验收记录（2026-09-05）

ZCode 桌面端已完成修改。审查方检查了最终生产代码差异和测试改动，并独立
运行最终工作区版本，结果为 **600 passed、14 skipped、52 subtests passed、
5 warnings**；Ruff 与 `git diff --check` 均通过。最初新增的 8 个回归用例未
被删除、跳过或削弱，全部通过。

复核期间补充发现的首次模型调用失败重试、旧 OR 汇合点之后的断点、受污染
的旧最终决策，以及新版 Trader 之后正常恢复，均已有真实 LangGraph/SQLite
行为测试。本文 F1–F5 及这些补充边界在本次验证范围内完成修复。

交付限制：旧版共享消息拓扑的执行断点，以及无法安全清理的污染断点，采用
明确拒绝恢复并保留原数据的策略，需要重新开始相应日期的分析；没有实现无损
迁移。测试未连接真实行情或付费模型，14 个可选依赖跳过项不属于本次已验证
能力。代码与记录均留在当前工作区，Git HEAD 仍为 `3f82495`，未提交或推送。
