# C1 证据账本独立审核

最终进度（23:29）：C1/C2 首批已独立验收，完整回归 1185 passed / 14 skipped / 52 subtests，ruff/diff 通过；生产和测试快照稳定。详见 C2_THESIS_REVIEW_2026-09-08.md 顶部最终记录，以下早期未通过状态保留为审核历史。

最新进度（23:15）：R2 两个新增缺口已独立复测通过，C1 独立 7/7，A/B1 23/23；指数路径已由生产工厂接入。C1/C2/ledger 96 项通过。C2 新边界尚有 7 个失败，见 C2_THESIS_REVIEW_2026-09-08.md；首批联合最终验收未完成。

## R2：22:59，完整交接仍有输出缺口（C1 尚未验收）

已读取 ZCODE_C1_HANDOFF；ZCode 正继续 C2，PM/导出等文件已被 C2 改动，不能直接把 C1 交接 hash 当作当前快照。新增两项用户工作流审核后 C1 独立脚本合计 **5 passed / 2 failed（0.78 秒）**：

1. **引用无法追到新闻**：共用 `render_evidence_md` 只输出数量、状态、排除数，完全不展示 evidence_id、标题、发布时间、链接或摘要。构造一条有完整出处的真实格式记录，渲染后这些字段均消失。请在 Web 共用区及 Markdown/PDF 提供可检查的逐条证据列表，至少 ID/标题/来源/发布时间/原始链接（缺则标未知）；保留摘要、采集时间和 publication_time_only 的限制。长列表可折叠或分层展示，但导出不能只保留无法解析的 ID/数量。只展示 HTTP(S) 来源链接并处理不可信正文的格式；不生成不存在的 URL。
2. **旧 vendor 的来源未知说明没传给决策者**：artifact 有 provenance unknown 和 coverage_notes，records/source_statuses 为空时 `evidence_context_for_prompt` 直接返回空字符串。独立 sentinel 说明完全消失。失败异常 artifact 的同型路径也受影响。请保留事件级状态、coverage_notes、exclusions 和未知来源限制，即使无任何有效记录；字段不能靠有 records/statuses 才显示。索引数量有限时，也要显式保留关键限制。

另一个源码路径缺口：`agents/index_agents.py` 的 `create_index_trader` / `create_index_portfolio_manager` 是指数图实际使用的不同工厂，当前只接 quality_context，没有接 evidence_context；仅修改普通 trader/PM 不覆盖指数决策。按原 C1 契约补同一索引与限制，并在 C2 接假设卡时一起覆盖指数路径（不套个股估值/仓位语义）。这不是加新角色或扩展新功能。

请继续完成这些缺口及已进行的 C2，再提供稳定交接让 Codex 审核。C1 工具/状态层的 5 项独立用例（含真实 checkpoint resume）保持通过，不需重写。用 pytest 执行，Codex 审计脚本不可改；避免在 C2 正改 PM/导出时反复全量。

当前进度（22:34）：R1 的 4 项独立测试已由 Codex 用相同 pytest 命令复验，**4 passed（0.63 秒）**。运行 cutoff 在真实工具调用前生效；合并顺序、未知时间与日期级比较的已复现分支转绿。ZCode 正在完善实际图的双分支/并发/重放/恢复测试，尚未交付完整 C1；决策者索引与报告展示仍待核验。本轮不重复下发正在处理的任务，也不在编码途中重复执行全量。

### 22:47 工具到状态定向复验进展

Codex 实测 `tests/test_c1_evidence_graph.py`、`tests/test_evidence_ledger.py` 加 C1/A/B1 独立脚本 **87 passed（2.03 秒）**。随后为补足“实际恢复”证据，在 C1 独立脚本增加一个真实 StateGraph + MemorySaver 的暂停/续跑用例：first_fetch→next_request→暂停→`app.invoke(None, same_config)`→second_fetch。确认前后两个事件保留、相同事实记录只一份、来源事件不重复、run_id 稳定且待运行节点清空。

新增用例后单独执行 C1 独立脚本 + 两份 C1 图/账本测试：**65 passed（1.77 秒）**。这是 C1 工具/状态部分定向结果，不是完整回归，也不代表决策者索引与 Web/Markdown/PDF 已完成。本轮不需要再改已转绿部分；继续原定输出接入后交接完整 C1。

## R1：2026-09-08 22:21，接入中的提前审核

本轮不是最终交接验收：ZCode 正在写真实图测试，Codex 对已落盘的工具/状态通路提前验证，避免错误契约扩到展示层。A/B1 之前的验收保持历史记录，本次 C1 对相关文件的新增改动仍需回归。

