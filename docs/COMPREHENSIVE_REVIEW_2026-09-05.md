# 全工程审核与整改任务书（2026-09-05）

## 整改后的状态（2026-09-06）

ZCode 已按本文方案分四批完成 15 项问题的代码整改，Codex 完成逐批代码复核、独立失败复现与修后验收。最终默认回归为 **753 passed、14 skipped、52 subtests passed、5 warnings**，包含八份独立审计脚本的 71 例；Ruff 与 `git diff --check` 通过。改动保留在当前工作区，未提交、未推送。

完整验收、审核中退回修正的边界和未验证项目见 [ZCode 编码与 Codex 独立验收记录](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/docs/ZCODE_IMPLEMENTATION_TRACKER_2026-09-05.md>)。SDK 子进程权限、Gemini、Windows 文件锁、上游实时行情及 Docker 镜像不属于本机已完成的动态验收；旧记忆没有自动迁移。

以下保留**整改前的证据与原始任务书**，其失败数与代码行号对应当时基线，不表示当前仍然失败。当前统一验证入口为：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check tradingagents web cli tests docs/audit_*.py
git diff --check
```

## 初始审核结论（整改前）

本轮确认 **15 项问题：7 项 P1、8 项 P2**。主要集中在入口行为不一致、历史数据时点、并发隔离、收益结算和结果持久化。修复上一轮列出的五项问题，并不意味着这些独立路径也已经正确。

审核基线为 Git HEAD `3f82495` **加当前未提交的第二轮修复**。本轮只新增审核文档和独立复现用例，不修改业务实现。请保留当前工作区已有改动，基于此工作区修复，不能只检出 HEAD 后宣称复现不到。

证据与复现用例：[docs/audit_repros_2026_09_05.py](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/docs/audit_repros_2026_09_05.py>)。编号 A01–A15 与下面的问题对应。

### 验证结果

| 检查 | 本轮实际结果 |
|---|---|
| 原有默认测试集 `.venv/bin/python -m pytest -q` | 600 passed、14 skipped、52 subtests passed、5 warnings |
| 静态检查 `.venv/bin/ruff check tradingagents web cli tests` | 通过 |
| `git diff --check` | 通过 |
| 独立审核用例 | 20 个预期行为断言失败，对应 15 项问题 |

初始独立用例位于 `docs`，当时不会被默认的 `testpaths = ["tests"]` 收集。**以下失败是整改前缺陷的复现结果。** 整改后已将 `docs/audit_*.py` 纳入默认收集，文件保留原位置以免产生两份测试或失效引用。其中 A15 检查实际生成的 SDK 参数，并用官方权限语义确认后果；没有运行有权限的 SDK 子进程。

执行命令（项目根目录）：

```bash
.venv/bin/python -m pytest docs/audit_repros_2026_09_05.py -q --tb=short
```

用例使用模拟 HTTP/LLM 和临时目录；并发问题通过受控交错复现。用例表达业务预期，重构接口时可以同步调整测试接缝，但不能删除关键断言来获得“通过”。

### 范围与证据边界

已检查：图构建、并行分析师与汇合、断点恢复、工具路由与缓存、A 股/指数数据、模型客户端与订阅适配、记忆日志与绩效、CLI、Web 启动/恢复/停止、历史列表、报告导出，以及安装和 Docker 配置。

本轮没有调用付费模型，没有对实时行情源进行全面联网验收，也没有构建 Docker 镜像。当前环境缺少 Gemini 和 Claude Agent SDK 的可选依赖，相关 14 个测试被跳过；不能将其视为验证通过。没有把缺少 CI、文件过长、风格偏好等直接计入缺陷数量。

## 问题清单

P1 表示应优先修复，会造成分析污染、数据丢失、主要功能失效或权限边界失效；P2 表示存在明确功能或结果正确性缺陷。

| 编号 | 等级 | 问题 | 主要影响 |
|---|---|---|---|
| A15 | P1 | SDK 的 allowed_tools 被当成工具禁用列表 | 订阅模式仍可获得文件/命令执行能力 |
| A02 | P1 | 默认模型地址固定到智谱 | 切换供应商后请求、认证信息发往错误端点 |
| A01 | P1 | CLI 绕过统一运行生命周期 | 断点、交易记忆、统一结果落盘不生效 |
| A03 | P1 | 北向资金及行业排名忽略历史时点 | 今天/未来数据进入历史报告 |
| A04 | P1 | 财报按报告期末而非披露日期过滤 | 尚未公开的财报被用于历史分析 |
| A05 | P1 | 运行配置保存为进程级全局变量 | 并发任务互换数据源、回溯窗口、语言和缓存路径 |
| A06 | P1 | 记忆日志更新覆盖并发追加 | 已成功保存的另一笔决策消失 |
| A07 | P2 | 超额收益按各自行号比较 | 停牌/缺失行情时比较不同日期，正负号可反转 |
| A08 | P2 | 五日收益在一天后永久结算 | 样本期限取决于用户何时再次运行 |
| A09 | P2 | Web 恢复任务使用侧栏当前类型 | 指数断点可能由个股图恢复，反之亦然 |
| A10 | P2 | 未启用分析师被判为失败 | 精简团队和指数模式被错误降低数据质量 |
| A11 | P2 | 旧报告隐藏并删除新失败任务索引 | 同标的同日期重跑后失去恢复入口 |
| A12 | P2 | 历史列表忽略自定义输出目录 | 报告保存成功却不出现在历史列表 |
| A13 | P2 | 东财“串行限流”没有锁 | 并发分析师仍同时发请求 |
| A14 | P2 | 状态落盘与展示字段不一致 | 实时导出缺交易员计划，历史重载丢质量结论 |

## P1：优先修复

### A15 — 订阅 SDK 未真正禁用内置工具

**位置：** [tradingagents/llm_clients/claude_agent_sdk_client.py:445](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/llm_clients/claude_agent_sdk_client.py:445>)，同文件 466–474 行工具循环配置。

代码把 `allowed_tools=[]` 注释为“no built-in tools”，同时设置 `permission_mode="bypassPermissions"`；有投研工具时仅将 `allowed_tools` 换成 MCP 工具名，没有设置 `tools` 或禁用内置工具。

**原因与影响：** `allowed_tools` 控制自动批准，不控制其他工具是否可用。官方文档明确说明，未列出的工具仍存在；在 `bypassPermissions` 下，Bash、Write、Edit 等仍会被批准。启用订阅模式后，原本只应读取投研数据的节点可能在 CLI 子进程的权限范围内操作文件或执行命令，超出了本模块声明的能力范围。

**证据：** 不带/带 MCP 工具两种配置分别得到：

```text
tools=None, allowed_tools=[], permission_mode=bypassPermissions
tools=None, allowed_tools=[mcp__astock_tools__get_stock_data], permission_mode=bypassPermissions
```

权限语义已核对 [Anthropic 官方权限文档](https://code.claude.com/docs/en/agent-sdk/permissions#allow-and-deny-rules)。没有尝试实际执行命令或读取私有文件。

**修复任务：** 明确配置可用工具集合，移除不需要的内置工具；确保研究节点仅能调用绑定的投研 MCP 工具，纯文本/结构化节点不能调用内置工具。评估是否仍需 bypass 模式，并核对支持的 SDK 版本。不要仅修改注释或继续缩短 `allowed_tools`。

**验收：** 对普通、结构化、工具循环三条路径检查传入 SDK 的工具集合；安装受支持的 SDK 后，在隔离的临时工作目录中验证非授权内置工具不可调用、投研 MCP 仍可使用。任何动态验证都应使用专用测试环境。

### A02 — 切换模型供应商后仍使用智谱地址

**位置：** [tradingagents/default_config.py:26](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/default_config.py:26>)；[web/app.py:168](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/app.py:168>)；[tradingagents/llm_clients/openai_client.py:229](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/llm_clients/openai_client.py:229>)。

未设置 `BACKEND_URL` 时，默认值仍是 `https://open.bigmodel.cn/api/coding/paas/v4`。Web 的空白地址会回落到这个值，再优先于每个客户端自己的默认端点。侧栏明确提示“留空用所选供应商官方地址”，实际行为与提示相反。

