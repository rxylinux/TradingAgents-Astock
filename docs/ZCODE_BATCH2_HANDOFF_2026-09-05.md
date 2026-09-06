# ZCode 第 2 批交接（A03 / A04 / A07 / A08 + 边写边审反馈）

日期：2026-09-06 · 分工：ZCode 编码，Codex 方案与逐批独立审核
基线：工作区（含第 1 批未提交修复），本批改动**未提交**。
两份独立脚本未改动（`docs/audit_repros_2026_09_05.py`、
`docs/audit_temporal_2026_09_05.py`，后者含 Codex 边写边审新增的 15 例）。

## 变更明细

### A03 — 北向/行业/资金流报告的历史时点（P1）

- `get_northbound_flow`（[a_stock.py](../tradingagents/dataflows/a_stock.py)）：
  历史分析日**完全跳过实时分钟段**（不发请求、不出数字/信号），并前置
  快照警告；历史缓存**先按分析日过滤、再取最近 20 日窗口**（边审 1：
  `_load_northbound_history` 新增 `n=None` 全量加载——先截尾会把窗口挤占
  成未来行，把真实历史误判为无数据）；过滤后为空时输出明确缺失（非
  「净流入为零」）；当前日期请求保持合法实时段与快照写入。
- `get_industry_comparison`：历史请求不请求实时排名，返回「数据缺失 +
  `_snapshot_notice` 禁止引用说明」——不是换标题继续给实时数字。
- `market_flow.gather_market_flow_data`：历史日期下行业/概念资金排名段
  前置快照警告（北向与行业排名的时点防护在各自函数内完成）。

### A04 — 财报以披露/修订可知时间过滤（P1）

`_get_financial_report_sina` 重构可知性过滤，规则（边审 2/3 落实）：

- **不依赖「报告日」列存在**：可知性检查独立执行（只有公告日期、没有
  报告期的行同样受检，无法证明可知即剔除）。
- **行级可知时间 = max(公告/披露日期, 修订日期)**：晚于分析日的修订
  （重述/更正）尚未发生，该版本不可用；**所有存在的时间字段都必须有
  效**——修订列存在而值缺失/无效时，无法证明是「无修订」还是「丢失了
  版本元数据」，保守剔除。
- 字段候选：披露 `_PUBLICATION_DATE_FIELDS=("公告日期","披露日期")`、
  修订 `_REVISION_DATE_FIELDS=("修订日期",)`——仅认数据源实际返回的字段，
  不预设线上接口一定提供；**全部缺失时历史模式整体缺失**（attrs.
  missing_reason → 工具层转 `[数据缺失: ...]`，不缓存）。
- 恢复被误删的非历史报告期过滤（`报告日 <= curr_date`，防未来报告期）；
  过滤后为空时给出明确可知性原因（「截至分析日无已披露数据」≠「源无
  数据」的成功空结果）。
- 同一报告期多版本取分析日当时已知最新；**最近 8 期显式按报告期降序**
  （边审 4：升序排序后 head(8) 会取最旧 8 期）。
- 缓存版本 **v3 → v4**：v3 及更早的 `financials-*` 结果（按报告期过滤、
  可能含未披露报表）留在原命名空间天然失效；不删除用户缓存。回归验证
  v3 旧字符串不能绕过新过滤命中。

### A07 — 股票/基准以完全一致的实际起止日期计算 alpha（P2，原始任务等级）

`TradingAgentsGraph._fetch_returns`（原生与 Yahoo 两路）：

- 窗口按**目标证券自身交易日**定义：起点=分析日起首个交易日，终点=第
  `holding_days` 个交易日；**基准必须在完全相同的两个日期有行情行**，
  起止价都取相同日期——按行号对齐在停牌/缺行时会比较不同日期（原始
  复现：股票 1/5→1/8、基准按行号取到 1/6，alpha 由 -20% 反转为 +9%）。
- 基准端点缺失 → 全 None 保持 pending（不静默换日期）；`outcome_end`
  即实际使用的结束交易日（R4 可知性一致）。
- Yahoo 路径：索引按**本地日历日**对齐（先 `tz_localize(None)` 再
  normalize）——跨时区的 normalize 不剥 tz 时，同一交易日在两条腿上是
  不相等的绝对时刻（边审新增断言）。

### A08 — 完整窗口才结算（P2，原始任务等级）

- 去掉 `min(holding_days, len(stock)-1, len(bench)-1)` 的静默缩短：目标
  证券交易日不满 `holding_days` → 全 None 保持 pending，绝不缩短定稿。
- **取数范围查到当前可用日期（阻塞 1 的语义修复）**：native 的
  `end_date=今天`、Yahoo 的 `end=今天+1`（Yahoo end 为排他语义）。窗口
  是否足够由拿到的**实际交易日行数**决定，不依赖任何固定自然日上限——
  已充分过去的长停牌（2 月/7 月复牌参数化）只要交易日满即可结算，仍
  不满（真未复牌够）才 pending。此前 `holding_days+15 → +90` 的两次
  常数调整已被该语义取代。
- 完整窗口只结算一次：结算即 pending→resolved，天然一次。
- **既有错误短期 outcomes 的迁移方案（有范围、不擅动用户数据）**：
  `TradingMemoryLog.find_short_window_outcomes(min_effective_days=5)`
  **只读诊断**——识别 resolved 且 holding < 5 的条目（旧版 min() 缩短
  结算的特征）。迁移流程（仅说明，未实际执行，不自动改写任何真实记忆）：
  ① 备份日志文件；② 对每个受影响条目，**仅把 tag 改回 pending 是不够的**
  ——旧 REFLECTION 段会保留，且重算回填时新反思会追加在其后造成两段拼
  接；必须**同时移除旧的 outcome 元数据（raw/alpha/holding/end/resolved
  字段）与整段 REFLECTION，仅保留 tag 与 DECISION**；③ 重跑同标的，回填
  按完整窗口重算并写入新的 outcome 与 REFLECTION。日志原样保留备份供
  对照。

