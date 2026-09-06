# ZCode 第 4 批交接（A09 / A10 / A11 / A12 / A14）—— 全工程整改收官

日期：2026-09-06 · 分工：ZCode 编码，Codex 方案与逐批独立审核
基线：工作区（含前三批全部未提交修复），本批改动**未提交**。
ZCode 未修改 Codex 的八份独立审计脚本。至此任务书 15 项问题（A01–A15）完成代码整改；Codex 最终独立验收已通过，适用范围与剩余验证边界见 `ZCODE_IMPLEMENTATION_TRACKER_2026-09-05.md`。

## 变更明细

### A09 — Web 恢复类型来自任务请求（P2）

- ``web/app.py::_build_config(run_request=None)`` **签名变更**（Codex 备注：
  原始 audit A09 以 AST 提取旧无参形态执行——新签名 ``run_request=None``
  时回退模块级 ``start_req`` 再回退侧栏状态，因此**旧无参调用语义仍成
  立**，audit 断言未改动即通过；如 Codex 决定适配显式参数形态，新签名
  为 ``_build_config(run_request: dict | None = None)``）。
- 分析类型（``instrument_type``）取自**本次任务请求**的
  ``analysis_type``（恢复任务的 start_req 携带正确类型——指数断点可撞上
  侧栏个股选择，反之亦然）；指数模式的分析师集合统一为指数预设
  （market/social/news/policy/hot_money），不再依赖侧栏勾选混入不适用的
  fundamentals/lockup。
- 启动路径只构建**一份** ``run_config = _build_config(start_req)``：同一份
  同时决定进度 stage_ids 与后台线程的实际图（原先 stage_ids 与
  ``run_analysis_in_thread`` 各自无参调用 ``_build_config`` 读不同状态）。
- ``web/runner.py`` 指数分支显式传
  ``selected_analysts=config.get("selected_analysts")``——图与进度阶段对
  齐，不再静默忽略配置。

### A10 — 质量门控只检查启用集合（P2）

- ``AgentState`` 声明 ``selected_analysts`` 字段；``Propagator.
  create_initial_state`` 新增 ``selected_analysts`` 参数（None=个股全集）；
  ``TradingAgentsGraph`` 构造时记住 ``self.selected_analysts``（顺序去重）
  并在 ``_prepare_graph_run`` 写入初始 state（恢复路径经 checkpoint 保留）。
- ``quality_gate`` 基于三层解析的启用集合：``state["selected_analysts"]``
  （新断点/初始 state）→ **工厂闭包 fallback**（``create_quality_gate(llm,
  active_analysts_fallback=...)``——``setup_graph`` 传入当前实际图的启用
  集合；旧断点无集合元数据时取当前图集合，**不再默认七位**：单 market
  恢复不再凭空 6 个 F、指数恢复不再 2 个 F，Codex quality_resume 实证
  修正）→ 全集七位（``create_quality_gate(llm)`` 独立无参调用的兼容默
  认）。硬检查只遍历启用角色；LLM 复审提示词动态生成 N 位分析师——硬检
  查、失败比例、提示词人数/角色三处一致。未启用角色不评级、不出现、不
  计失败；**已启用但空报告照旧 F**。
- **跳过 LLM 复审阈值为严格多数（``n//2+1``）**，不是 ``ceil(n/2)``：
  偶数团队恰好一半失败（2 位中 1 位、4 位中 2 位）不算多数、复审仍执行；
  1 位=1、7 位=4（与原行为一致）（Codex 退回修正 + 参数化回归）。
- **恢复团队不匹配拒绝**（``_refuse_team_mismatch_checkpoint``）：断点记
  录的 ``selected_analysts`` 与当前图启用集合不一致（如原 market 恢复成
  market+news）→ prepare 阶段 RuntimeError 明确拒绝，保留断点、不重跑、
  不删除、不调用节点——静默混用拓扑会让质量门控/进度/实际执行互相矛盾。