**复现：** 清除环境变量影响，选择 DeepSeek、地址留空，实际生成的 `backend_url` 仍为智谱地址。Python API 从 DEFAULT_CONFIG 复制后仅修改供应商和模型也受影响。

**影响：** 请求失败，或使用其他供应商的 API Key 向智谱端点发起认证。本轮只检查配置，没有发送真实凭据。

**修复任务：** 通用默认地址恢复为 None；如需要保留智谱 Coding 地址默认值，应放在智谱专属分支。明确自定义地址、环境变量与供应商默认值的优先级，切换供应商不能意外沿用隐含的旧地址。

**验收：** 至少覆盖 GLM、DeepSeek、OpenAI、Ollama；未显式指定地址时使用各自默认值；显式网关配置仍有效。

### A01 — CLI 绕过统一启动、完成和关闭流程

**位置：** [cli/main.py:1259](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/cli/main.py:1259>)、[cli/main.py:1371](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/cli/main.py:1371>)；参照 [tradingagents/graph/trading_graph.py:659](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/graph/trading_graph.py:659>) 和 [web/runner.py:295](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/runner.py:295>)。

CLI 虽把 `checkpoint` 写入配置，却直接调用 `propagator.create_initial_state()` 和 `graph.graph.stream()`，结束时只调用 `process_signal()`。构造函数编译的图没有 checkpointer；真正加载断点、回填记忆和注入历史上下文都发生在被跳过的 `prepare_graph_run()` 中。保存决策和统一 JSON 报告则在被跳过的 `finalize_graph_run()` 中。

