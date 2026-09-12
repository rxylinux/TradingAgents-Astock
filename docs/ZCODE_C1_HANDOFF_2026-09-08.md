# C1 证据账本交接（ZCode → Codex）

日期：2026-09-08。状态：核心通路 + R1 四项边界修复 + 决策者索引 + 展示出口完成，待 Codex 独立审核。

## 实现架构（按 2026-09-08 21:55 Codex 六点契约）

```
a_stock vendor 核心（单次抓取 → (文本, artifact)，文本与原 get_news 逐字节一致）
  ↘ interface.route_to_vendor_with_evidence（与 route_to_vendor 同链路：指数接缝 → 配置/工具级 vendor → rate-limit-only fallback；evidence twin 与普通 vendor 同一 try）
     ↘ evidence/graph_tools.get_news / get_global_news（@tool(response_format="content_and_artifact")，工具名/参数/docstring/纯文本 .invoke() 契约与原工具完全一致；模型不控制 return_evidence/run_id/cutoff）
        ↘ ToolNode → ToolMessage.artifact
           ↘ graph/setup._branch_isolated_tool_node：只收集本次执行的 artifact →
             collect_tool_message_delta（事件身份 {role}:{tool_call_id}；run_id 取自 state.run_metadata；trade_date 开启调用域可信截止）
             → AgentState["evidence_bundle"]（evidence_reducer）
                ↘ RM / Trader / PM prompt 证据索引（evidence.prompt_context，旧 state 空段）
                ↘ _log_state（canonicalize 后随统一 JSON 保存）→ Web expander / Markdown / PDF（evidence.display.render_evidence_md，三出口同一渲染）
```

关键语义（`tradingagents/evidence/ledger.py`）：

- **不可变合并**：reducer 从不修改输入（单元测试深拷贝对证）。
- **幂等**：事件以 `{role}:{tool_call_id}` 为身份；同一 delta 重放整批跳过，来源统计/排除数不倍增（图级 + reducer 级双测试）。
- **确定性**：events/records/source_statuses/exclusions/coverage_notes 全部规范排序；同 evidence_id 的记录按固定规则合并（最早 retrieved_at，(url,title,digest) 稳定 tiebreak）——合并顺序无关；同 ID 不同 digest → 保留规范记录并在覆盖说明中标记冲突。
- **运行隔离**：delta.run_id ≠ bundle.run_id → 整体拒绝（并发运行/陈旧线程不串证据）。
- **首轮直存兼容**：LangGraph BinaryOperatorAggregate 对空通道首轮写入不经 reducer 直存——reducer 遇 delta 形态的 old 会先自举成 bundle；fresh 运行在 `_prepare_graph_run` 预置 `empty_bundle(run_id)`；`_log_state` 经 `canonicalize_bundle` 兜底，落盘 schema 稳定。

## R1 审计四项修复（docs/C1_EVIDENCE_REVIEW_2026-09-08.md）

1. **可信截止时点**（`evidence/cutoff.py` + `setup.py` + `graph_tools.py`）：分支隔离工具节点在**单次** ToolNode.invoke 调用域内开启 `state["trade_date"]` 为可信截止（ContextVar，同步 set/reset，非运行级可变收集器；无 trade_date = 无截止，直接调用行为不变）。工具在调用 vendor 前把越界的 end_date/curr_date 收窄到分析时点，文本与 artifact 均附可见注释 `[注: 请求的日期 X 超过本次分析时点 Y，已按分析时点截断]`，artifact.requested_window 记录 model_requested_end 与 effective_end。模型传 2027、运行时点 2026 → 未来正文进不了 ToolMessage.content 也进不了证据索引（Codex 审计真实编译图用例 + 我方 get_global_news/policy-social 复用入口用例均过）。
2. **同记录合并顺序稳定**：见上"确定性"；事件级抓取时间保留在各自 event，公共记录取最早 retrieved_at（"本次运行最早观察到该事实"），不因分支到达顺序而变。
3. **未知时间不再误标合规**：`validate_reference` 改为联合检查（ID 存在 且 截止请求时时点可验证且合规）；发布时间未知/无法解析、截止无法解析 → valid=False 并说明原因；不提供截止 → valid=True 且 reason 明示"时点未验证"。引用有效≠声明受支持。
4. **日期级截止 vs aware 时间戳**：date-only 截止按上海时区日终（与 A 批次日界一致：+1 天 -1µs）；显式时刻按 offset 转换；naive 按上海时区。不再有 naive/aware TypeError。

另按 review 修正：`route_to_vendor_with_evidence` 的 twin 调用纳入与普通 vendor 相同的 rate-limit fallback try。

## e2e 期间发现并修复的两个额外边界

- **首轮直存**（见上）——不修则单分支运行的最终 bundle 是 delta 形态。
- **retrieved_at 改为抓取时打点**（a_stock 每个 fetch 一次，随 artifact 携带；normalize 透传）——否则同一 ToolMessage 重复收集会生成不同记录，破坏重放幂等。

## 旧 vendor / 旧报告

- 无 evidence twin 的 vendor（yfinance/alpha_vantage/指数接缝）：走**同一配置路由链**取回文本，包装为 provenance=unknown 的 artifact（不解析文本、不猜 records）；DataFailure 文本 → status=failed。
- 旧 state / 旧 JSON 无 evidence_bundle：prompt 空段、Web/MD/PDF 显示"未记录来源证据"、render 空——不凭空生成。

## 验证（更正记录）

