# 项目 Review 修复计划（供后续 AI 执行）

日期：2026-09-05  
审查基线：`9f195faa31e7dc40bbd6a4578238516f74f6042b`  
当前状态：✅ 已全部实施完成（2026-09-05）。三个提交：
- `2c17070` fix(graph): R1 并行分支消息/工具隔离 + R2 汇合屏障
- `f2e4a75` fix(data): R3 数据失败语义 + R5 失败缓存防护
- `9367754` fix(memory): R4 历史记忆时间边界
完整结果见文末「执行结果」。

## 1. 任务目标与执行约束

修复本次审查确认的 5 个正确性问题，补充能够在修复前失败、修复后通过的回归测试。重点保证并行分析能够取得自己的工具结果、最终决策使用完整报告、数据故障不会伪装成无风险事件、历史分析不会使用未来记忆。

执行要求：

- 开始前阅读当前分支代码和适用的 `AGENTS.md`，确认基线之后是否已有相关修改；以下行号只是审查时定位，以函数名为准。
- 保留用户已有修改，只处理本计划涉及的行为。不要顺带大规模格式化、升级依赖或重写无关模块。
- 先添加真实行为的回归测试，再修改实现。测试需要经过实际 `GraphSetup.setup_graph()` 和 LangGraph 执行路径，不能只检查节点拓扑，也不能用永远直接返回报告的假模型替代所有工具循环。
- 自动化验证使用假 LLM、假工具、临时目录与固定时间；不得依赖真实行情网络、付费 LLM 或用户 API Key。
- 缓存、记忆和 checkpoint 测试必须隔离到临时目录，不读写用户真实历史数据。不以删除用户数据库或记忆文件作为修复手段。
- 保留 CLI/Web 入口、角色模型配置、个股/指数模式，以及报告字段的对外兼容性。涉及内部状态变化时明确处理旧 checkpoint 的兼容性。
- 完成每项后在本文任务清单中打勾，补充实现摘要、实际测试结果和必要的兼容性说明；没有验证通过的事项不要标记完成。

## 2. 已有验证与修复顺序

审查时执行结果：

- `.venv/bin/python -m pytest -q`：527 passed、14 skipped、52 subtests passed、5 warnings。
- `.venv/bin/python -m ruff check tradingagents web cli tests`：通过。
- 跳过项主要涉及未安装的 Google / Claude Agent SDK 可选依赖；不能据此声称这些供应商已验证。
- 5 个问题均通过额外的离线最小用例验证。复现脚本当时通过临时 Python 进程执行，尚未保存为仓库回归测试。

| 顺序 | 编号 | 优先级 | 任务 | 依赖 |
|---|---|---|---|---|
| 1 | R1 | P1 | 隔离并行分支的消息与工具调用 | 无 |
| 2 | R2 | P1 | 等待所有选中分析师完成后再启动质量检查 | 与 R1 一起设计，先后验证 |
| 3 | R3 | P1 | 区分数据请求失败与成功但没有记录 | 无 |
| 4 | R5 | P2 | 禁止长期缓存失败结果，处理旧污染缓存 | 建议在 R3 的错误语义明确后实施 |
| 5 | R4 | P1 | 按分析时点过滤历史记忆与收益复盘 | 可独立实施；排在最后是为了按模块集中修改 |

建议形成三个可独立审查的提交：并行流程（R1/R2）、数据失败与缓存（R3/R5）、记忆时间边界（R4）。优先级表示缺陷影响，不要求为每项单独创建分支。

## 3. R1：并行工具节点读取了其他分析师的请求

### 代码位置

- `tradingagents/graph/trading_graph.py`：`_create_tool_nodes()`，约 437 行。
- `tradingagents/graph/setup.py`：分析师 fan-out 和工具循环。
- `tradingagents/agents/utils/agent_states.py`：`AgentState(MessagesState)`。
- `tradingagents/agents/utils/agent_utils.py`：`filter_analyst_messages()`、`create_msg_delete()`。
- `tradingagents/agents/analysts/`、`tradingagents/agents/index_agents.py`：分析师消息输入输出。