**复现：** 用离线图执行真实 `run_analysis(checkpoint=True)`，三个生命周期方法调用次数全部为 0。

**影响：** `--checkpoint` 没有承诺的保存/恢复能力；CLI 不读取/回填/写入交易记忆，也不产生 Web 历史列表识别的统一 JSON。CLI 自己保存的 Markdown 和消息日志不等价于这些状态。另一个同入口遗漏是消息日志仍只读共享 `messages`，看不到上一轮改成分支通道的分析师工具消息。

**修复任务：** CLI 接入统一的 prepare / stream / finalize / finally-close 流程；支持恢复时 initial_state=None；按各分支通道增量采集工具消息。保留交互界面和报告导出。

**验收：** CLI 在中途失败后第二次执行能从真实 SQLite 断点继续，已完成分析师不重复调用；成功后写入一笔决策并清理对应断点，失败时保留；实时与历史运行都注入正确时点的上下文；工具日志包含分支调用。

### A03 — 北向资金和行业排名仍有历史数据泄漏

**位置：** [tradingagents/dataflows/a_stock.py:1924](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/dataflows/a_stock.py:1924>)、[tradingagents/dataflows/a_stock.py:2497](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/dataflows/a_stock.py:2497>)；相关聚合入口 [tradingagents/market_flow.py:86](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/market_flow.py:86>)。

`get_northbound_flow(curr_date)` 用指定日期作标题，却无条件获取当前分钟数据、生成 bullish/bearish 信号；本地历史也直接拿最近 20 条，没有按 curr_date 截断。`get_industry_comparison(ticker, trade_date)` 同样将实时行业表现放在历史日期标题下面，且没有当前快照限制说明。独立资金流报告又组合了北向和无日期约束的板块排名。

**复现：** 请求 2026-01-15 时，模拟的当前净流入 123456，以及日期为 2099-01-01 的缓存行，均进入输出。行业工具把 FUTURE_SECTOR 及涨幅 88% 放入 2026-01-15 的报告，未提示数据不属于该日。

**修复任务：** 历史请求跳过实时分钟段，缓存记录先按日期过滤再取窗口；没有历史行业快照时返回明确缺失，或至少沿用已有 `_snapshot_notice` 的禁止引用说明，不把实时涨幅当作历史证据。独立资金流报告遵循相同规则。

**验收：** 分析日之后的数据不参与表格、平均值、信号或总结；当前日期请求仍保留合法实时数据；无历史覆盖时明示缺失。

### A04 — 财报的会计报告期被误当成信息可知日期

**位置：** [tradingagents/dataflows/a_stock.py:1277](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/dataflows/a_stock.py:1277>)，影响资产负债表、利润表和现金流量表三个工具。

过滤条件仅为 `报告日 <= curr_date`。报告期末不等于实际披露时间，因此在两个日期之间做历史分析时，会获得当时尚未公开的财务数据。仅在输出头部打印“Data retrieved on”不能弥补这个问题。