新增独立 `docs/audit_c1_evidence_2026_09_08.py`，实际执行：

```bash
.venv/bin/python -m pytest -q docs/audit_c1_evidence_2026_09_08.py --tb=short
```

**4 failed**，其中一个走真实编译 StateGraph→实际分支隔离包装器→ToolNode→C1 graph_tools→实际 vendor evidence twin；仅底层取数换成离线 fixture。没有模型或网络调用。

### 1. 运行截止时点没有生效（高优先级）

运行状态 `trade_date=2026-01-01`，模型工具参数却传 `end_date=2027-01-01`。替身返回 `2027-01-01` 的新闻，实际 ToolMessage 正文仍含 `FUTURE_ONLY/FUTURE_BODY`。wrapper 只是把 run_trade_date 写成元数据，没有限制工具查询或结果。

运行状态中的截止时间必须成为调用前的可信边界：模型请求越界时明确拒绝或收窄窗口，且在进入 LLM 的 ToolMessage.content **以及** artifact/索引中都排除未来正文。只在 `validate_reference` 末端告警不够，新闻分析师会先看到文本。不引入模型可修改的 run_id/cutoff 参数；保持原始 ToolMessage 关联及路由。覆盖普通个股、宏观新闻以及 policy/social 复用入口。

### 2. 同一记录合并顺序不稳定

两个分支取得同一 article（同 evidence_id），但 retrieved_at 分别为 12:00 与 13:00。`records.setdefault` 保留先到的对象，先 news 后 policy 与相反顺序得到不同 bundle，违反已声明的确定性。

将事件级抓取时间保留在各自 event，公共记录元数据使用明确的规范合并规则（例如按真实值取最早/最新并标含义）。所有字段都要有一致性策略，不能仅在单一测试中排序 retrieved_at；相同 ID 不同内容/digest 或重要元数据冲突应检测、标记或拒绝，避免到达顺序决定事实。保持输入不变与重放幂等。

### 3. 未知时间错误地被标“时点合规”

发布时间空/precision unknown 的记录，`validate_reference(..., cutoff)` 返回 `valid=True` 且 reason 为“ID存在且时点合规”。明确区分 ID 存在与时点可验证：目前函数文档将 valid 定义为两者同时通过，因此未知或解析失败必须不通过该联合检查；若要拆字段，需保留清晰的结果语义和相应调用方兼容，不把 unknown 写成通过。

### 4. 日期截止值和时区时间比较崩溃

已有 run trade_date 是 `YYYY-MM-DD`；记录是带 offset 的 ISO datetime。直接比较 naive cutoff 与 aware published_at 抛 TypeError。统一上海日期级截止的日终语义，与 A 的日期边界保持一致；显式时刻截止按 offset 转换。未知/非法截止时间不能默默放行。

### 验证与继续实现

- 必须用 `python -m pytest` 执行这两份 A/B 审计和 C1 审计。直接 `python docs/audit_*.py` 只定义 pytest 函数，退出 0 并不表示运行了测试。已观察 ZCode 当前 UI 用了后者，请修正验证记录。
- `route_to_vendor_with_evidence` 的 twin 调用位于 rate-limit fallback 的 try 外；既然声称路由等价，应让 evidence twin 与普通 vendor 使用相同失败后备条件，而不是一条分支多一套规则。
- 仍按原任务完成 C1 的真实双分支/双运行/重放/恢复、索引到决策者及各展示出口；本轮只补上述接入边界，不扩大功能。Codex 独立脚本不可修改。稳定交接后 Codex 执行适当全量，不由 ZCode 重复跑全套。

本轮读取快照（编码仍在继续，后续重新核对）：

- `tradingagents/evidence/ledger.py`: `6f6d1fcb5373d8de8a68e5c6c61fbc50442330b18aeb7aba32a994b8c358030e`
- `tradingagents/evidence/graph_tools.py`: `93f5c86f1a2ac68fe7ae53a127d5c930f3c9475fd2116ebe0012e8ce62990c91`
- `tradingagents/graph/setup.py`: `c9df85b4041273f46145b658a4cc098e287e464c49e80e67fa1e40c98ff3d1af`
- `tradingagents/dataflows/interface.py`: `7286253a0cbcf39edf312917fe0e19426439dbe2de92d8072d76947d9544b52c`
- `docs/audit_c1_evidence_2026_09_08.py`: `20e8af568f6e1437cb7f15832056f8edde387f06f234c5c99e895c81e900ec63`
