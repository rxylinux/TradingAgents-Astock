# Agent 能力演进方案与 ZCode 任务书

日期：2026-09-06。用户分工：Codex 研究、设计、独立审核和验收，ZCode 编码。基线是上一轮已验收的当前工作区（含全部未提交修复），默认测试 753 passed、14 skipped、52 subtests passed、5 warnings。

**交付状态：N01–N03 及 N04 离线阶段已由 ZCode 实现并通过 Codex 独立验收，当前全量 1009 passed、14 skipped。** N01–N03 的历史结果为 865 passed，见 [原验收记录](AGENT_EVOLUTION_ACCEPTANCE_2026-09-06.md) 和 [对比使用说明](REPORT_COMPARISON_GUIDE.md)；本轮见 [N04 验收](N04_ACCEPTANCE_2026-09-06.md) 与 [离线评测用法](OFFLINE_EVALUATION_GUIDE.md)。N04 真实模型实验及 N05–N08 仍是后续路线图。

## 研究结论

当前已有七分析师并行、指数专用提示词、角色模型配置、结构化决策、质量检查、SQLite 断点、记忆复盘、调用统计、Web 与报告导出。首批优先补齐**研究过程可追溯与报告复盘**，暂不增加分析师数量。

本轮编码前确认的缺口（前三项已在 N01–N03 中补齐，最终结果见验收记录）：

- `quality_gate.py` 的硬检查有评级但只输出文字；它检查报告完整性，并未核验每个事实。研究经理、交易员与最终组合经理没有统一直接接收质量限制，结论页面也缺明确的结构化状态。
- `TradingAgentsGraph._log_state` 只保存同 ticker/date 的一个固定 JSON。同一天换模型或重跑会覆盖上一份报告，缺少配置档案与稳定的运行编号。
- `web/history.py` 和侧栏按 ticker/date 展示单份历史，不能选择两次运行逐项比较。
- `performance.py` 已有收益/方向统计，但缺冻结样本与模型评测；当前调用统计主要在运行时内存中，不能直接声称它是完整成本账本。