**复现：** 构造报告期 2026-06-30、披露日 2026-08-30 的报表，分析日为 2026-07-10；三种报表均未被过滤。这里的“公告日期”是测试夹具字段，用于明确可知性条件，并非声称已验证新浪线上接口一定提供同名字段。

**修复任务：** 以实际披露时间建立 as-of 过滤；核对数据源是否提供可靠的披露/修订时间。源头无法证明可知时间时，历史分析应缺失或显式禁用该快照。不能用固定天数偏移猜测真实公告时间，也不能允许重述版本悄悄回填到过去。

**验收：** 披露日前不可见，披露日后可见；披露时间缺失/无效时保守处理；年度、季度及三种报表均覆盖；当天分析仍可取到合法数据。

### A05 — 多个分析任务共享可变全局配置

**位置：** [tradingagents/dataflows/config.py:14](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/dataflows/config.py:14>)；[tradingagents/graph/trading_graph.py:185](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/graph/trading_graph.py:185>)；读取点包括 [tradingagents/agents/analysts/market_analyst.py:18](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/agents/analysts/market_analyst.py:18>) 和 [tradingagents/dataflows/interface.py:191](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/dataflows/interface.py:191>)。

每个图构造时执行 `set_config(self.config)`，覆盖同一进程的 `_config`。分析师和工具在实际执行时动态读取它。Web 多会话会创建后台线程；即使串行构造两个图、再执行第一个，也会读到后一个配置。

**复现：** A 设置 English、5 天窗口；B 设置 Chinese、90 天；A 随后读到 Chinese、90 天。无需真实模型调用就能确定串扰。

**影响：** 语言、分析窗口、数据供应商和缓存目录可由另一个任务改变。调用方保留自己的 graph.config 不足以隔离，因为底层函数读的不是它。

**修复任务：** 将配置作为运行上下文或显式依赖传递；创建图时固定节点的必要配置。若使用 ContextVar，要验证 LangGraph 工作线程和 SDK 的线程桥接确实传播上下文。仅给 set_config/get_config 加互斥锁无法消除两次调用之间的串扰。

**验收：** 两个配置不同的图并发执行，真实分析师提示词与工具路由始终使用各自配置；先构造 A、再构造 B、最后执行 A 同样正确。

### A06 — 原子替换文件仍会丢失并发决策

**位置：** [tradingagents/agents/utils/memory.py:227](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/agents/utils/memory.py:227>)、[tradingagents/agents/utils/memory.py:268](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/agents/utils/memory.py:268>)；同文件 `store_decision` 与单条 `update_with_outcome`。

结果回填先读完整日志，在内存中改写，再用临时文件 replace；这一整个读改写过程没有事务锁。另一个任务在“读完旧文件”和“替换文件”之间追加决策后，追加内容会被旧快照覆盖。

**复现：** 两个线程分别回填 600519 和追加 000001；追加成功后，最终文件只剩 600519。用例在读取完成后控制线程顺序，因此不是依赖概率的压力测试。两个回填还共用同一个 `.tmp` 文件名，存在互相覆盖/替换失败的额外风险。

**修复任务：** 所有追加、单条回填、批量回填和轮转遵循同一事务边界。可采用支持事务的存储，或跨实例/跨进程共享的文件锁与独占临时文件；不应只把临时文件改为唯一名称。模型反思调用应放在锁外，提交时重新读取并核对条目状态。

**验收：** 追加与回填、两个回填、重复决策并发时无丢失、无重复、无临时文件冲突；覆盖两个日志对象和两个进程；发生中断后文件仍可读。

## P2：明确的正确性问题

### A07 — 股票和基准未按相同日期计算 alpha

**位置：** [tradingagents/graph/trading_graph.py:555](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/graph/trading_graph.py:555>)；Yahoo 分支同文件 590–607 行。

两张表各自排序，再以相同 `iloc[actual_days]` 取值，没有对齐起止日期。股票停牌或数据缺行时，“第 N 行”不是同一个交易日。

**复现：** 股票 1 月 5 日到 8 日涨 10%，基准同窗口涨 30%，应得 alpha=-20%；当前代码拿基准 1 月 6 日的 +1%，返回 **+9%**，正负号反转。