### 测试夹具适配（均已说明依据，不弱化断言）

- `tests/test_memory_log.py`：
  - `_price_df` 增加 DatetimeIndex——真实 yfinance `history()` 以
    DatetimeIndex 返回，A07 按日期对齐后 fixture 需与真实形状一致；
  - `test_fetch_returns_benchmark_shorter_than_stock`：原断言固化了
    min() 缩短行为（days==2 即结算），按 A07/A08 正确行为改为「基准缺
    对齐端点 → 全 None 保持 pending」；
- `tests/test_review_round2_regressions.py`（Codex 明确允许的夹具完善）：
  三个财报用例**保留原 `curr_date="2026-09-04"`**（历史日期），给成功
  响应增加 `公告日期: 2026-08-30`（分析时点已披露、合法历史数据）——
  断言不变（失败不缓存、恢复后重新取数、拿到 12345、失败输出无
  "No "）。此前我临时改到 2099 年的做法已回退。

## 新增回归（tests/test_temporal_boundaries.py，25 例，默认收集）

- 北向：历史跳过实时（实时请求被替身为炸）、先过滤后取窗（60 日缓存/
  分析 1/15 → 15 行 + 均值 8.00）、无缓存明示缺失、当前日期保留实时；
- 行业：历史缺失+禁止引用、当前日期正常取排名；资金流报告快照警告；
- 财报：6 类不可证行（无披露/无效披露/未来披露/无报告期/晚修订/无效
  修订）全空、披露后可见、修订后整体可见、最近 8 期、空结果原因、无
  披露字段整体缺失、v3 缓存不绕过；
- 收益窗口：native/Yahoo 相同日期对齐（alpha=-0.2）、跨时区本地日对齐、
  基准端点缺失 pending、短数据保持 pending（不生成反思）、长停牌满窗
  结算、短期 outcomes 只读诊断。
- 离线保证：Yahoo 路径 patch `yf.Ticker`（见下方联网事件记录）。

## 验证记录

```
本批定向（temporal_boundaries + memory_log + lookahead_guard + market_flow
  + data_failure_semantics + cache_and_resilience + astock_sina_supplement）:
  182 passed
全量: 644 passed, 14 skipped, 52 subtests passed, 5 warnings
docs/audit_repros_2026_09_05.py + docs/audit_temporal_2026_09_05.py:
  26 passed（本批 A03×2/A04×3/A07×1/A08×1 + 边审/阻塞全部转绿），
  9 failed 均属第 3/4 批（A05/A06/A09–A14），保留失败
ruff check tradingagents web cli tests: 通过
git diff --check: 通过
```

跳过项仍为未安装的可选依赖（claude-agent-sdk / google Agent SDK）。

## 联网事件如实记录（不能声称本批全程离线）

修复 A08 初版时，`docs/audit_repros` 的 A08 复现在我第一次运行中**发生了
一次意外的真实联网**：audit 的隔离夹具只 patch 了 `requests.Session.request`，
而 **yfinance 不经该入口**——native 数据窗口不足时代码落到了 Yahoo 后备，
真实取到了 600519 行情并据此结算（当次运行约 7.6 秒、返回了真实收盘价）。
发现后立即修复了语义（native 已有该标的数据但窗口不满时不再落到其他源，
直接保持 pending），复现不再联网（0.5 秒内完成）。无凭据参与（yfinance
无需 key）；持久化方面准确地说：当次运行仅在 audit 的测试临时记忆/缓存
目录内有写入，未改动任何真实用户记忆。后续我的 Yahoo 路径
测试一律 patch `yf.Ticker`；Codex 已在独立脚本补齐禁止联网替身。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `tradingagents/dataflows/a_stock.py` | A03 北向/行业时点；A04 披露/修订过滤、最近 8 期、原因说明 |
| `tradingagents/dataflows/cache_utils.py` | 缓存版本 v3→v4 |
| `tradingagents/market_flow.py` | A03 历史排名快照警告 |
| `tradingagents/graph/trading_graph.py` | A07 日期对齐（含跨时区）；A08 完整窗口 + 查到当前日期 |
| `tradingagents/agents/utils/memory.py` | A08 短期 outcomes 只读诊断 |
| `tests/test_temporal_boundaries.py` | 本批回归 ×25（新文件） |
| `tests/test_memory_log.py` | 夹具适配（DatetimeIndex、基准短缺端点新行为） |
| `tests/test_review_round2_regressions.py` | 夹具完善（保留历史日期 + 公告日期，断言不变） |

## 请求 Codex 审核

1. A04 的修订语义：修订列存在而值无效 → 剔除（保守），是否符合线上数
   据实际（若线上修订列对未修订行为空，历史模式会把未修订行也剔除——
   保守方向的取舍已按边审断言实现，请确认接受）。
2. A08 取数到「今天」：native end 含当日（回填通常发生在收盘后）；若需
   排除当日盘中未收盘价，可改为昨日，请定口径。
3. 迁移方案（诊断 + 用户手工恢复 pending + 备份建议）是否满足「有范围
   的失效/重算策略」要求，或需要提供脚本化工具。
