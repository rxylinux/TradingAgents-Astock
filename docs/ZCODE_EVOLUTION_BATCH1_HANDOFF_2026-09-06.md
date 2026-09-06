# ZCode 演进第一批交接（N01 质量卡与结论限制 + N02 运行档案与版本存档）

日期：2026-09-06 · 分工：Codex 计划与验收，ZCode 编码
基线：上一轮已验收工作区（753 passed 基线），本批改动**未提交**。
Codex 的十份独立审计脚本（含本轮新增 `docs/audit_evolution_runs_
2026_09_06.py` 15 例与 `docs/audit_evolution_quality_2026_09_06.py` 15 例，
随边写边审持续扩充）均未改动，全部随默认 pytest 收集执行。

## N01 — 结构化报告质量卡与结论限制

**新模块 `tradingagents/agents/report_quality.py`**：

- `assess_reports(state, active_analysts)`：复用 `quality_gate.
  _hard_check_report`（单一评级来源，无第二套算法、无额外模型调用），
  产出 JSON 可序列化质量卡：`schema_version=1`、`status`、逐角色
  `grade/detail/chars`、`active_count`、`fail_count`（仅 D/F，未启用不计）、
  `limitations`（由实际硬检查产生）。
- 状态规则（固定）：全部 A → `complete`；D/F ≥ 严格多数 `n//2+1` →
  `insufficient`；其余存在 B/C/D/F → `limited`；无可检查集合/旧记录 →
  `unknown`。状态是**报告完整性检查**——所有渲染出口（卡片/提示词/标题）
  都明确不是事实准确率/胜率/置信度。
- `render_limitation_notice` / `append_limitation_notice`：确定性受限提示，
  limited/insufficient 时由 PM 节点**代码级追加**到 `final_trade_decision`
  （幂等判定匹配**当前完整通知文本**——Codex 边审：模型复述标记或残留的
  旧通知不得压制当前真实限制；提示恰好一次）；complete/unknown/缺失不
  追加（不凭空生成「检查通过」、不伪装成中性）。追加后随现有记忆保存路
  径进入复盘，限制保留。
- `quality_context_for_prompt`：RM/交易员/PM prompt 的精简限制段；
  **unknown 返回空串**（Codex 边审：unknown 永不变成 passed 提示）。
- `render_quality_card_md`：Web/Markdown/PDF 三出口共用的卡片渲染；旧
  报告显示「未记录结构化质量检查」。

**接线**：`AgentState.data_quality` 字段；质量门控同时返回
`data_quality` 与原 `data_quality_summary`（旧文字接口不变；LLM 自述只
在文字 summary，不覆盖硬检查）。下游注入（个股与指数双路径）：研究经理、
交易员（个股 `trader.py` + `create_index_trader`）、组合经理（个股 +
`create_index_portfolio_manager`）prompt 直接拼接质量段——不依赖辩论转
述；两个 PM 生成决策后代码级追加确定性提示。统一 JSON 保存 `data_quality`；
Web 报告前部质量卡 expander；MD/PDF sections 置顶同一卡片；原
`data_quality_summary` 文字仍可在数据质量 expander 查看。

## N02 — 运行档案与版本存档

**新模块 `tradingagents/run_records.py`**：

- `new_run_metadata(config, ticker, trade_date, instrument_type)`：
  `schema_version=1`、`run_id`（uuid4 hex，`validate_run_id` 限 32 位小写
  hex——版本目录名据此拒绝路径穿越）、`created_at`（UTC 带时区 ISO）、
  `config_snapshot`（白名单深拷贝）、`config_fingerprint`（规范排序 JSON
  的 SHA-256）。
- **白名单快照**（字段级 + 值级双重净化，Codex 边审逐轮加固）：
  分析师集合（排序归一）、模型/provider 公开配置（两档模型、订阅覆盖、
  SDK 模型与降级、推理力度、辩论早停等 11 键）、类型/语言/回溯窗口/辩论
  轮次/输出限制；`role_llms` 只保 provider/model 且**必须标量字符串**；
  `data_vendors`/`tool_vendors` 只保**已知公开路由键**（未知键、嵌套
  dict、疑似凭据键名一律丢弃）。API Key/URL 凭据/请求头/环境变量/未知
  嵌套配置永不入档；快照不引用调用方可变字典（构造后修改原 config 不影
  响已生成档案）。
- 指纹对集合顺序与 dict 顺序不敏感；模型/窗口/数据源/推理力度等改变必然
  改变指纹。档案**表示配置**，不声称记录每次请求实际落到的供应商（SDK
  后备仍可能改变实际路径）；本指纹用于档案与对比，不新增恢复拒绝机制
  （团队兼容拒绝保持上一轮语义）。

**生命周期**：`prepare_graph_run` 在 fresh 初始 state 写入
`run_metadata`（每次 fresh 必然新 ID）；SQLite 恢复路径不经过该处——
同一 run 恢复沿用 checkpoint 中的原 ID/配置/created_at（Codex 实测：
恢复后 metadata 与原始逐字段相等；旧断点无该字段 → 保持未记录，不伪
造）。不改变任何既有 checkpoint key/thread_id。

**存档（`_log_state`）**：

```
results_dir/_runs/<run_id>/<ticker>/TradingAgentsStrategy_logs/full_states_log_<date>.json
results_dir/<ticker>/TradingAgentsStrategy_logs/full_states_log_<date>.json   # 最新兼容入口
```

- **先写版本、成功后才更新 latest**（Codex 边审：版本写失败时异常传播，
  绝不能已替换上一次成功的 latest）。
