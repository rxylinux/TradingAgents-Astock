# F3 独立离线验收样例

Codex生成并通过真实CLI验证，2026-09-09。全部是合成输入与预录输出，只证明工程与计算契约，不代表投资预测效果、收益或准确率提高。

两组pair（p/q）、两个标的特征（f/g）、两个repeat、两个arm，共16次计划运行。baseline缺1次输出，candidate有1次拒答；其余14次各有事实、数值、时点、方向标签。candidate每次有效输出包含unresolved/inconclusive/resolved各1条。

| 手算检查 | baseline | candidate |
|---|---:|---:|
| planned | 8 | 8 |
| missing / refused | 1 / 0 | 0 / 1 |
| 事实支持 | 4/7 | 3/7 |
| 数值错误 | 3/7 | 4/7 |
| 有时点违规的运行 | 3/7 | 4/7 |
| 方向一致 | 7/7 | 7/7 |
| E unresolved / inconclusive | 不适用 | 各7/21 |
| 已知CNY费用（非完整总成本声明） | 0.07 | 0.08 |

每个pair的baseline/candidate事实支持重复均值分别为p=1、q=0；两次重复总体标准差均0。每个arm六指标overall分子/分母等于全部pair/repeat原始行求和。缺失输出的usage保持unknown。

实际prepare和score均exit0；独立复算最终写盘plan_digest/result_digest与完整内容一致。已保留原输入和两个出口，供复核。

## 复现（从仓库根目录）

```bash
.venv/bin/python -m tradingagents.evaluation.paired_eval prepare \
  --features docs/audit_artifacts/f3_final_2026-09-09/features.jsonl \
  --trials docs/audit_artifacts/f3_final_2026-09-09/trials.json \
  --splits docs/audit_artifacts/f3_final_2026-09-09/splits.json \
  --output-dir /tmp/f3-independent-plan

.venv/bin/python -m tradingagents.evaluation.paired_eval score \
  --plan /tmp/f3-independent-plan/eval_plan.json \
  --predictions docs/audit_artifacts/f3_final_2026-09-09/predictions.jsonl \
  --labels docs/audit_artifacts/f3_final_2026-09-09/labels.jsonl \
  --as-of 2025-07-01 --output-dir /tmp/f3-independent-score
```

原始输入内容一致时，新prepare计划身份与预录预测匹配；修改特征、配置或切分后必须重新提供匹配新计划的预测，不能改写摘要强行混配。输入文件、来源声明与摘要自洽本身不证明过去真实冻结或历史信息可用性。
