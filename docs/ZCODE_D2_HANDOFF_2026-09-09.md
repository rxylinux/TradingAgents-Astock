# D2 交接（D2a + D2b，ZCode → Codex）

日期：2026-09-09。权威契约：`docs/D2_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`（九条，未改动）。上游 D1 已独立验收。D2 不新增任何 LLM 调用/网络/取数/记忆写入；D1 数学语义零改动（64 项面板测试原样通过）。

## D2a：准备层 + 提示词投影 + 恢复隔离

**投影**（`financial_panel.panel_context_for_prompt`，规则 2/8）：

- 只渲染 `status == ok` 的指标与情景值，及其实际参与依赖（D1 依赖链天然只含 eligible 输入）；**完整原始输入表（含 future/unknown 排除声明）只留存在审计 JSON，绝不进提示词**；
- 依赖出处中的机器路径被遮蔽（`[路径已隐去]`）；manifest 路径不进卡/元数据/提示词；
- 上限：15 数值行、单行 220 字符、总 4000 字符，全部截断明示；
- 零 ok 面板仍输出缺口分类（按 reason_code 计数）与关键 limitations——非空；
- helper 直接遇到无效/未绑定卡 → `invalid-panel` 受控提示，**不暴露任何数字**（正式流程由准备层阻断）。

**准备层**（`trading_graph._prepare_financial_panel`，规则 1/3/4/7）：

- `config["financial_panel_manifest"]` 缺省 → 无卡（个股/指数一致；不为指数伪造 dummy 输入；指数 not_applicable 卡只来自显式 manifest 经 D1 校验）；
- fresh 在图执行/模型调用前完成：读取（2MB 字节上限、UTF-8、JSON 对象、≤500 条输入上限）→ D1 纯计算（可信 instrument/type/trade_date，manifest 不可覆盖）→ `bind_run` 绑定本 run → 注入 `initial_state`；任何失败 `ManifestError` **fail-fast**（零模型调用）；
- `run_metadata.financial_panel` 只存 `{manifest_digest, overall_status}` 可复验摘要（路径不替代 digest）；
- 身份区分：同 manifest 两次 fresh → 数值与 digest 相同、`run_binding.run_id` 不同（新 fresh 必然新 run_id）；计算纯函数不感知 run_id，绑定在注入层。

**恢复**（`_validate_resumed_panel`，规则 5/6）：

- `_prepare_graph_run` 先判 resume 再处理 fresh 输入（原有顺序未动）；
- 恢复**只读 checkpoint 原卡**：不打开当前 manifest（测试以"恢复前删除 manifest 文件"证明）；恢复时才配置 manifest 也不给旧无卡断点补卡；
- 归属校验：`run_binding.run_id` vs `state.run_metadata.run_id`、卡内 instrument/instrument_type/analysis_date vs 可信上下文、`manifest_digest` vs 内嵌输入表复算——任一失配 → 准备期 `RuntimeError`（逐项列出）**在任何模型调用之前**，断点原样保留（不清空不覆盖，不伪装成未记录）；键缺失才是合法旧态"未记录"。

**五工厂接入**（规则 8）：个股 RM/Trader/PM + 指数 Trader/PM（指数 RM 共用个股工厂）注入 `panel_block`，与 quality/evidence 块并列；无额外 LLM 调用（调用次数与无面板一致，测试断言）。

## D2b：CLI + Web 入口（规则 7）

- **CLI**：`tradingagents analyze --financial-panel-manifest PATH`（及裸跑默认路径同名旗标，缺省空=不注入；`tests/test_cli_default_command.py` 断言随默认路径透传且缺省不注入）。
- **Web**：侧栏「高级：财务面板」expander——明确标注的本机路径输入 + 可选上传（写入本次会话独立临时目录 `tempfile.mkdtemp`，2MB/UTF-8 预检，不改用户历史/缓存；恢复不依赖临时文件存在）；`_build_config` 仅在提供时写入 `financial_panel_manifest`。

## 验证（pytest 定向，未跑全量）

```bash
.venv/bin/python -m pytest tests/test_financial_panel_integration.py tests/test_financial_panel.py
# 81 passed（D2a 17 项含：真实 SqliteSaver interrupt→resume 删文件不重读/卡逐字节不变/
# 原节点不重跑 next==node_b；旧无卡断点不补卡；run_id 失配与 digest 篡改拒绝且断点保留；
# 两次 fresh 数值同 run_id 异；默认无卡（个股+指数）；显式指数 manifest not_applicable 无 dummy；
# 非法/标的不一致/文件上限 fail-fast 零模型调用；future sentinel 不入真实 PM 提示词而审计 JSON 保留；
# 调用次数有无面板一致；五工厂覆盖；零 ok 限制存活；invalid 无数字；三出口一致）

.venv/bin/python -m pytest -q docs/audit_d1_financial_2026_09_08.py docs/audit_c2_thesis_2026_09_08.py docs/audit_c1_evidence_2026_09_08.py docs/audit_optimization_news_2026_09_08.py docs/audit_optimization_retry_2026_09_08.py
# 49 passed（全部独立审计无回退）

.venv/bin/python -m pytest tests/test_memory_log.py tests/test_index_support.py tests/test_report_and_history_contract.py tests/test_pdf_export.py tests/test_c2_thesis_card.py tests/test_c1_evidence_graph.py tests/test_evidence_ledger.py tests/test_checkpoint_resume.py tests/test_cli_default_command.py
# 304 passed（触达模块定向回归）

.venv/bin/python -m ruff check <全部 D2 触达文件>
# All checks passed!
```

