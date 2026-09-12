# E 独立审核

## 最终独立验收（2026-09-09）：E范围通过

三轮发现的请求预算、工具范围、时间/标的、结构化降级与恢复缺口已修复。独立E审计24项通过（含实际HTTP上限/重试计数、真实GraphSetup→prepare→SQLite E-on恢复完成规划且不重跑初判）；R3原23项与ZCode46项合并69 passed in 12.58s，新增完整正常恢复控制后独立24 passed in 1.03s。审计恢复坏结构用例补齐合法模式锚点，避免仅因缺锚拒绝而掩盖schema/形状检查；正常恢复fixture将历史上下文替身统一为None，未将fixture的空字符串差异误报为业务缺陷。

独立全量：**1356 passed, 14 skipped, 52 subtests passed, 5 existing warnings in 73.62s**（exit0）。仓库ruff与diff检查通过。测试前后298文件SHA-256完全不变；仅新增F2设计文档。快照：`/var/folders/1k/6mfcfpgd38bgh4s31fz6wsv00000gn/T/codex-e-final-84q9u2oz/before.json`。

```text
116724c27159ffdd421f2e07e1e2bb25dcd423e0092712b507b31a35828424ed tradingagents/agents/debate_evidence.py
2ff571a7c23ed934faf10850fc3c9c660d27129ba36a7aee35c8e61cb6d49664 tradingagents/graph/trading_graph.py
380b34fc4176524a86ee793f05326aeb78b3818ec05f35e40fec1783eefbbee5 tradingagents/graph/setup.py
46ab0ba864a219b940856bfb6ef3a06add2e7689e547d92cd1a929945d30ac52 tests/test_evidence_debate.py
8a415a19f8b1598a071b0b86f4ec886e102ba219dfa37735c78ecdab4e62e197 docs/audit_e_debate_2026_09_09.py
```

验收仅证明当前E工程契约；默认关闭，引用合法不等于语义支持，不保证投资效果。HTTP未观测时保持unknown，wrapper调用数另标语义；真实模型/真实数据E-on/off效果实验不在本次范围。已批准立即按[F1 Codex实施契约](F1_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md)实现离线不可变复盘记录、严格时点检索与CLI演示。F2/F3仍分别审查。以下R1–R3保留作历史。

## R1（2026-09-09 00:55）：暂不验收

新增 Codex 审计 `docs/audit_e_debate_2026_09_09.py`，11项中 **9 failed / 2 passed in 1.91s**（exit 1）。ZCode 当前自身测试 **26 passed in 3.45s**；独立脚本 ruff 通过。开发仍进行中，本轮是早期实测反馈，不跑全量。

```text
.venv/bin/python -m pytest -q docs/audit_e_debate_2026_09_09.py --tb=short
.venv/bin/python -m pytest -q tests/test_evidence_debate.py --tb=short
```

### 已复现失败及修复契约

1. **P1 输出预算没有到 HTTP**：真实 `NormalizedChatOpenAI` + `httpx.MockTransport`，共享客户端 max_tokens=9000；经过 E 初判结构化路径，实际请求 `max_completion_tokens=9000`，输出却写 cap=2048。`bind(max_tokens).with_structured_output` 丢失绑定限制，广泛 except 又回退到无上限原客户端；plain fallback 也未带限制。必须在全部实际请求路径传递上限，保持共享客户端不变，不能只让测试替身支持 bind。
2. **P1 用户重试数被包装器丢弃**：同一真实客户端 max_retries=0、持续429、sleep仅测试替换，实际发3次请求，应该1次。`_CountingLLM` 没有转发 B1 `_get_retry_budget`，触发默认3。应保留配置能力与结构化/后备共用预算；真实传输计数验证0/1/持续429与解析失败，不得修改旧审计绕过。
3. **P1 跨标的取数**：真正 StateGraph→E recheck→ToolNode→新闻供应商替身，state标的600519，规划参数000001，实际供应商拿到000001。校验代码格式不等于绑定可信标的；拒绝失配或显式收窄到可信代码并记录。合法600519控制例确实执行一次供应商调用，不以未执行的空结果冒充通过。
4. **P1 时间前置校验缺口（4例）**：非法start_date、非法end_date、start>end，以及缺可信trade_date但模型请求2099，都到达 `ToolNode.invoke`。仅clamp_end_date把错误留给供应商不满足本契约“无法校验不调用”。E入口严格解析可信日与全部工具日期，收窄后验证起止顺序；拒绝时0次实际工具invoke并保留原因。未知可信截止不能退为无界调用。stock窗口也需明确有限lookback；bool不能作为整数limit通过。
5. **P1 当前工具集合被扩大**：真实GraphSetup只注册news/get_news，E仍硬编码注册get_news与get_global_news。从当前实际选中分析师所用、已注册且支持artifact的工具取白名单交集；没有可用工具保留缺口，不构造新工具。规划节点也应收到实际白名单，避免无效问题占掉前2个合格槽位。不得以全局常量工具可导入证明当前运行已启用。
6. **P2 摘要上限仍越界**：max_chars=2400、长topic输出2429；尾部截断说明必须在预算内，优先保留限制，不能只切正文后追加。

