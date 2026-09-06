# 离线报告评测使用说明

N04 对已保存的研究报告 JSON 进行确定性检查，并比较两个版本的结果。适合在修改模型、提示词或流程之后核对报告完整性、失败和遗漏、人工抽查的事实以及导入费用。本阶段提供 CLI，不执行真实模型，也不采集运行费用。

## 先运行合成示例

在项目根目录执行，使用现有 `.venv`。每个输出目录都必须不存在；评测不会覆盖既有目录。

```bash
EVAL_WORK=$(mktemp -d "${TMPDIR:-/tmp}/ta-eval.XXXXXX")

.venv/bin/python -m tradingagents.evaluation run \
  --suite examples/evaluation/suite.json \
  --submission examples/evaluation/baseline.json \
  --output-dir "$EVAL_WORK/base"

.venv/bin/python -m tradingagents.evaluation run \
  --suite examples/evaluation/suite.json \
  --submission examples/evaluation/candidate.json \
  --output-dir "$EVAL_WORK/candidate"

.venv/bin/python -m tradingagents.evaluation compare \
  --baseline "$EVAL_WORK/base/evaluation.json" \
  --candidate "$EVAL_WORK/candidate/evaluation.json" \
  --output-dir "$EVAL_WORK/comparison" --fail-on-regression
echo "退出码=$?"  # 该合成示例预期为 1，结果文件仍会写出
echo "$EVAL_WORK"
```

合成示例共 6 类案例、每类 2 次尝试。候选整体有 2 项改善、1 项退化；`index_normal / trial 2` 从完成变为失败，丢失 7 个原本通过的检查，所以即使整体通过率提高，退化门控仍返回 1。相同结果自比较返回 0。

基线有 USD 和 CNY 两类合成费用，候选对应指数案例使用 USD。不同币种各自汇总，该案例不生成配对费用比较。候选未提供人工事实标注，因此不能与基线声称事实支持率提升。

详细示例文件见 [examples/evaluation/README.md](../examples/evaluation/README.md)。全部是合成数据，不能用于判断真实模型优劣。

## 评估自己的报告

1. 保留每次研究产生的独立报告 JSON。已有运行档案和历史版本的查看方式见 [历史报告对比指南](REPORT_COMPARISON_GUIDE.md)。每次真实尝试保留自己的运行编号，不把同一份输出复制成多次独立实验。
2. 复制示例案例集，先冻结标的、日期、时区明确的 `as_of`、实际分析师团队、预期质量状态、允许评级及证据。指数使用合法分析师团队；证据的 `available_at` 是其可获得时间。
3. 把报告副本放在提交清单所在目录或其子目录。清单的 `report_path` 使用相对 JSON 路径，不能含 `..`、绝对路径或指向目录外的符号链接。
4. 复制提交清单，逐次填写 `case_id`、`trial_id` 和 `status`。失败填写 `failed`；未提供的尝试自动计为 `missing`，两者都会保留在总分母中。不可读取、坏 JSON 或不可评分的单份报告计为 `invalid`。
5. 用下方命令计算案例集的规范 JSON 摘要，填入清单 `suite_digest`。不要使用原始文件字节的普通 SHA-256 替代规范 JSON 摘要。修改案例集必须重新冻结并更新摘要。
6. 可选人工标注填写审核者、审核时间、报告摘要、准确引用的字段与原文、`supported` / `contradicted` / `unverifiable` 以及冻结证据 ID。报告正文变化后须重新审核和更新 `report_digest`；评级建议属于判断，不能仅引用评级文本就认定其为受支持事实。
7. 可选 `observations` 填入已经测得的延迟、输入/输出 token、费用和币种。费用数额和三位大写币种必须成对；缺失是未记录，实际 0 保留为 0。
8. 分别执行 `run`，再执行 `compare`。两份结果须使用相同案例摘要、评分器版本和尝试集合。

```bash
.venv/bin/python -m tradingagents.evaluation digest path/to/suite.json
.venv/bin/python -m tradingagents.evaluation digest path/to/report.json
```

字段合同和完整设计见 [N04 任务书](N04_OFFLINE_EVALUATION_PLAN_2026-09-06.md)。

## 输出和退出码

| 命令 | 输出 | 退出码 |
|---|---|---|
| `run` | `evaluation.json`、`evaluation.md` | 0 表示评分完成，即使有未通过案例 |
| `compare` | `comparison.json`、`comparison.md` | 通常为 0；带门控且存在退化为 1 |
| 非法输入或写出错误 | 错误说明 | 2；已有输出目录不会覆盖 |

契约通过率衡量八项确定性规则。事实支持率只覆盖人工标注声明，时点违规率只覆盖显式引用的证据；零分母显示无法计算，JSON 使用 `null`。配对事实统计只比较双方共同覆盖的指标，费用只比较同币种且双方都记录的值。`--fail-on-regression` 针对工程检查退化，不附加事实率或费用阈值。

最终独立测试和验证边界见 [验收记录](N04_ACCEPTANCE_2026-09-06.md)。
