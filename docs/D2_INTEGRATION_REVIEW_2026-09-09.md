# D2 独立审核

## 最终独立验收（2026-09-09 00:37）：D2 范围通过

R1 六项独立审计全部通过，与 ZCode 20 项集成测试合并为 **26 passed in 1.93s**。已复核共用提示词完整归属校验、运行元数据摘要锚定，以及 SQLite 测试实际 `invoke(None)` 完成续跑并保持原卡。输出预算将情景计入总行数并预留缺口/限制说明。

独立全量：**1286 passed, 14 skipped, 52 subtests passed, 5 existing warnings in 63.41s**，退出码0。仓库 ruff 与 `git diff --check` 通过。290文件测试前快照与测试后比较，生产和测试文件均未变化；只有 E 草案修订及新增 Codex E 契约（均为文档）。快照：`/var/folders/1k/6mfcfpgd38bgh4s31fz6wsv00000gn/T/codex-d2-final-wscgwwij/before.json`。

验收文件 SHA-256：

```text
e27f599c5080bf8c29a4ec538f4ebc8e28354ca552a2377146d47c0995a78a57 tradingagents/dataflows/financial_panel.py
90bc93f6a9606fdc4cab87629b99726f8212a328e2c20ffb41e9291169542610 tradingagents/graph/trading_graph.py
281df2a8b52baeeddaf486c7220cc907229b7f205e83147333b8302b2041f1f5 tests/test_financial_panel_integration.py
40cb3b7941340d1a7a187f641d358144bb890134c680eec63a6ada186d11d8f7 docs/audit_d2_integration_2026_09_09.py
```

通过范围是 D2 准备/提示词/持久化恢复与入口工程契约；不代表模型文案不会算错，也不代表投资效果已改善。下方 R1 保留作历史。批准立即按 [E Codex 实施契约](E_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md) 开始 E1/E2，稳定交接后独立审核。

## R1（00:22）：暂不验收，归属/摘要校验缺口

Codex 新增独立脚本 `docs/audit_d2_integration_2026_09_09.py`，未改业务实现或 ZCode 测试。六项独立检查 4 failed / 2 passed；与 ZCode 17 项 D2 测试合并：

```text
.venv/bin/python -m pytest -q docs/audit_d2_integration_2026_09_09.py tests/test_financial_panel_integration.py --tb=short
4 failed, 19 passed in 0.94s (exit 1)
```

失败四例：

1. `panel_context_for_prompt` 仅检查 run_binding 是 dict，未比对 state.run_metadata.run_id。将 state 改为另一个 run 后仍输出 20.00% 等原卡数值，违反直接调用无效卡也不得暴露数字的契约。
2. 卡 inputs 的 normalized_value 被修改、与 manifest_digest 不符，直接提示词 helper 仍输出原卡数值。调用完整归属/摘要验证，失败给受控 invalid-panel、不展示数值。
3. `_validate_resumed_panel` 只核对卡内 digest 与卡内 inputs，自洽不等于属于这个运行。将 run_metadata.financial_panel.manifest_digest 改为 DIFFERENT 仍不报错。必须将卡摘要与独立运行元数据摘要比较，连同 instrument/type/date/run_id 检查。真正旧无卡断点兼容；有卡而锚定摘要缺失不能自动自证有效。损坏 checkpoint 保留，模型调用前失败，不清空/伪装缺失。
4. 真实 `create_portfolio_manager` 工厂（离线捕获 LLM，单次调用）接 cross-run state 后确实把 20.00% 送入提示词，证明不是只有纯函数边界问题。修复要覆盖五个真实工厂并保留正确卡单次调用控制例。

正确卡投影有数值控制例通过，当前长清单总字符测试通过。初版 PM 审计 fixture 缺风险状态字段导致 KeyError，Codex 已补齐 fixture 后复跑，上述最终失败是实际提示词包含异 run 数值，不把最初 fixture 错误算产品缺陷。

修复建议：共用完整校验例程接受可信 state/context 与 run_metadata 摘要；格式损坏返回明确校验失败而不是抛出无说明的 KeyError。提示词校验前不得读取数值。保留 D1 纯计算语义，不为修复要求真实取数或额外模型调用。现有 ZCode factory 测试有仅 card、无可信运行身份的输入，应按生产 fresh state 补齐而不是继续让无身份卡通过。

相邻契约核对（本轮未新增失败测试，不冒充复现结果）：

- 现有 SQLite 测试只 prepare 并检查 next==node_b，尚未 invoke(None) 完成真正续跑；补上执行与节点计数、最终卡不变验证。
- 15 数值行预算需要包含三个情景行，总字符预算包含截断说明；当前代码明显以 15 个 metrics 再追加 scenarios，总长截断后又加尾注。保持限制说明独立，不能因为前段占满字符而消失。
- 验证未知版本、错误标的/类型/日期和 metadata 摘要缺失；不要只覆盖卡输入自身篡改。

继续修 D2 R1，定向通过后稳定交接。E 文档设计可继续，未经审核不进入 E 实现；不重复全量、不编辑 Codex 审计。

本次 SHA-256：

```text
82345e8b1bbeedb827a928a4d0a78aadb159b6da2993df933f0fd4bf908b517d tradingagents/dataflows/financial_panel.py
e6949311604a3432bba47a1c7a5df030ff74f2f6c90a7998fe01a4321ccb7216 tradingagents/graph/trading_graph.py
40cb3b7941340d1a7a187f641d358144bb890134c680eec63a6ada186d11d8f7 docs/audit_d2_integration_2026_09_09.py
```