两个通过控制：合法本标的工具链实际执行；已完成问题不重放。最初跨标的探针直接在图外调用ToolNode遇到缺runtime配置，Codex已改为真正StateGraph路径再得到上述失败；不把初始空结果算通过证明。

### 相邻检查建议（未在此轮计为失败）

- `_CountingLLM.invoke_count` 是runnable调用次数，plain路径内部重试可能超过此数，不应一概名为actual HTTP requests；已知请求与未知token独立统计，当前aggregate把token未知连带变请求未知。结构化输出可保留raw usage，不将已知部分吞掉，失败也留限制。
- 字符串正文/列表总量尚无硬限制（claim、assumption、question、disagreement自由文本）；保留失效主张与截断说明但限制持久体积及planner提示词。规划的bull/bear引用也应附校验，不能只有初判引用有校验。
- 输出语义解析无tool_calls时真实模型可能返回None，当前render先把None append后异常、后备完成再validate None会崩；供应商失败与空结构应生成受限记录并明确预算，不能伪称完整初判。
- E自身run_id/版本/开关阶段锚点应做恢复完整性校验；仅看has_e与debate_count不足覆盖初判之前的E-on/off切换。真实生产prepare+checkpoint路径需验证，重建的小图只证明局部节点行为。
- 下游摘要目前只给方向与主张计数、分歧topic、补查条数，没有双方具体主张、决策影响及新证据实质。按契约保留足够受限内容用于原辩论/RM，不让补查只对最终JSON有用。

继续修E R1，完成后定向交接；F设计仍可不冲突推进，但未经独立验收不得跳过失败。Codex不改业务实现，不修改ZCode测试。

本轮SHA-256（开发中快照）：

```text
a6d5fb171e28ea6c4d003847da171ac014cc360183a0403632d8e3d27530be4f tradingagents/agents/debate_evidence.py
62307c38a0bf6f1022451e36cee21421bbd588ae963debdc6332ae1889d85c49 tradingagents/graph/setup.py
0a884575bfaad686c1ff28a39d003be0e5e75e59166b3dc954d875a7720a8346 docs/audit_e_debate_2026_09_09.py
```

## R2（2026-09-09 01:08）：11旧探针通过，7项新增失败

R1修正独立复验与自身33项合并 **44 passed in 14.44s**。进一步在相邻既有契约边界新增7项，当前独立E审计 **7 failed / 11 passed in 1.95s**（exit 1）；未跑全量。

1. **实际规划节点未传白名单（2例）**：`create_disagreement_planner_node(..., allowed_tools=[])` 与 `['get_news']` 均把模型的get_global_news规划为pending，eligible=1。根因有二：空list经truthiness回退到全局默认；结构化成功分支调用validate_plan漏传allowed（仅后备传了）。测试必须穿过真实planner，不只测validate_plan。显式空集合必须保持空，None才可用文档化默认。
2. **不支持cap仍无上限请求（1例）**：provider拒绝with_structured_output的cap kwarg后，包装器实际再次无cap构造并invoke，且cap_applied=False根本未写入usage/limitations。保留在提示词里不符合硬上限；无法保证时不发无界请求，返回受限记录，正常支持provider控制例保持。plain TypeError捕获同样不能随意再次发未计数请求（TypeError可能在已经请求之后出现）。
3. **空结构化响应崩溃（1例）**：真实NormalizedChatOpenAI+MockTransport返回普通content、没有tool_calls；结构化parser给None，render先append(None)再抛异常，纯文本后备结束后validate_view(None)触发AttributeError。验证类型后才能capture；空/错误/拒绝响应形成明确受限记录与已耗用量，不能伪造claim。规划同类路径一并验证，不得将render自身缺陷算新的无界纠错调用理由。
4. **恢复跨run未拒绝（1例）**：state.run_metadata.run_id=audit-e，evidence_debate.run_id=other-run，真实TradingAgentsGraph._validate_resumed_evidence_debate不报错。E结构版本、可信身份及阶段/开关锚定应贯穿prepare/checkpoint恢复；不能只检查has_e。两初判尚无run绑定也需要覆盖，不能只给plan加guard。旧无E断点保持兼容，明确在初判前切换拓扑的处理；实际SQLite/prepare恢复路径验证，不改坏断点。
5. **单主张正文无硬限（1例）**：validate_view接收100万字符claim仍完整保存、无截断说明。现有条数上限不等于总体有限。给claim/assumption/question/impact/ID等字符串与总列表明示有限上限；保留主张与引用校验，不通过删掉坏主张刷合规。传planner与下游时遵守同一边界，schema侧与持久化侧防护都要实际生效。
6. **已知请求数被未知token抹掉（1例）**：三个各已知1次的计数输入，token未知导致aggregate的actual_request_count也变unknown。两个维度分开汇总；可观测的部分不能丢失。另需诚实命名runnable invoke与HTTP transport计数，plain内部重试不可冒充只有1个HTTP。若没有HTTP可观测性可另字段unknown，但保留已知调用数，不把上限当实耗。