- **边界声明（不声称完整配置指纹）**：旧断点（无 ``selected_analysts``
  字段）与绕过 ``__init__`` 的异常实例**无法可靠验证原始团队**——此时按
  当前图集合执行（质量门控经工厂闭包），团队是否与原运行一致不可证明。
  完整的配置指纹校验（分析师集合之外的分析窗口/语言/数据源等）本轮未实
  现。

### A11 — 旧报告不隐藏新失败任务（P2）

- ``web/history.py::get_incomplete_history``：判定依据从「存在同
  ticker/date 完成报告」改为**断点存在性**——有有效断点 → 保留恢复入口
  （旧报告不代表这一次运行完成，fresh 重跑只清断点不删旧报告）；无断点
  且有完成报告 → 陈旧条目过滤；无断点无报告 → 保留（展示失败原因）。
- 成功完成后清理对应未完成记录：``web/runner`` 成功路径已有
  ``clear_incomplete_task`` 调用（保持）。
- 既有测试 ``test_completed_history_hides_incomplete_task`` 固化了旧行为
  （有报告一律隐藏）——按 A11 正确语义更新（断言未弱化：新增「有断点必
  保留」分支，保留「无断点+报告过滤」分支）。

### A12 — 历史目录与写入配置一致（P2）

- ``web/history.py::_results_dir``：固定 ``~/.tradingagents/logs`` 改为读
  ``DEFAULT_CONFIG["results_dir"]``——与写入端（运行配置的 results_dir）
  同一配置源，``TRADINGAGENTS_RESULTS_DIR`` 环境变量优先级自动一致。自定
  义输出目录中的真实 JSON 保存后可被列表找到并加载（回归覆盖）。

### A14 — 报告字段契约（P2）

- 新增共享访问器 ``web/report_fields.py``：
  ``trader_plan(state)``（canonical ``trader_investment_plan`` 优先，
  legacy ``trader_investment_decision`` 回退；两者都有只取一次）与
  ``quality_summary(state)``。Web 展示（report_viewer）、Markdown、PDF 导
  出全部改经访问器取值——实时与历史重载同一规则。
- ``TradingAgentsGraph._log_state``：统一 JSON 双写
  ``trader_investment_plan``（canonical）与 ``trader_investment_decision``
  （legacy 别名，旧读者兼容）+ 新增 ``data_quality_summary`` 落盘；全部
  字段容错读取（部分完成的恢复路径缺字段不再 KeyError）。
- **Codex roundtrip 补验修正**（``docs/audit_report_roundtrip_2026_09_05.py``
  12 例 = 4 状态 × 3 出口）：共享导出 sections（``_collect_sections``，
  MD/PDF 同源）新增「数据质量评估」section（经 ``quality_summary`` 访问
  器）——此前 MD/PDF 漏质量结论；Web ``render_report`` 新增「🎯 最终交易
  决策（组合经理）」正文区——此前只显示 ``investment_plan``（研究经理意
  见），``final_trade_decision``（组合层面最终结论）正文从未展示。交易员
  canonical 优先、只出现一次、legacy 别名不赢的断言在 4 状态下保持。

## 新增回归（tests/test_report_and_history_contract.py，25 例）

- A09 ×4：指数请求覆盖个股侧栏（类型+预设）、反向、显式参数优先于模块
  请求、无请求时侧栏生效（AST 执行真实 ``_build_config``，不启动 UI）；
- A10 ×11：单 market 无 F 且 LLM 提示只含 1 位、混合子集、指数五位、启用
  但空报告 F、独立调用无集合时回退全集；另含五种严格多数阈值场景与旧状态的实际图集合回退；
- A11 ×3：旧报告+有断点保留、无断点陈旧过滤、完成后清理；
- A12 ×2：自定义目录可见、真实 ``_log_state`` 保存→列表→加载闭环；
- A14 ×5：canonical 优先/legacy 回退、Markdown 用 canonical、落盘双字段+
  质量结论+访问器一致、部分 state 安全落盘。

## 验证记录