### 问题与复现证据

并行分析师向共享 `messages` 写入各自的 `AIMessage`。工具节点接收到合并后的消息，默认根据最后一条 AI 消息执行工具，不能保证那条消息属于当前分支。现有 `filter_analyst_messages()` 只过滤 LLM 输入，不能修复 ToolNode 的输入；按工具名称过滤也不能区分共用 `get_news` 等工具的多个角色。

已用真实 `GraphSetup`、真实 `ToolNode` 和两个假工具复现：

1. 选择 `market`、`news` 两个分析师，通过 `node_factories` 注入假节点。
2. 两个节点第一次调用分别返回 `market_data`、`news_data` 工具请求，第二次返回各自报告。
3. 两个工具节点只注册自己的工具，并记录实际执行次数。
4. 在 Quality Gate 后中断，检查工具执行和 ToolMessage。
5. 实际仅执行了 `news_data`；市场分支返回 `news_data is not a valid tool`，`market_data` 完全没有执行。

### 修复方案

- 首选让每个分析师的 LLM → 工具 → LLM 循环运行在拥有独立消息状态的子图中，父图只接收该分支的最终报告。
- 也可以使用明确按角色隔离的消息通道，但分析师、条件路由和 ToolNode 必须一致使用该通道；不能只修改过滤函数。
- 共享初始输入只包含标的、分析日期及必要配置。工具请求与响应按分支和 `tool_call_id` 配对。
- 如使用子图，核查 Web 进度、回调、调试事件和 checkpoint 的实际传播行为，必要时适配，不能假设嵌套节点事件与原来相同。

### 回归测试与验收

- [x] 两个分析师同时调用不同工具，两项工具各执行一次，无无效工具错误。
- [x] 两个分析师调用同名工具但参数、请求 ID 不同，各自只收到自己的响应。
- [x] 支持一条 AIMessage 内多个工具调用和连续多轮工具循环。
- [x] 一位分析师直接完成、其他分析师继续调用工具时，已完成报告不会被覆盖为空。
- [x] 完成或失败后恢复 checkpoint，不重复执行已确认完成的节点；消息请求与响应仍然配对。
- [x] 个股 7 分析师、指数 5 分析师、自选子集和单分析师均正常。

测试落点：优先扩展 `tests/test_graph_parallelism.py`，并按需要补充 `tests/test_checkpoint_resume.py`、`tests/test_index_support.py`、Web 进度/调试回归测试。

## 4. R2：Quality Gate 没有等待全部分支完成

### 代码位置

- `tradingagents/graph/setup.py`：约 180–181 行，循环内的 `workflow.add_edge(current_clear, "Quality Gate")`。
- `tradingagents/agents/quality_gate.py`：报告汇总入口。
- `web/runner.py`：报告完成状态识别，核查是否会把提前产出的下游结果当作最终结果。

### 问题与复现证据

为每个分支单独添加通向 Quality Gate 的边，不等价于等待所有分支完成。分支工具循环轮数不同时，Quality Gate 可能提前运行，后续分支到达又可能重新触发下游。

已验证：市场节点直接返回报告，新闻节点先调用一次工具再返回报告。第一次进入 Quality Gate 时 `market_report` 已存在，`news_report` 仍为空。该问题取决于图执行步数，不能仅靠线程 sleep 模拟耗时差异。

### 修复方案

- 为所有选中分析师的完成节点建立真正的汇合屏障，例如一次性添加 `add_edge([完成节点列表], "Quality Gate")`，结合 R1 的最终图结构实施。
- 若 R1 改为子图，可在父图对分析师子图完成节点建立汇合屏障。
- 屏障只等待本次选中的分析师，不能等待未启用角色；质量检查之后的多空辩论、交易计划、风控和最终决策只启动一条流程。
- 明确分支失败策略：如果传播异常，则不能伪造完整报告或进入成功决策；如果允许降级，则必须以显式失败状态完成分支，供质量检查识别。