这些属于权威E契约规则2/3/4/8的现有要求，不是可在本批无条件延期的功能扩展。请优先完成R2及同类边界，定向交接，暂不进入F业务代码。另按R1建议补下游摘要的实际双方主张/决策影响/证据要点及限制；只显示补查条数不能支撑反证决策。

R2 开发快照 SHA-256：

```text
6f6d7acb873da38f385c18ea3b8c74139a0b5d7e0e4f9a33f90d85025276dc22 tradingagents/agents/debate_evidence.py
380b34fc4176524a86ee793f05326aeb78b3818ec05f35e40fec1783eefbbee5 tradingagents/graph/setup.py
31e8336cbe91fb8ce2508515698825a65930b4146b5ab9c304afedfad2283d5a tradingagents/graph/trading_graph.py
2d5b01aa2c0a7370b0e80b5f18b8a00f063b887c3a0cc55f281c53a0431f57d3 docs/audit_e_debate_2026_09_09.py
```

## R3（2026-09-09 01:29）：生产恢复阶段与损坏结构

R2独立18项与ZCode42项合并 **60 passed in 5.31s**。进一步检查此前要求的完整恢复边界，新增5项失败；独立审计当前 **5 failed / 18 passed in 2.02s**，exit1。仍不跑全量。

1. **真实生产图阶段切换会漏E**：使用GraphSetup真实E-off拓扑、真实_prepare_graph_run生成fresh初始态、SqliteSaver在Quality Gate节点完成位置持久化（update_state as_node='Quality Gate'；真实后继为Bull Researcher）。关闭连接后以E-on生产图调用真实_prepare_graph_run，竟正常允许恢复。此时无E字段、debate.count=0，has_e/count防线漏掉。LangGraph恢复的是已持久的待执行Bull Researcher，不会因为新图插入E边而重新经过Quality Gate。必须从fresh即持久化E模式/版本锚点，并在读取checkpoint任何阶段比较；或对无锚旧态依据可信待执行阶段拒绝危险切换。不得重跑已完成分析师/质量检查来伪装恢复成功。E-off旧态的同模式兼容保留，E-on尚未初判就关闭也应对称处理。测试要实际GraphSetup+SQLite+prepare，不只重建小图。
2. **缺可信run仍通过**：run_metadata没有run_id，E自身run_id存在；当前比较用`elif meta_run_id and ...`，可信身份缺失反而跳过比较。有E状态但不能证明归属应拒绝。
3. **未知schema_version=999通过**：同run计划未知版本未拒绝。两初判/计划都需明确schema兼容检查，不能忽略版本后继续使用。
4. **计划损坏成list通过**：has_e=True但后续只处理dict，list直接绕过校验。
5. **初判损坏成字符串通过**：同样因not isinstance(...,dict):continue绕过。缺失/None（旧态）与存在但坏类型必须明确区分；归属有效也不代表内部结构有效。

请完成这组R3和对称控制例，再交接独立终审。F1修订文档可继续，但不绕过E恢复缺口进入生产编码。断点失败时保留原数据、模型/工具零调用；不要把坏状态清空后当fresh。全量等稳定快照。

R3 快照 SHA-256：

```text
116724c27159ffdd421f2e07e1e2bb25dcd423e0092712b507b31a35828424ed tradingagents/agents/debate_evidence.py
38104be7da5f4a615b75d5b17005f0780b0721218edce70db7a022ced1194a24 tradingagents/graph/trading_graph.py
c1db9b3ec1a54b2fe2810d26b379a7251e2e193e888046b635b30a1697320f80 docs/audit_e_debate_2026_09_09.py
```