```
完整默认回归集（一次运行同时验收原测试与独立审计）:
  753 passed, 14 skipped, 52 subtests passed, 5 warnings
  = tests/ 单元与集成 682 例 + docs/ 八份独立审计 71 例
八份独立审核（docs/audit_*.py 全部）: 71 passed / 0 failed
  = repros(A01–A15) + temporal(时点) + run_isolation(运行隔离)
    + memory_process(跨进程锁) + context_cleanup(生命周期清理)
    + cli_lifecycle(A01 边界) + report_roundtrip(报告内容契约 12 例)
    + quality_resume(质量门控恢复边界 5 例：旧断点 fallback / 指数 /
      双向 / 团队不匹配拒绝)
  —— 15 项问题的独立断言与全部边审补充全部转绿
ruff check tradingagents web cli tests: 通过
git diff --check: 通过
```

跳过项仍为未安装的可选依赖（claude-agent-sdk / Gemini 的 langchain-google-genai）。全程
离线（本批无网络路径；此前批次已固化 yfinance 替身要求）；未触碰真实缓
存/断点/记忆/凭据；未提交未推送。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `web/app.py` | A09：`_build_config(run_request)` + 单一 run_config 驱动 |
| `web/runner.py` | A09：指数分支显式 selected_analysts |
| `tradingagents/agents/utils/agent_states.py` | A10：`selected_analysts` 字段 |
| `tradingagents/graph/propagation.py` | A10：初始 state 写入集合 |
| `tradingagents/graph/trading_graph.py` | A10：记住并传递集合；A14：落盘双写+容错 |
| `tradingagents/agents/quality_gate.py` | A10：启用集合贯穿硬检查/比例/提示词 |
| `web/history.py` | A11：断点存在性判定；A12：配置化目录 |
| `web/report_fields.py` | A14：共享字段访问器（新文件） |
| `web/components/report_viewer.py` / `web/pdf_export.py` | A14：经访问器取值 |
| `tests/test_report_and_history_contract.py` | 本批回归 ×25（新文件） |
| `tests/test_memory_log.py` / `tests/test_web_history.py` | 接缝适配（lambda 签名）/固化行为修正 |

## Codex 最终审核结论

1. A09 的 `_build_config` 兼容签名保留；实际启动路径已确认显式传入 `start_req`，同一配置驱动进度和后台图。
2. A10 使用严格多数 `n // 2 + 1`，单人 F/D 仍跳过复审；恰好一半失败仍复审。已独立核对新增回归。旧状态的集合回退与新断点不匹配拒绝也经过真实 SQLite 复验。
3. A11 接受有效断点优先保留入口的修复，本批不引入完整 run_id。尚未实现的配置指纹与历史索引边界已记录在 tracker。
4. Codex 独立默认回归 753 passed、14 skipped、52 subtests passed、5 warnings；源码、默认测试和八份独立审计的 Ruff 检查、`git diff --check` 均通过。

## 默认回归入口（独立审计纳入默认收集）

`pyproject.toml` 的 pytest 配置：`testpaths = ["tests", "docs"]`、
`python_files = ["test_*.py", "audit_*.py"]`——八份独立审计脚本**原样引用**
（不复制两套、不改行为断言），随每次默认 `pytest` 一并执行。合计
**753 passed / 14 skipped / 52 subtests / 5 warnings**；跳过项全部为未安装
的可选依赖（claude-agent-sdk / langchain-google-genai），不能视为已验证。任务书原「移入正常测试目录」按 Codex 最终口径更新为
「纳入默认收集」。

## 全工程收官状态

15/15 项修复完成（第 1 批 A15/A02/A01、第 2 批 A03/A04/A07/A08、第 3 批
A05/A06/A13、第 4 批 A09/A10/A11/A12/A14），各批交接见
`docs/ZCODE_BATCH{1,2,3,4}_HANDOFF_2026-09-05.md`；全部改动保留在工作区
未提交，Codex 已完成最终独立审核。验证边界及旧数据处理说明见 tracker；不能将本地通过解释为所有部署环境、实时数据源和旧数据都已经验收。