### 回归测试与验收

- [x] 通过真实 GraphSetup 构建 0、1、3 次工具循环的不同分析师分支，验证 Quality Gate 首次执行时报告已齐全。
- [x] 对 Quality Gate 和下游节点计数，完整成功执行中 Quality Gate 与最终决策均只执行一次。
- [x] 单分析师、自选子集、指数默认角色集合不死锁。
- [x] 分支执行失败时，不启动声称报告完整的下游决策。
- [x] 部分分支完成后失败并恢复，最终报告齐全且下游无重复执行。

测试落点：`tests/test_graph_parallelism.py`、`tests/test_checkpoint_resume.py`。保留有价值的拓扑断言，但同步修改它们，使其验证真正的屏障而不是原有错误连线。

## 5. R3：接口故障被解释为没有风险事件

### 代码位置

- `tradingagents/dataflows/a_stock.py`：`_eastmoney_datacenter()`，约 599–607 行。
- 同文件：`get_dragon_tiger_board()`、`get_lockup_expiry()` 及其他调用该查询函数的代码。
- `tradingagents/dataflows/cache_utils.py`：`robust_api_call()`。

### 问题与复现证据

`_eastmoney_datacenter()` 在重试失败后返回 `[]`，与成功查询但没有记录完全相同。下游据此输出确定性的“没有事件”结论。

离线复现步骤：隔离缓存，patch `_em_get` 始终抛出 `TimeoutError`，禁用测试中的重试等待，再调用 `get_lockup_expiry("600519", "2026-09-04")`。

实际输出包含：

```text
无历史解禁记录。
未来 90 天无待解禁。
```

### 修复方案

- 内部数据层用明确异常或结构化状态区分成功、成功空结果、失败；推荐让重试耗尽后的异常传播至工具边界统一转换为数据缺失信息。
- 正确识别 HTTP 失败和供应商 JSON 业务错误，不能仅凭 JSON 可解析就认定成功。先核查实际响应协议和现有 fixtures，再确定有效空结果的判断规则。
- 对工具公开的输出保留现有字符串契约也可以，但错误必须明确标记，例如 `[数据缺失: 解禁查询失败]`，不能落入“无待解禁”的成功分支。
- 支持部分失败：历史记录查询成功、未来日历查询失败时，仅保留已确认结果，并单独指出缺失部分。
- 审核所有 `_eastmoney_datacenter()` 调用点，避免修改后引入未初始化变量、异常被吞掉或遗漏错误提示。

### 回归测试与验收

- [x] 网络超时、HTTP 失败、供应商业务失败均显示数据缺失，不能显示“无待解禁/未上龙虎榜”。
- [x] 确实成功且没有记录时，仍可以输出没有事件。
- [x] 首次失败、重试成功时返回正常结果，重试次数有上限。
- [x] 部分查询失败不会使已有成功数据消失，也不会声称数据完整。
- [x] 质量检查能够识别这些缺失提示，不把失败报告判作完整数据。

测试落点：扩展数据工具测试或新增 `tests/test_data_failure_semantics.py`；保留 `tests/test_cache_and_resilience.py` 对重试的隔离验证。

## 6. R5：错误字符串被长期持久化缓存

### 代码位置

- `tradingagents/dataflows/cache_utils.py`：`cached_data()`，约 126–163 行。
- `tradingagents/dataflows/a_stock.py`：所有 `@cached_data` 装饰函数，尤其财报三表、龙虎榜、解禁、行业对比。

### 问题与复现证据