工程依据：LangGraph 明确区分检查点中的线程状态与跨线程持久数据；恢复使用当前图，图及状态变更需考虑兼容。因此新增运行档案应随状态保存，不能在每次重试时重建身份，也不能借此悄悄改变既有 checkpoint key。[LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)、[兼容性](https://docs.langchain.com/oss/python/langgraph/backward-compatibility)。

评测依据：Agent 的最终文本、工具轨迹和真实环境结果是不同证据；模型输出有随机性，应使用固定案例和多次试验来评价改进。因此本轮报告对比只展示变化，不把一次评级变化解读为模型能力或收益提升。[Anthropic：Agent 评测](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。

## 功能优先级

| 编号 | 功能 | 用户收益 | 本轮范围 |
|---|---|---|---|
| N01 | 结构化报告质量卡与结论限制 | 看清哪些报告缺失、为何结论受限，限制不会在下游决策中消失 | 已实现并验收 |
| N02 | 每次运行的配置档案与版本存档 | 同日多次分析都保留；能核对使用的模型、分析师、窗口、数据源配置 | 已实现并验收 |
| N03 | 历史报告对比，支持 Web 与 CLI | 查看评级、配置、质量和正文的变化，导出对比结果 | 已实现并验收 |
| N04 | 固定研究案例与离线/真实模型评测 | 量化人工标注支持率、时点违规、失败率和导入延迟/费用 | 离线阶段已验收；真实模型执行与效果实验待做 |
| N05 | 预算、超时、重试和运行统计落盘 | 了解一次研究消耗，并控制工具循环与超支 | 后续；需覆盖结构化重试、SDK 后备及并发取消 |
| N06 | 自选股有界批量研究与任务队列 | 一次处理多个标的，失败项单独恢复 | 后续；先完善每次运行身份及资源配额 |
| N07 | 逐条事实的数据出处与历史快照 | 追溯具体数据来源、披露时间与修订版本 | 后续；需要数据工具返回真实元数据，不从报告文字猜造 |
| N08 | 研究假设、反证条件与事件跟踪 | 明确什么新证据会改变结论，支持后续复盘 | 后续；要与 N04/N07 的证据和评测关联 |

首次交付 N01–N03，本次继续完成 N04 离线阶段。N04 真实模型实验与 N05–N08 仍为路线图，不能在交接中标成已经实现，也不自动建立定时任务、订阅或真实交易功能。

## N01：报告质量卡与结论限制

### 数据和规则

新增不依赖 Web 的质量模块，建议 `tradingagents/agents/report_quality.py`，提供 `assess_reports(state, active_analysts)` 和确定性的质量提示渲染函数。复用现有硬检查，避免出现两套不同的评级算法。

`AgentState` 新增可 JSON 序列化字段 `data_quality`，至少包含：

- `schema_version=1`；`status` 为 `complete / limited / insufficient / unknown`。
- 本次 `selected_analysts`，每个启用角色的 `grade`、`detail`、报告字符数。
- 启用数、硬检查失败数（D/F）；未启用角色不计入。
- `limitations`：由实际硬检查产生的限制项。

状态定义固定为：全部 A → complete；D/F 严格多数（`n//2+1`）→ insufficient；其余存在 B/C/D/F → limited；没有可检查集合/旧报告没有记录 → unknown。状态表示**报告完整性检查**，不是事实准确率、投资胜率或校准后的置信度。UI 要明确这一点，不能换成看似精确的“可信度 92%”。

质量门控同时返回结构化 `data_quality` 与原有 `data_quality_summary`，保持旧文字接口。保留上一轮的启用集合回退、严格多数阈值和 LLM 复审异常处理；LLM 自述的评级不覆盖代码硬检查结果。不要额外增加一次模型调用。

### 对决策和展示的影响

研究经理、交易员、最终组合经理（包括指数路径）直接获得精简的质量限制；不能仅依赖多空辩论间接转述。limited/insufficient 时，最终组合决策必须附有**确定性质量提示**，说明是受限研判；即便模型不遵循提示词，该提示也不能消失。保留原评级，不强制改成 Hold，也不把“资料不足”伪装成中性判断。

提示须幂等，只出现一次；缺新字段的旧 state 兼容原有行为，不凭空为旧报告生成“检查通过”。最终文本随现有记忆保存路径进入复盘，质量限制也需保留。

Web 报告前部展示质量卡，Markdown/PDF 使用相同结构化摘要；统一 JSON 保存新字段。旧报告显示“未记录结构化质量检查”，原质量文字仍可查看。

### 验收

单角色、任意子集、七角色、指数五角色；空报告与主动关闭区别；A/B/C/D/F 的状态映射；偶数团队严格多数；LLM 输出与硬检查矛盾时不覆盖；普通/指数最终决策、记忆与三种报告出口保留限制；旧 JSON 和旧断点仍兼容。

## N02：运行档案和版本存档

### 运行身份与配置

新增纯核心模块，建议 `tradingagents/run_records.py`。`AgentState` 新增 `run_metadata`，字段至少为：

- `schema_version=1`、`run_id`（安全 UUID/hex）、`ticker`、`trade_date`、`instrument_type`。
- `created_at`（明确时区的 UTC 时间），完成时可增加 `completed_at`。
- `config_snapshot`（公开配置白名单）、`config_fingerprint`（稳定排序的规范 JSON 的 SHA-256）。

新运行在 `prepare_graph_run` 创建初始状态时生成身份；同一次运行从 SQLite 恢复后保留同一 ID、原始配置和创建时间。同一图实例下一次 fresh 运行必须产生新 ID。旧断点缺该字段时标示未记录，不能把恢复时的当前配置伪装成当初的原配置。

公开配置包括分析师集合、模型与 provider 配置、角色模型、指数/个股类型、语言、回溯窗口、辩论轮次、公开数据源路由和输出限制。配置字典顺序、分析师集合顺序不应造成无意义的不同指纹；模型、窗口、数据源改变应产生不同指纹。

**持久化采用字段白名单**：不保存 API Key、认证/请求头、Cookie、原始 backend URL、SDK 私有目录、环境变量全集或任意未知嵌套配置。角色覆盖只保存公开 provider/model 等明确许可字段。快照不能引用调用方可变字典。此档案表示配置，不能声称记录了每次请求实际落到的供应商；SDK 后备仍可能改变实际调用路径。

白名单需递归到配置结构：data_vendors 仅认可类别名与字符串路由，tool_vendors 仅认可工具名与字符串路由；不能只筛顶层后原样复制嵌套字典。公开模型配置须覆盖订阅 provider override、SDK 模型/后备、provider reasoning/effort 和辩论提前结束开关等实际影响研究流程的字段。相同 fingerprint 只表示**白名单覆盖的公开配置一致**，不是端点、凭据、程序代码、实时数据乃至整个运行环境完全相同的证明。

这批 fingerprint 用于档案与对比，不新增完整配置指纹恢复拒绝机制；保持现有团队兼容拒绝，避免在有限范围内谎称已经解决全部旧断点迁移。

### 存储和兼容

每个新 run_id 保存一份独立报告，同时保留旧的最新报告路径供既有客户端读取。建议版本目录：

```text
results_dir/_runs/<run_id>/<ticker>/TradingAgentsStrategy_logs/full_states_log_<date>.json
results_dir/<ticker>/TradingAgentsStrategy_logs/full_states_log_<date>.json  # 最新兼容入口
```

可选其他目录结构，但必须同步适配所有历史列表/加载/显示入口，不能依赖新路径下错误的 `parent.parent.name`。报告原子写入；同一 run_id 的重复 finalize 不产生额外历史项、不覆盖已有不同内容的版本；同 ticker/date 的不同 run_id 都保留。新存档与旧最新文件的同一 run_id 在列表只显示一次。已有无 metadata 的旧报告继续可见。

历史项增加运行 ID、创建时间及必要标签，侧栏按钮 key 基于唯一记录，不能继续只用 ticker/date。畸形 JSON 个别跳过并能诊断，不拖垮整个历史列表。不得迁移、删除或改写用户已有历史、缓存和断点。

发布顺序先保证版本存档成功，再更新最新兼容入口；版本写入失败保留上一份成功的最新报告。同 run_id 已存在但正文不同应明确拒绝，至少不得只更新最新入口而让同一身份出现两份不同正文。版本存在性检查与写入应有跨进程互斥或等价的不可覆盖发布保证，原子 replace 本身不能消除检查后被其他写入者覆盖的竞争。

### 验收

真实图 fresh/失败/SQLite 恢复 ID 稳定；同实例 fresh ID 更新；旧断点不伪造档案；两份同日结果都保留且可加载、最新入口正确、列表不重复、重复 finalize 幂等；配置秘密与嵌套秘密不落盘；排序稳定及重要配置变化；run_id/ticker/date 路径穿越拒绝；失败写入不留下可见的半份 JSON。

## N03：历史报告对比

新增核心模块 `tradingagents/report_comparison.py`，建议接口：

- `compare_reports(left, right) -> dict`，纯计算、不调用模型或行情服务。
- `render_comparison_markdown(comparison) -> str`，供 Web 下载和 CLI 共用。
- `python -m tradingagents.report_comparison LEFT.json RIGHT.json [--output FILE.md]`。

比较同一标的与同一 instrument_type 的两份报告（不同分析日期允许）；不同标的/股票和指数混用明确拒绝。本轮不做跨股评分排名。旧报告可比较，但缺配置/质量时标为未知，不能默认成相同或零。

输出至少包括：双方日期/运行编号、评级原值、公开配置差异、结构化质量状态及限制变化、哪些分析师/交易员/最终决策正文改变、完整文本差异。最终采用 Web 直接展开、Markdown 保留全部差异的方式，长报告页面可能较长；折叠只是后续展示优化。交易员 canonical 优先、legacy 回退且只比较一次；角色未运行与报告空白要能区分；不能拿所有字段的字符串变化比例当模型优劣分数。

配置不同或元数据缺失时，明确比较条件差异/无法确认；**不输出“新模型更准确”“收益提升”之类没有实证的判断**。比较结果的 Markdown 可下载，不增加 LLM 费用。

Web 必须有真实入口：从历史记录选两份不同运行，查看对比、下载 Markdown、返回单份报告。选择器/按钮 ID 唯一，不能破坏原有分析启动/恢复/报告查看。分析运行期间避免对比界面打断进度刷新，可只在非运行状态提供入口。

### 验收

同日不同模型、不同日同配置、旧新格式、缺 metadata、空报告、全部字段相同、不同 ticker/type 拒绝；实际 Web 渲染与下载内容、CLI 子进程输出和错误退出码；全路径零网络、零模型调用。保留所有旧报告出口测试。

## 执行批次与边界

1. **第一批 N01+N02**：核心结构、生命周期、存档和质量展示。ZCode 写 `docs/ZCODE_EVOLUTION_BATCH1_HANDOFF_2026-09-06.md`；等待 Codex 独立审核，通过后才做第二批。
2. **第二批 N03**：对比核心、Web/CLI 入口、使用文档。ZCode 写 `docs/ZCODE_EVOLUTION_BATCH2_HANDOFF_2026-09-06.md`；Codex 独立测试并进行实际 Web 流程核验。

编码只在当前项目。保留上一轮全部未提交改动；不提交/推送、不安装依赖/修改全局环境、不读私有认证文件、不改变真实缓存/记忆/断点、不运行付费模型或真实行情取数。测试只用临时目录和离线模型/数据替身，同时拦截 requests 与 yfinance/curl_cffi 后备路径。

Codex 新增独立验收脚本由 Codex 维护；ZCode 不删断言、不改它们来刷绿。若接口变动需适配接缝，应报告具体理由。默认回归包含原有八份审计脚本。任何测试调整须保留原业务不变量。

统一回归：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check tradingagents web cli tests docs/audit_*.py
git diff --check
```

SDK/Gemini 缺可选依赖仍如实跳过。不能以合成案例或文本完整性指标声称改善了实际预测准确率；本轮验收的是工程行为与用户工作流。