**修复与验收：** 先确定实际评估起止日，再取这两个日期的股票和基准价格；明确缺失端点与停牌政策，不应静默改用不同日期。原生和 Yahoo 两条路径都覆盖。回填的 outcome_end 必须覆盖实际使用的所有价格日期。

### A08 — 固定持有期被静默缩短并永久定稿

**位置：** [tradingagents/graph/trading_graph.py:556](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/graph/trading_graph.py:556>)、[tradingagents/graph/trading_graph.py:628](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/graph/trading_graph.py:628>)。

默认请求五日收益，但只要两条价格数据就用 `min(...)` 得到 1 天，立即生成反思并将 pending 改为已结算。后续不会再更新成五日结果。

**复现：** 只提供 1 月 5 日和 6 日价格，调用实际 `_resolve_pending_entries` 后 pending 为空，日志记录 1d。

**影响：** 同一决策在次日重跑与一周后重跑，得到不同的永久评估窗口；绩效与反思由用户操作频率决定。

**修复与验收：** 未达到指定期限时保持 pending；需要临时收益时用独立字段记录，不覆盖最终结果。达到完整窗口后只结算一次；较长假期、停牌、上市数据不足均覆盖，并与 A07 共同明确期限口径。

### A09 — Web 恢复类型取错来源

**位置：** [web/app.py:184](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/app.py:184>)、[web/app.py:284](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/app.py:284>)；[web/components/sidebar.py:435](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/components/sidebar.py:435>)；[web/runner.py:266](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/runner.py:266>)。

未完成任务按钮已经在 start_req 中写了正确的 analysis_type，页面也用它构建进度阶段；实际传给后台的 `_build_config()` 却从当前侧栏 session_state 读取类型。

**复现：** 当前侧栏为“个股”，恢复请求为 `000001.SH / 指数`，生成配置仍为 `instrument_type=stock`。后台会据此选择个股图，进度页面与实际图不一致。反向切换也有同类问题。

**修复与验收：** 一次性从任务请求生成确定的运行配置，同时用于图和进度。恢复指数/个股时，即便侧栏保持相反类型，实际图仍正确。建议保存恢复所需的分析师集合、窗口和配置版本，拒绝不兼容的配置混用；这一建议的全部组合尚未逐项动态复现。

### A10 — 主动关闭的分析师被计为 F 级

**位置：** [tradingagents/agents/quality_gate.py:130](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/agents/quality_gate.py:130>)、[tradingagents/agents/quality_gate.py:145](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/agents/quality_gate.py:145>)。

质量检查固定遍历七个角色，没有本次启用集合。只选技术分析师时，其余六个主动未运行的角色都被评为 F；fail_count 达阈值后还跳过 LLM 复审。指数预设中的两个不适用角色也被当成空报告失败。

**复现：** 唯一启用的 market 报告通过 A 级硬检查，仍得到六个 F 以及“多数报告未通过硬检查”。

**修复与验收：** 质量门控显式接收启用/适用集合，未启用标记为未参与或不适用，不计失败；启用后返回空报告仍算失败。覆盖单角色、任意子集、七角色、指数五角色。

### A11 — 历史成功结果会抹掉新失败任务的恢复入口

**位置：** [web/history.py:197](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/history.py:197>)、[web/history.py:203](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/history.py:203>)。

`get_incomplete_history()` 只要发现同 ticker/date 的完成报告就过滤新任务，并把过滤结果写回索引。开始 fresh 重跑只清断点和未完成索引，不删除旧报告，所以新一轮运行中途失败时也会被旧报告认作已完成。

**复现：** 先创建旧完成报告，再记录同标的同日期的新 error 任务并提供 step=8 的断点；查询未完成任务返回 []，索引被清空。

**修复与验收：** 为运行引入身份或可靠的开始/完成顺序，只有对应那一次运行完成才删除未完成记录。有旧成功报告、新失败任务和有效断点时，恢复入口必须保留，同时旧报告仍可查看。

### A12 — 历史列表不读取配置的结果目录

**位置：** [web/history.py:24](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/history.py:24>)；对应写入端 [tradingagents/graph/trading_graph.py:939](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/graph/trading_graph.py:939>)。