缓存有效性只检查 `result is not None`，而工具会将异常转换成字符串。错误字符串因此进入内存和默认 24 小时有效的磁盘缓存。内存 TTL 到期并不意味着重新获取源数据，因为下一步仍可能命中磁盘。

已验证：

1. 用临时 `DiskCache` 和新的 `TTLCache` 替换全局缓存。
2. patch `_get_financial_report_sina` 抛出超时，调用 `get_balance_sheet()`，获得 `Error retrieving balance sheet ...`。
3. 清空内存缓存，将数据源改成返回有效 DataFrame。
4. 相同参数再次请求，结果仍是旧错误，恢复后的数据源调用次数为 0。

### 修复方案

- 与 R3 一起确定成功状态；推荐缓存成功的原始数据，在缓存之外格式化工具结果和错误提示。
- 如保留现有装饰器，可增加明确的可缓存判断，但不能只匹配某一种英文错误前缀；必须覆盖实际调用点的中文失败、数据缺失和部分失败输出。
- 失败默认不进入长期缓存。确需防止短时间重复请求时，使用独立且明确的短期失败策略，不能沿用成功结果的磁盘 TTL。
- 成功空结果与故障分开处理，空结果是否缓存及 TTL 按数据类型明确设置。
- 对历史版本已经写入的污染缓存设计迁移方式，例如升级缓存命名空间/版本或读取时验证；仅修改新写入条件不能消除已有错误结果。不要批量删除用户无关缓存。
- 测试 patch 全局单例为临时缓存；模拟过期用固定/可控时钟，不实际等待一小时。

### 回归测试与验收

- [x] 首次错误、随后数据源恢复时，相同参数能再次取数并返回成功。
- [x] 清空内存或模拟进程重启后，不会从磁盘恢复长期失败结果。
- [x] 成功结果仍可命中内存和磁盘，过期后能重新取数。
- [x] 成功空结果与请求故障行为不同，TTL 有明确测试。
- [x] 旧格式的已污染缓存不会继续阻塞数据恢复。

测试落点：`tests/test_cache_and_resilience.py`，补充至少一个经过实际财报/解禁函数的集成式离线测试，不能只测试返回整数的装饰器。

## 7. R4：历史分析使用了未来记忆

### 代码位置

- `tradingagents/graph/trading_graph.py`：`prepare_graph_run()` 约 702–704 行、`_resolve_pending_entries()`、`_fetch_returns()`。
- `tradingagents/agents/utils/memory.py`：`get_past_context()`、读取/写入/批量更新日志的方法。
- `tradingagents/agents/managers/portfolio_manager.py`、`tradingagents/agents/index_agents.py`：`past_context` 的消费位置。

### 问题与复现证据

`get_past_context(company_name)` 没有分析日期参数，只筛选已完成收益回填的记录。这些决策、收益与复盘会进入最终决策提示词，即使记录产生于分析日之后。

已在临时日志中写入并回填一条 `2026-08-01` 的决策、`+20%` 收益和八月复盘。无论将运行日期设为何时，当前读取接口都会返回该内容。因此分析一月份时也可能读到八月份的结果。

### 修复方案

- 为记忆读取增加必需或由运行入口明确提供的 `as_of` 分析时点，同时过滤同标的历史和跨标的经验；CLI、Web、个股、指数均使用相同规则。
- 不仅比较决策日期，还要比较收益结果的可获知时间。决策在分析日前、但持有期结束在分析日后时，其收益和复盘仍属于未来信息。
- 保存收益窗口实际结束时间，以及实现所需的复盘生成/可用时间；明确复盘可用时间的定义，不能用自然日简单加 `holding_days` 代替实际行情日期。
- 检查 `_resolve_pending_entries()` 在历史运行前自动查询收益、生成复盘的路径，避免绕过时间限制或把刚取得的未来结果注入当前历史运行。
- 旧日志缺少时间元数据时采用保守行为：对无法证明当时可用的收益和复盘，从历史上下文中排除。保留原日志，并说明实时分析下的兼容策略。
- 对日期解析失败、同日边界和时区制定明确规则；当前输入若只有日期，说明假定分析发生在盘前还是盘后，并保持数据和记忆规则一致。
- 旧 checkpoint 可能已包含未经时间过滤的 `past_context`。历史恢复路径也要重新验证，或给出明确的不兼容提示，不能无条件沿用污染状态。