- 同 run_id 重复 finalize：内容相同 → no-op（不产生额外历史项、不重写已
  发布文件）；**内容冲突 → 显式 RuntimeError 拒绝**，存档与 latest 都不
  动（已发布记录不可变）。无档案（旧流程/旧断点）只写兼容路径。
- 原子写：唯一临时文件（pid+tid）+ `os.replace`；序列化失败不留完整名
  的半份文件。
- `run_metadata` 随 JSON 保存（旧断点 → null = 未记录）。

**历史与侧栏**：`history.get_history` 双路径扫描——版本目录（同 ticker/
date 的不同 run 各自保留）+ 兼容入口；**畸形 JSON 整条跳过**（warning 诊
断、不以目录名伪装身份、不拖垮列表）；同 run_id 的版本+兼容文件只显示一
次（版本路径优先，身份以文件内容为准，与目录名不一致时告警）；条目新增
`run_id`/`created_at`，排序 date → created_at 降序。侧栏按钮 key 基于
唯一记录（`hist_{t}_{d}_{run_id}`，旧记录回退规范化路径 hash），标签带
运行短 ID 与创建时间——同日多版本可区分且 key 不冲突；旧记录显示
「历史版本」。

## 新增回归（tests/test_evolution_batch1.py，33 例）

状态规则（单角色 D=insufficient、偶数半数=limited、严格多数、unknown）、
双输出、LLM 不覆盖、提示幂等/complete 不附/unknown 空、prompt 上下文、
卡片三出口、JSON 保存；档案（唯一 ID、白名单秘密剔除、顺序不敏感指纹、
深拷贝）、SQLite（恢复同 ID、同实例 fresh 新 ID、旧断点不伪造）、存档
（同日两 run 保留、冲突拒绝、无档案单路径、去重、畸形跳过、穿越拒绝、
原子性）、侧栏 key 唯一性。

## 验证记录

```
默认完整回归（tests + docs 十份审计自动收集），最终一次实测输出：
  824 passed, 14 skipped, 52 subtests passed, 5 warnings
  其中 Codex 冻结的演进审计 38 例（evolution_quality 21 + evolution_runs
  17）全部通过；此前八份历史审计（71 例）保持通过。
跳过 14 项的准确构成：13 × claude-agent-sdk（可选依赖未安装）、
  1 × langchain-google-genai（Gemini；因与 mootdx 的 httpx 版本约束冲突
  默认不安装，见 #87）。
ruff check tradingagents web cli tests: 通过
git diff --check: 通过
```

全程离线（requests 与 yfinance 均有断言级拦截）；未动真实缓存/断点/记忆
与凭据；未提交未推送。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `tradingagents/agents/report_quality.py` | 新：质量卡/状态/提示/渲染 |
| `tradingagents/run_records.py` | 新：运行档案/白名单/指纹 |
| `tradingagents/agents/quality_gate.py` | 双输出（data_quality + summary） |
| `tradingagents/agents/utils/agent_states.py` | `data_quality` / `run_metadata` 字段 |
| RM / trader / PM（个股+指数 共 5 节点） | prompt 质量段 + PM 确定性追加；trader/PM 缺键容错、trader name 默认值 |
| `tradingagents/graph/trading_graph.py` | prepare 生成身份；_log_state 版本+latest 顺序/冲突/原子 |
| `web/history.py` | 双路径扫描/去重/畸形跳过/新字段 |
| `web/components/sidebar.py` | 唯一按钮 key + 运行标签 |
| `web/components/report_viewer.py` / `web/pdf_export.py` | 质量卡（与 MD/PDF 同渲染） |
| `tests/test_evolution_batch1.py` | 本批回归 ×33（新文件） |

## 已知限制（如实声明）

- 质量状态是完整性检查，不代表事实准确率/胜率；本轮未做事实核验（N04
  路线）。`complete` 时 prompt 告知「全部通过硬检查」——这是本运行记录
  的事实，不是为旧报告凭空生成（旧/unknown 均为空段或「未记录」）。
- 指纹覆盖白名单公开配置；未入档的秘密配置（如自定义网关 URL）差异不会
  体现在指纹中——这是安全取舍，不是遗漏。档案不记录实际请求路径。
- 旧断点（无 run_metadata）恢复后完成的报告只写兼容路径（无版本存档），
  历史列表仍可见；不迁移旧数据。
- `insufficient` 时 PM prompt 收到限制且代码追加提示，但不强制改评级
  （保留模型原评级，符合任务书「保留原评级」）。
- N03–N08 未实现（路线图）；本批未加 LLM 调用、未加网络路径。

## 请求 Codex 审核

1. 发布锁以 latest 路径为锚（同 ticker/date 范围）——若你希望以独立
   锚文件或更粗/细粒度范围，请指出口径。
2. `role_llms` 嵌套 model 值（非标量）整项丢弃 vs 字符串化保存——按你
   的审计语义实现为丢弃。
3. 侧栏标签格式（`运行 xxxxxxxx · 时间`）是否满足「必要标签」要求。

## 第一批收口补记（Codex 验收后小修，随第二批一并交付）

- `validate_run_id`/`is_run_metadata` 改用 `fullmatch`（拒绝末尾换行等尾
  随字符）。
- `history._read_run_info` 增加非法 UTF-8 单条跳过 + 警告。
- 跳过项措辞纠正：13 × claude-agent-sdk + 1 × langchain-google-genai
  （Gemini，#87 依赖冲突）。