## 文件清单（SHA-256）

```
82345e8b1bbeedb827a928a4d0a78aadb159b6da2993df933f0fd4bf908b517d  tradingagents/dataflows/financial_panel.py
e6949311604a3432bba47a1c7a5df030ff74f2f6c90a7998fe01a4321ccb7216  tradingagents/graph/trading_graph.py
140c8490284614e08a338e95a004f36f4165fac3aa6c85807cc39e1cfddfea66  tradingagents/agents/managers/research_manager.py
ce0b8d87e317787e0559db4c213aae2d3e8388200932e99d7a47188dfc024d70  tradingagents/agents/trader/trader.py
e15a86f37e6690f0ca7212cfe92e943a3cd32967d3581a06029340fe79327a01  tradingagents/agents/managers/portfolio_manager.py
633f1c6fc78afc5e5c005e2b0a42c6f3b7cd0adc756fe6bb3988d6a6e815259e  tradingagents/agents/index_agents.py
118ce071fc06fc98cd0d7a91b7679336f31bf22564146aaaa976ee5762069a3d  cli/main.py
5c248a5c34abed47df15edb4a52b7f45b06c588fa5e43547b1a4c72953df57cd  web/app.py
fcfd12bd4c75ce512b79e3166ea8bf0d0e4505adc7aa4fd7fbdf7f8616499ae1  web/components/sidebar.py
f8df9ae3d9b688c0a367de27b84ab9df7bb18aaa9cce39cef5c0ecd18bd8bd7f  tests/test_financial_panel_integration.py
4cbb57b01baaf5330c74eccedecfa998385bd5affc37c679e74c4a2466897efe  tests/test_cli_default_command.py
```

## 限制（如实声明）

- D2 只接显式离线 manifest；无真实 vendor 抓取；面板未配置时所有现有行为零差异。
- 提示词投影不能保证模型文案永不写错数——代码表（state/统一 JSON/三出口）始终保存供逐值复核（契约原话保留）。
- 上传临时文件仅供 fresh 注入；恢复不依赖它（契约规则 7）。
- SQLite 恢复实测经由 `_prepare_graph_run` 真实路径 + `get_checkpointer` 重开同一 DB；生产 `TradingAgentsGraph` 全链路（含 LLM）不在离线测试范围。

---

## R1 修正附录（2026-09-09 00:3x，对应 docs/D2_INTEGRATION_REVIEW_2026-09-09.md）

Codex 独立审计 6 项 4 failed → 修复后 **6/6**（脚本未改动）；与 ZCode 测试合并运行全绿。

修正内容：

1. **提示词 helper 全量归属校验（读任何数值之前）**：新增 `_trusted_problems(state, card)` 共用校验——run_id（vs `state.run_metadata.run_id`）、instrument（vs `company_of_interest`）、instrument_type（state 提供时）、analysis_date（vs `trade_date`）、**独立元数据摘要锚定**（`run_metadata.financial_panel.manifest_digest`，缺失即 invalid——有卡不能自证）、卡自身 digest 复算（内嵌表篡改检出）。任何失败 → `invalid-panel` 受控提示 + 明确原因行，**不暴露任何数值**；格式损坏返回明确校验失败而非 KeyError。
2. **恢复校验补锚定**：`_validate_resumed_panel` 现传入 `metadata_digest`（来自 checkpoint 的 `run_metadata.financial_panel.manifest_digest`）——卡摘要与独立运行元数据摘要不符/锚缺失 → 拒绝恢复（断点保留、模型调用前）；真正旧无卡断点（键缺失）仍为合法未记录。
3. **真实工厂防线**：五个真实决策工厂经由同一 helper 获得 protection——cross-run 卡在**实际 PM 提示词**中只出现 invalid-panel（新增真实工厂测试断言单次调用 + 无 20.00% + invalid-panel）。
4. **测试夹具修正**：工厂/投影测试的 state 补齐生产 fresh 的完整可信身份（run_id + financial_panel 锚定 + instrument_type/trade_date）——不再允许无身份卡通过；指数工厂改用真实指数卡（not_applicable，无数字）而非股票卡冒充。
5. **预算修正**：15 数值行预算现**包含三行情景**（metrics 行数 = 15 − 情景行数）；总字符上限**包含截断说明本身**——截断只削数值行，缺口/限制块先行组装永不因数值占满而消失。
6. **相邻项**：SQLite 恢复测试现真正 `invoke(None)` 完成续跑——node_b 执行（sender=="b"）、`next` 清空、终态卡与断点前逐字节一致。

验证（定向，未跑全量）：`tests/test_financial_panel_integration.py` **20 passed**（新增 cross-run 工厂/锚缺失/内嵌表篡改/invoke(None) 完成四类）；六份独立审计合计 **55 passed**（D2 6/6、D1 11/11、C2 8/8、C1 7/7、A 12/12、B1 11/11）；触达模块定向回归 **367 passed**；ruff 全绿。D1 数学语义零改动（`tests/test_financial_panel.py` 64 项原样通过）。

### R1 后文件清单（SHA-256，以此为准）

```
e27f599c5080bf8c29a4ec538f4ebc8e28354ca552a2377146d47c0995a78a57  tradingagents/dataflows/financial_panel.py
90bc93f6a9606fdc4cab87629b99726f8212a328e2c20ffb41e9291169542610  tradingagents/graph/trading_graph.py
281df2a8b52baeeddaf486c7220cc907229b7f205e83147333b8302b2041f1f5  tests/test_financial_panel_integration.py
```