### 回归测试与验收

- [x] 分析一月份时，不返回八月份的决策、收益和复盘。
- [x] 同标的与跨标的记忆均遵守时点限制。
- [x] 决策日期在分析日前、收益结束日在分析日后时，不能泄漏未来收益或复盘。
- [x] 有充分时间证据且当时已可用的历史经验仍能正常注入。
- [x] 缺少时间元数据的旧日志在历史模式下保守处理，不破坏日志读写与新记录追加。
- [x] 历史运行触发收益回填以及从 checkpoint 恢复时，不会重新引入未来信息。

测试落点：`tests/test_memory_log.py`、`tests/test_lookahead_guard.py`、`tests/test_checkpoint_resume.py`；使用临时日志和固定日期，不访问真实收益数据。

## 8. 最终验证与交付

针对修改先运行相关测试，全部修复后运行一次完整验证：

```bash
.venv/bin/python -m pytest -q tests/test_graph_parallelism.py tests/test_checkpoint_resume.py tests/test_index_support.py
.venv/bin/python -m pytest -q tests/test_cache_and_resilience.py tests/test_memory_log.py tests/test_lookahead_guard.py
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check tradingagents web cli tests
git diff --check
```

新增测试文件应包含在对应的定向验证中。若目标环境没有 `.venv`，按项目已有环境方式运行等价命令；不要为了测试擅自升级全项目依赖。

最终交付清单：

- [x] R1 并行消息/工具隔离完成。
- [x] R2 汇合屏障与下游单次执行完成。
- [x] R3 请求失败与成功空结果分离完成。
- [x] R5 失败缓存策略及旧污染缓存处理完成。
- [x] R4 历史记忆的时间边界与恢复路径完成。
- [x] 每项均有修复前能失败、修复后能通过的测试，并说明测试覆盖的实际触发条件。
- [x] 完整测试和 Ruff 通过，列明跳过项及原因。
- [x] 记录旧 checkpoint、记忆日志和缓存格式的兼容行为。
- [x] `git diff` 只包含本计划相关修改，没有用户真实缓存、收益日志、API Key 或本地配置。

后续 AI 完成后，请报告：每个编号修复了什么、对应提交/文件、实际测试结果，以及仍未解决的限制。不要仅用“现有测试全部通过”作为问题已经修复的证据。

## 9. 执行结果（2026-09-05）

### R1 + R2（提交 `2c17070`）

实现：采用**按角色隔离的消息通道**（文档备选方案）：`AgentState` 新增 7 个
`{role}_messages` 分支通道；`GraphSetup` 对分析师节点与 ToolNode 统一做通道映射
包装（`_branch_isolated_analyst_node` / `_branch_isolated_tool_node`），个股与
指数共 12 个节点工厂零改动；条件路由（`ConditionalLogic`）读对应分支通道。
R2 为 `add_edge([全部 Msg Clear 节点], "Quality Gate")` 的 AND 汇合屏障；分支
异常保持向上传播，不伪造完整报告。适配：`web/runner.py` 进度检测扫描分支通道；
CLI debug 流式输出按通道增量打印。

测试：`tests/test_graph_parallelism.py` 新增 14 个行为级回归（真实
`GraphSetup.setup_graph()` + 真实 `ToolNode` + 真实 checkpoint 恢复），修复前
8 个失败（`market_data` 执行 0 次、跨分支 invalid tool、QG 提前触发导致
`InvalidUpdateError`、恢复后重复执行）；另新增 `tests/test_web_progress_channels.py`
（2 例）。R2 混合轮数场景用全 7 分析师 + 通过硬检查的长报告驱动 QG 的 LLM 复审。

