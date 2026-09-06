# N04 离线评测示例（全部为合成数据，非模型实测）

本目录包含可直接运行的评测示例：冻结案例集、两份提交清单、合成报告文件。

## 快速运行

```bash
cd 项目根目录

# 1. 运行基线评测
.venv/bin/python -m tradingagents.evaluation run \
  --suite examples/evaluation/suite.json \
  --submission examples/evaluation/baseline.json \
  --output-dir /tmp/eval/base

# 2. 运行候选评测
.venv/bin/python -m tradingagents.evaluation run \
  --suite examples/evaluation/suite.json \
  --submission examples/evaluation/candidate.json \
  --output-dir /tmp/eval/cand

# 3. 对比（有退化时 exit 1）
.venv/bin/python -m tradingagents.evaluation compare \
  --baseline /tmp/eval/base/evaluation.json \
  --candidate /tmp/eval/cand/evaluation.json \
  --output-dir /tmp/eval/diff \
  --fail-on-regression
echo "exit=$?"  # 本示例候选有退化，预期 1

# 计算文件 digest（更新 suite 后需重算）
.venv/bin/python -m tradingagents.evaluation digest examples/evaluation/suite.json
```

## 文件说明

| 文件 | 说明 |
|---|---|
| `suite.json` | 冻结案例集（6 案例 × 2 trial = 12 预期） |
| `baseline.json` | 基线提交清单（含遗漏/失败/标注/未来证据/混币） |
| `candidate.json` | 候选提交（补齐遗漏但引入新退化） |
| `reports/*.json` | 合成报告文件 |

## 案例覆盖

1. **stock_complete**：正常个股报告 + 具体合成事实标注（营收/毛利率）
2. **stock_limited**：资料受限 + 未来证据时点违规演示
3. **stock_insufficient**：严格多数不足（空报告）
4. **index_normal**：指数五位分析师（候选退化演示）
5. **stock_legacy**：旧格式报告（缺质量卡/运行档案 → unknown）
6. **stock_negative**：负例（无评级）

## 自定义使用

1. 冻结自己的案例：复制 `suite.json`，修改案例/证据/预期值
2. 计算 digest：`... digest your_suite.json`（修改后必须重算）
3. 编制提交清单：复制 `baseline.json`，填入报告路径和标注
4. 更新 report_digest：对每份报告运行 digest 并填入 review

## 关键指标说明

- **契约通过率**：报告完整性检查通过的比例，**不是事实准确率或投资胜率**
- **事实支持率**：人工标注声明中受时点内证据支持的比例，仅覆盖标注部分
- **时点违规率**：引用了未来证据的声明占引用证据声明的比例
- **引用了证据的声明数**：包含 contradicted 和未来引用，不等于"得到支持"
- **成本**：按币种分组汇总，不跨币相加；为导入测量值非评分器实测
- **混币演示**：基线 index_normal trial 1 为合成 CNY 成本、候选同 trial 为合成 USD——
  不同币种不产生配对成本比较（`cost_comparison` 字段缺席是正确行为）

## 注意

- 所有数据为**合成演示**，非模型实测，非真实行情
- 评级/建议属于判断，不作为 supported 事实标注
- 事实支持率仅覆盖人工标注的声明，不代表整篇报告事实准确率
