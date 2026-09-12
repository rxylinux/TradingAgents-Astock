# C2 独立审核

## 最终独立验收（23:29）

C2 R1 的 7 个失败已修复，完整控制例保持 assessable。C1/C2 首批按已定义契约通过独立验收：

- 四份独立审核脚本：38 passed in 1.85s，exit 0。
- `.venv/bin/python -m pytest -q`：**1185 passed / 14 skipped / 52 subtests / 5 existing warnings**，53.05s，exit 0。
- `.venv/bin/ruff check .` 与 `git diff --check`：通过。
- 271 个文件 hash 前后比较：只有 Codex 同期追加的 D 契约文档变化；其余 270 个文件（包含全部生产代码与测试）不变，无新增文件。HEAD 仍为 d269ba43047fe23c2e60307d5ca83425578f8080，未提交/推送。
- 当前 C1/C2/ledger 三份测试实际收集 108 项；ZCode R1 交接的 90 项数字不准确，不沿用该数字。

最终修复 hash：thesis.py `158c12f421ba402a8e34c21b5a8887278d78277423068e862fcb22283971f24d`；ledger.py `da466a265d9a834573db660bb814bbc5e871e243a5ca7466fb93a9c9622a23a5`；test_c2_thesis_card.py `4036081d5acc7dd58d3f76a0fc6661a6be840f581ca029d9ca3d95c171931f74`。

验收范围：历史新闻/错误语义、OpenAI 同步调用预算、新闻证据从实际图工具到状态/决策提示词/报告，以及同一次 PM 调用生成的确定性假设卡；个股与指数共享契约。十四项跳过涉及未安装的可选 SDK/Gemini。未做真实模型准确率或投资效果验证；引用存在且时点合规不等于论点受支持。后续 D 的变化需另行验证，不继承本次全量结论。

已让 ZCode 根据 D 契约末尾 Codex 修正继续 D1 编码，D2 另做明确输入进入研究流程的接入。

## R1（23:15）：暂不验收，7 个缺口复现

Codex 只新增独立审核脚本，未改业务实现或 ZCode 测试。C1 R2 的 7 项独立审计已通过；A/B1 23 项仍通过。ZCode 当前 C1/C2/ledger 96 项亦通过，但下列新边界未覆盖。

真实命令及结果：

```text
.venv/bin/python -m pytest -q docs/audit_c2_thesis_2026_09_08.py tests/test_c1_evidence_graph.py tests/test_c2_thesis_card.py tests/test_evidence_ledger.py --tb=short
7 failed, 97 passed in 1.51s (exit 1)

.venv/bin/python -m pytest -q docs/audit_c2_thesis_2026_09_08.py docs/audit_c1_evidence_2026_09_08.py docs/audit_optimization_news_2026_09_08.py docs/audit_optimization_retry_2026_09_08.py --tb=short
首次版本 6 failed, 31 passed in 2.05s (exit 1；随后新增缺分析日用例，上条已执行)

.venv/bin/ruff check docs/audit_c2_thesis_2026_09_08.py
All checks passed! (exit 0)
git diff --check
通过 (exit 0)
```

失败与修复契约：

1. `prediction_horizon='unknown'` 和 `'未知'` 都得到 `assessable`。非空字符串并不表示已声明期限。规范化空白与常见 unknown 占位；保留原文供审计，不臆造期限。不需要本轮发明自然语言期限解析器。
2. 失效条件 indicator/comparator/threshold/period/source 全为 `unknown`，仍 `observable=True`。需要排除占位值；无法确定比较语义的自由文本只能待人工判断，不能因四个字段非空就声称可观察。
3. 失效条件未给 observation source 仍 `observable=True`。按原 C2 契约，实际所需来源必须明确；缺来源保留条件并标待人工判断。不要编造财报/数据源。
4. 失效条件 `evidence_id='ev-does-not-exist'` 完全绕过校验，卡片仍可评估。所有已提供引用均需校验，包含 condition 的引用；让校验结果区分支持/反驳/条件的位置。条件可不填引用，但一旦填入不能默认为有效。同样覆盖 future / unknown-time 条件引用。保留“ID 和时点有效不等于语义受支持”的边界。
5. 账本有 `HISTORICAL_COVERAGE_GAP: archival source unavailable` 覆盖限制，但卡片 `assessment_reasons=[]` 且 assessable。C1 提示词已经保留这些缺口，C2 又丢掉。原样带入相关覆盖限制及未知/失败事件，并确定性降为 limited；不能只看 source_statuses 的 failed/partial。规范可得性提醒与失败缺口可区分，但不得静默忽略明确缺口。不依赖只匹配这个测试 sentinel。
6. state 缺 `trade_date` 时卡片 analysis_date=unknown，引用仅查存在而未校验时点，仍 assessable。C2 必须要求可验证分析时点，缺失/非法时点给 unknown 或 limited 和原因。不要更改 C1 通用函数合法的“只查存在”调用语义来掩盖 C2 前置条件缺失。

完整明确信息控制例仍 assessable，禁止一刀切将所有卡片降级刷绿。修复后保持 stock/index 同一构建契约，状态与决策文本/JSON/共享展示一致，不增加 LLM 调用。覆盖限制、unknown-time 与不存在 ID 最好使用结构化原因类别，避免依赖中文 reason 的包含匹配误归类。

## 稳定快照

```text
db81d8976907c7fbfd3ca140b14d854269783605bcb3a72c142b20fb69aacbd2 tradingagents/agents/thesis.py
d25390ba0862896b4f679b684c6a47d51c992d4a673e2c880c5c23568618d97a tradingagents/agents/schemas.py
39b9a67e3aa6bbc7419c1867b87a462e0395cf57f2b7a69c66973e9e945626d0 tradingagents/agents/index_agents.py
95d3295f9df8dffa5cd97198a2dbe0ff8dedd090b53ed95a0c47834ca4426bf8 tradingagents/evidence/display.py
5d6cdb45e597a72b56c78f9edce35124f135405234d5cd559c19cc68e84e8e44 tradingagents/evidence/prompt_context.py
2497dd1596831bd1e3ced96df38290acf78285b170441bcb6f83eae844926733 docs/audit_c2_thesis_2026_09_08.py
```

下步：先修 C2 R1，定向验证后写稳定交接；Codex 再做完整回归。继续准备 D 明确输入与公式契约，无须等用户确认。当前不重复全量，不宣称预测效果提高。