兼容性：旧 checkpoint（无分支通道）恢复点在分析师中间时会重跑该分支（消息丢
失、报告重算，行为正确）；恢复点在 Quality Gate 及之后不受影响。拓扑断言测试
无需修改（join 屏障的可视图与逐条边相同）。

### R3 + R5（提交 `f2e4a75`）

实现（R3）：`_eastmoney_datacenter` 识别供应商业务错误（`success=false` 或无
`result` 载荷），网络/HTTP/业务失败在重试耗尽后抛出异常；调用方（解禁/龙虎榜）
转换为 `[数据缺失: ...]` 显式标注，成功空结果（`success=true` 且无记录）才输出
"无记录"。部分失败保留已确认数据并单独标注；消除首段失败时 `data`/`buy_data`/
`sell_data` 未初始化的隐患。

实现（R5）：`cached_data` 三层防护——(1) 已知失败标记（`[数据缺失` /
`Error retrieving` / `查询失败`）不写入内存与磁盘；(2) 磁盘命名空间升级
`{namespace}-v2`，旧版污染条目留在原命名空间天然失效（不删除用户缓存）；
(3) 读取时对命中值做失败标记二次验证。成功空结果（如 `No data found`）照常
缓存，TTL 不变。

测试：新增 `tests/test_data_failure_semantics.py`（12 例），扩展
`tests/test_cache_and_resilience.py`（10 例，含固定时钟 TTL、进程重启模拟、
真实 `get_balance_sheet` 集成）。修复前 18 例失败。

### R4（提交 `9367754`）

实现：`get_past_context(ticker, as_of=...)` 时点过滤——决策日 > as_of 完全隐
藏；收益窗口实际结束日（`end=`）≤ as_of 完整注入；窗口跨 as_of 或旧格式缺时
间元数据时仅注入决策文本并注明（保守规则：无法证明可用即视为未来）。时间语
义：as_of = 该交易日收盘后（同日决策与同日收盘的收益可见）。`_fetch_returns`
返回窗口实际结束行情日（非日历推算），回填写入 `end=`/`resolved=` 元数据。
CLI/Web/个股/指数统一经 `prepare_graph_run(as_of=trade_date)`；历史运行触发
的收益回填经同一过滤不反注入；checkpoint 恢复时重算 `past_context` 并在污染
时以 `update_state` 覆盖（一致则不动，读取失败降级为日志提示）。

测试：`TestAsOfContextFiltering`（10 例）+ `TestPointInTimeGraphIntegration`
（5 例），修复前 15 例全部失败。

### 最终验证

```
tests/test_graph_parallelism.py + test_checkpoint_resume.py + test_index_support.py:
    112 passed
tests/test_cache_and_resilience.py + test_memory_log.py + test_lookahead_guard.py:
    124 passed
全量: 581 passed, 14 skipped, 52 subtests passed, 5 warnings
ruff check tradingagents web cli tests: 通过
git diff --check: 干净
```

跳过项均为未安装的可选依赖（claude-agent-sdk / google Agent SDK），与本计划
修改无关。`git diff` 仅含本计划相关文件，未触碰用户缓存、收益日志或配置。

### 未解决的限制

- 旧格式 resolved 条目（无 `end=`）在历史模式下只注入决策文本：随时间推移被
  新回填条目自然替代，不做一次性迁移。
- 旧 checkpoint 的分析师分支消息不可恢复（通道当时不存在）：恢复点在分析师中
  间时该分支重跑；报告字段照常保留。
- R3 的空结果判定依赖东财 `success=true + result=null` 协议；若供应商未来变更
  无数据响应形态，`_eastmoney_datacenter` 会将其判为失败并显示数据缺失（保守
  方向失效，不会伪装成"无事件"）。
