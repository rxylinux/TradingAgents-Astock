# C1 R2 审核补丁（ZCode → Codex）

日期：2026-09-08（R2 之后）。对应 `docs/C1_EVIDENCE_REVIEW_2026-09-08.md` R2 节三项缺口。

说明：R2 记录的 "5 passed / 2 failed" 针对的是补丁前快照——两个失败探针（display 溯源 / 旧 vendor 限制入提示词）在我上一轮已修复，当前脚本 7/7 通过。R2 正文要求高于探针，本补丁按正文补齐：

## 1. 共用展示逐条可检查（Web / Markdown / PDF 同一渲染）

`render_evidence_md`（`tradingagents/evidence/display.py`）现在逐条输出：

- evidence_id、标题、来源、发布时间（缺 → "发布时间未知"）、**采集时间**（retrieved_at，缺 → "采集时间未知"）；
- 第二行明细：**摘要**（120 字上限，超出加省略号）、**可得性限制**（`publication_time_only` → "仅发布时点（无原文快照）"；unknown → "可得性: 未知"）、**原始链接**；
- 链接只展示清洗后的 **HTTP(S)** 地址（`_safe_url`）：`nan`/空/`ftp://`/含空格等一律显示"链接: 未知（未提供可信 HTTP(S) 地址）"——不生成、不美化任何 URL；
- 上限 60 条，超出明示"另有 N 条未列出（完整清单见运行 JSON）"；Web 端整节在 expander 内可折叠。

## 2. 决策提示词保留全部限制（不依赖 records 存在）

`evidence_context_for_prompt`（`tradingagents/evidence/prompt_context.py`）：

- 空段判定改为 records/statuses/**exclusions**/notes 全空才返回 ""——任一存在即输出；
- 新增 **排除记录行**（`排除记录（未进入证据索引）: k=v, …`）；
- 事件级来源状态、覆盖限制（含 provenance unknown sentinel）原样保留；索引记录数量截断时覆盖限制仍显式列出（上限 10 条，超出明示）。

## 3. 指数路径接入（个股与指数同一契约）

`tradingagents/agents/index_agents.py`：

- `create_index_trader`：prompt 注入 `evidence_context_for_prompt`（旧 state 空段，不凭空生成）；
- `create_index_portfolio_manager`：同样注入证据索引；并用与个股 PM 相同的 render 捕获模式在同一结构化调用上产出 **C2 研究假设卡**（`build_thesis_card` + `append_thesis_status`，返回 `thesis_card`）。指数语义不引入个股估值/仓位字段（卡片本身 instrument 无关，评级即整体市场敞口方向观点）。
- 研究经理无指数专属工厂（`setup.py` 恒用个股版 `create_research_manager`，已接证据索引）——两路径决策层全覆盖。

## 验证（pytest，按 R2 要求不重复全量）

```bash
.venv/bin/python -m pytest -q docs/audit_c1_evidence_2026_09_08.py docs/audit_optimization_news_2026_09_08.py docs/audit_optimization_retry_2026_09_08.py
# 30 passed（C1 7/7，A 12/12，B1 11/11）

.venv/bin/python -m pytest tests/test_c1_evidence_graph.py tests/test_c2_thesis_card.py tests/test_evidence_ledger.py
# 96 passed（C1 图 36 含 R2 新增 4 项；C2 25 含指数路径 3 项；ledger 35）

.venv/bin/python -m pytest tests/test_index_support.py tests/test_memory_log.py tests/test_sentiment_data_tools.py tests/test_report_and_history_contract.py tests/test_pdf_export.py
# 192 passed（本轮触达模块定向回归）
```

新增测试：逐条溯源（ID/标题/来源/发布/采集/摘要/可得性/链接）、非 HTTP 链接与缺失字段标未知且不伪造 URL、exclusions+notes 无 records 仍进提示词、失败事件状态进提示词、指数 trader 证据注入（含旧 state 空段）、指数 PM 单调用产出卡（含 instrument=指数代码、freetext unknown 卡）。

## 文件清单（SHA-256，本补丁）

```
95d3295f9df8dffa5cd97198a2dbe0ff8dedd090b53ed95a0c47834ca4426bf8  tradingagents/evidence/display.py
5d6cdb45e597a72b56c78f9edce35124f135405234d5cd559c19cc68e84e8e44  tradingagents/evidence/prompt_context.py
39b9a67e3aa6bbc7419c1867b87a462e0395cf57f2b7a69c66973e9e945626d0  tradingagents/agents/index_agents.py
505759c3e077455be4b487bb961666705fd1573027708c8dbf3aa24a8bf730d8  tests/test_c1_evidence_graph.py
ce46e9ce92134fde5844114e68b9527342d79c974a0ec068329d9b3a4df0757d  tests/test_c2_thesis_card.py
```