**更正**：此前 A/B1 轮我用 `python docs/audit_*.py` 直接执行并按 exit 0 记为通过——该方式只定义 pytest 函数、不运行任何测试，记录无效。本轮起全部用 `python -m pytest` 执行：

```bash
.venv/bin/python -m pytest -q docs/audit_c1_evidence_2026_09_08.py docs/audit_optimization_news_2026_09_08.py docs/audit_optimization_retry_2026_09_08.py
# 27 passed（C1 4/4，此前 4 failed；A 12/12；B1 11/11）

.venv/bin/python -m pytest -q
# 1134 passed / 14 skipped / 52 subtests（pyproject testpaths=tests+docs 全量）
```

测试构成：`tests/test_evidence_ledger.py` 35 项（纯函数：记录/artifact/delta/reducer/渲染/引用语义）；`tests/test_c1_evidence_graph.py` 32 项（真实编译 StateGraph + 生产分支隔离包装器 + 真实 ToolNode + 真实证据工具 + 真实 reducer 通道；vendor 取数在 `_fetch_news_eastmoney/_fetch_news_sina/_requests/_em_get` 最低层换成离线 fixture——与 A 批次测试同一接缝）。覆盖：双分支合并+JSON roundtrip、同 superstep 两工具、图级+reducer 级重放不倍增、MemorySaver checkpoint 持久化、恢复吸收新事件/异 run 拒绝、双并发运行隔离、双源失败=failed 事件、部分失败=partial、可信截止（个股+宏观+越界注释）、无 news 分支的 policy/social 采集、旧 vendor provenance=unknown、旧报告兼容、a_stock 双出口文本逐字节一致、vendor payload（仅过滤后记录/完整 ISO+08:00/empty≠successful/去重与 limit 计量/双源失败）、cutoff 调用域（无图外泄漏/非法日期不截断）。

回归适配一处：`tests/test_sentiment_data_tools.py::test_graph_tool_node_matches_analyst_tools` 按源码标识符比对 social ToolNode 注册表——C1 包装在运行时工具名/参数/契约与原工具一致（Python 标识符带 `_with_evidence` 后缀），测试改为按运行时名归一化后比对（断言语义不变）。

## 文件清单（SHA-256）

```
462074f7021c264fc3958b3195636632d9e8dfd79b12c361d0cca53c7379115d  tradingagents/evidence/ledger.py
4e9373fb2130d598c6a43221a8d6dc85b6bb51bda4a5c366818c206a6a36f050  tradingagents/evidence/graph_tools.py
61f4f0586c0fbaa6cd2ec40c29a234629948a470f4f5f08f944f59797a403d9d  tradingagents/evidence/cutoff.py
0a3f97ac142a3f83f7ad8a49c46440f0fc81d88893b37833a59f5cff5e517923  tradingagents/evidence/prompt_context.py
e568c3338036163865646fe57bd940311a1fa5493f0dc5f2ee45dafd2f7f2fe6  tradingagents/evidence/display.py
df5dc00563809a3bdd6008f181f42b28a8c5899308ec8f76efd9c25f37c43937  tradingagents/evidence/__init__.py
cbf28f1ed6187494e142c483c154ead354f212f7d2419cbeb48796664a2bf00b  tradingagents/graph/setup.py
c542acaceb38298c72f2bdc49b730a86812bec023113eb535d1c9ff2a93277b8  tradingagents/graph/trading_graph.py
6e1ec647f507de3f3a9a4dec646e13b6c08c8ff108491dbc13c924e48f9e6916  tradingagents/dataflows/a_stock.py
f8e4c0f7d09f26c8732cffef6bbd0311633155a93d07e7faa2e86b5898b63b32  tradingagents/dataflows/interface.py
cd947981292cb8ad81f85ff73fbbde9b9b69c6ea49f525e2041eb93426287c63  tradingagents/agents/utils/agent_states.py
08b319f9f61ab1a2b77e9cb71ba8c1259c906d3f81fda66553c3ea6fa494d0ca  tradingagents/agents/managers/research_manager.py
afed6224b6b3be68152e5f8c89aef0893e7b0c5f0dfe70df3fe48aff62692749  tradingagents/agents/managers/portfolio_manager.py
7cd9befb3d20d9957fc7c88b9595517b59a585fb0c71a8d9ba909a0396289028  tradingagents/agents/trader/trader.py
9a685f345df1b77577914a8f81b8cc748b85663ffb5191ef09c02dc857293b93  web/pdf_export.py
23517aa8d40d1002f106b1cd86e13aa8eccc09edafe5b58f4d410abc69410bf2  web/components/report_viewer.py
011f8fae40134d5f813e21e716c5281db82d56336853861842462e9979819a11  tests/test_evidence_ledger.py
7851275d53c4b734924e7185220b7170f4d2a7d04d02a38be97d2031e8d08e5f  tests/test_c1_evidence_graph.py
```

删除：`tradingagents/evidence/news_adapter.py`（monkeypatch 草案，Codex 已否决；其职责由 a_stock 核心函数 + interface twin 路由承担）。

## 边界与未做

- 证据模块结果不构成投资准确率提升证明（展示文案已明确）。
- C2（研究假设卡：thesis + supporting/contradicting evidence_ids + assessment_status + invalidation conditions）未开始，等 C1 稳定交接后按 DECISION_ACCURACY_PLAN 实施。
- 未做：SQLite checkpoint 的跨进程恢复实测（MemorySaver 已覆盖通道持久化语义；SQLite 序列化走同一通道机制）；评估系统（evaluation/）尚未消费 evidence_bundle。