图将结果写入 config.results_dir，支持 `TRADINGAGENTS_RESULTS_DIR`；Web 历史固定扫描 `~/.tradingagents/logs`。

**复现：** 将有效结果写入配置中的自定义目录，历史查询不到该记录。

**修复与验收：** 读写使用同一配置来源；覆盖默认目录、环境变量指定目录、直接配置指定目录。若允许查看旧目录，应明确兼容查找规则，不能悄悄忽略当前目录。

### A13 — 东财限流在并发分析师下失效

**位置：** [tradingagents/dataflows/a_stock.py:553](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/dataflows/a_stock.py:553>)，特别是 560–568 行。

各线程都基于上一次“请求完成”时间判断 sleep，但检查、等待、请求和更新时间没有串行保护。第一条请求尚未完成时，第二条也能看到相同的旧时间并立即进入 HTTP 调用。

**复现：** 用两个线程，保持第一条模拟 HTTP 未完成；第二条仍进入 HTTP，证明所谓串行限流没有成立。无需依赖线上限流阈值。

**修复与验收：** 使用共享的并发/速率调度机制，按实际约定限制并发数和请求间隔，使用单调时钟。成功、超时、异常路径均释放占用；多分析师共用相同调度器。不要只增加随机 sleep。

### A14 — 实时状态、落盘状态和报告展示没有统一字段契约

**位置：** [tradingagents/graph/trading_graph.py:902](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/graph/trading_graph.py:902>)、[tradingagents/graph/trading_graph.py:925](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/tradingagents/graph/trading_graph.py:925>)；[web/components/report_viewer.py:169](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/components/report_viewer.py:169>)、[web/pdf_export.py:675](</Volumes/solid hard disk/github/rxylinux/TradingAgents-AStock/web/pdf_export.py:675>)。

图内交易员产出叫 `trader_investment_plan`，JSON 落盘改名为 `trader_investment_decision`；Web 实时展示和导出只读取后者，所以新分析完成时交易员内容缺失，而加载 JSON 后才可能出现。反过来，质量门控的 `data_quality_summary` 没有被写入 JSON，实时能看见的质量结论在重载后消失。

**复现：** 带 UNIQUE_TRADER_PLAN 的真实格式状态导出 Markdown 后找不到该文本；调用实际 `_log_state` 再加载 JSON 后，CRITICAL_QUALITY_WARNING 丢失。

**修复与验收：** 定义一致的报告状态结构，保存质量结论、交易员计划及必要元数据；兼容旧 JSON 的别名读取。对同一状态分别验证实时页面、实时 Markdown/PDF、保存后重载页面和导出，重要报告内容应一致。

## 交给修改 AI 的执行顺序

1. **边界与入口：A15、A02、A01。** 先修 SDK 工具集合和模型地址，再把 CLI 接入统一运行流程。
2. **数据可信度：A03、A04、A07、A08。** 统一可知日期与收益窗口口径，避免旧错误数据继续成为记忆。
3. **并发与持久化：A05、A06、A13。** 配置必须按运行隔离，存储必须按事务保护，请求必须按实际并发约束调度。
4. **恢复和报告：A09、A10、A11、A12、A14。** 保存运行所需元数据，统一读写和展示的字段契约。

每组修复应独立说明改动、补充的回归覆盖与剩余限制。完成后：

- 将经过修正接缝的独立用例纳入默认测试收集；保留本报告记录的失败条件。最终采用 `testpaths = ["tests", "docs"]` 与 `python_files = ["test_*.py", "audit_*.py"]`，不复制两套脚本。
- 补 CLI 的实际 SQLite 中断/恢复集成测试，而不只检查 callback 或函数名。
- 保留上一轮关于分支消息隔离、汇合屏障、失败不缓存、记忆时点与旧断点拒绝恢复的回归测试。
- 运行完整 pytest、ruff 和 diff 检查；可选依赖被跳过时明确写出，不能标成“全部覆盖”。
- 不清空用户的记忆或历史文件来规避兼容问题；需要存储格式迁移时提供可验证的兼容读取或迁移流程。
- 历史缓存/结算记录可能已受影响。修复代码后应制定有范围的失效/重算策略，并保留原始记录以供对照，不能宣称旧数据自动修好了。
