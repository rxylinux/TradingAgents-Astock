> Codex最终状态（2026-09-09）：F3已按已验证范围验收；最终全量1593 passed、14可选依赖skipped。具体限制以[F3审核记录](F3_REVIEW_2026-09-09.md)为准。真实预测效果未验证。

# 决策质量功能用户指南（A/B1/C1/C2/D/E/F1/F2 交付 + F3 已按范围验收）

日期：2026-09-09。适用版本：本轮连续优化批次。本指南只描述**已交付**功能与真实用法；F3 已完成限定范围的独立验收，具体边界见上方审核记录。

## 总览：启用方式与验收状态

功能的启用方式分三类：**自动**（C1/C2，随分析流程工作，无需配置）、**可选关闭**（D/E/F2，默认不注入）、**独立离线 CLI**（F1/F3，不进运行）。对于**可选关闭**类功能，不提供参数时对应功能的注入路径保持关闭（已有真实 prepare→工厂对比测试锁定该路径零差异）；本指南**不声称**整个分析系统与本队列变更前逐字节一致。逐项说明如下。

| 功能 | 启用方式 | 状态 |
|---|---|---|
| C1 证据账本 | 自动（无需配置） | 已验收 |
| C2 研究假设卡 | 自动（无需配置） | 已验收 |
| D1/D2 财务面板 | 可选关闭（`--financial-panel-manifest` / Web 高级） | 已验收 |
| E 独立初判与分歧核查 | 可选关闭（`evidence_debate_enabled` 配置） | 已验收 |
| F1 历史复盘记录检索 | 独立离线 CLI | 已验收 |
| F2 历史经验投影 | 可选关闭（`--review-records` / Web 高级） | 已验收 |
| F3 离线配对评估 | 独立离线 CLI（两阶段） | 已按范围验收 |

## C1 证据账本（自动启用，无需配置）

每次分析中新闻类工具（`get_news`/`get_global_news`，含 policy/social 等分支复用）自动产出结构化证据：来源抓取状态、按分析时点过滤后的记录、排除计数。可信截止时点（运行的 trade_date）约束工具请求——模型请求 2027 的数据会被代码收窄到运行日，正文与证据索引都不会出现未来数据。

- 查看位置：报告 JSON 的 `evidence_bundle` 字段；Web「🗂️ 数据来源与证据」expander；Markdown/PDF「数据来源与证据」section（逐条 evidence_id → 标题/来源/发布时间/链接，可追溯）。
- 边界：证据 ID 与时点合法**不代表**论断被事实支持；旧 vendor（无结构化输出）标记 provenance=unknown。

## C2 研究假设卡（自动启用）

组合经理的既有结构化调用同时产出研究假设卡：方向/期限/主要论点/支持与反驳证据引用/失效条件/评估状态。引用经 C1 校验（悬空/未来/冲突被标记）；评估状态（assessable/limited/insufficient/unknown）只由**可观察缺口**推导——模型自述信心恒标 uncalibrated，不显示任何投资胜率。

- 查看位置：报告 JSON 的 `thesis_card`；Web「🧪 研究假设卡」；MD/PDF「研究假设卡」。失效条件含待人工判断项时**不会自动触发**——仅生成与展示。

## D1/D2 财务面板（可选，默认关闭）

离线 manifest 经确定性纯函数计算同比增长/利润率/现金流比/限定口径净现金/PE 情景估值，注入运行并进入 RM/Trader/PM 提示词与报告。

CLI 入口是 `.venv/bin/tradingagents`（激活 venv 后直接 `tradingagents`）——注意 `python -m tradingagents` **不是**可用入口。

```bash
# 独立离线计算（不进运行）：
.venv/bin/python -m tradingagents.dataflows.financial_panel \
  --manifest tests/fixtures/financial_panel/stock_normal.json \
  --instrument 600519 --instrument-type stock \
  --analysis-date 2024-11-05 --output-dir out/   # JSON+Markdown；坏输入 exit 2

# 注入运行（可选关闭路径启用）：
.venv/bin/tradingagents \
  --financial-panel-manifest tests/fixtures/financial_panel/stock_normal.json
```

- Web：侧栏「高级：财务面板」→ 本机路径或上传 JSON。
- 关键语义：情景倍数**必须显式给定**（无默认倍数）；净现金是限定口径（四项有息债务减现金，缺任一=unknown 不补零）；披露晚于分析时点的输入被排除；恢复断点不重读 manifest。
- 查看位置：报告 JSON `financial_panel`；Web「🧮 财务面板（可复算）」；MD/PDF section。

## E 独立初判与有界反证补查（可选，默认关闭）

配置 `config["evidence_debate_enabled"] = True`（CLI/Web 暂无开关入口）后，辩论前插入：多空**互盲**结构化初判 → 分歧规划 → 最多 2 个问题各 1 次真实新闻工具补查（受可信截止与标的绑定约束）→ 原辩论机制照常。最多新增 3 次逻辑模型调用 + 2 次工具调用，用量随报告保存。

- 边界：无自动裁决（`resolved_side` 不存在）；跨来源转载按内容摘要标记不投票；补查工具仅限当前已注册的新闻工具。

## F1 历史复盘记录检索（独立离线 CLI）

```bash
# 基本检索
.venv/bin/python -m tradingagents.evaluation.review_record \
  --file tests/fixtures/review_records/records.jsonl \
  --as-of 2025-06-01 --ticker 600519 \
  --instrument-type stock --limit 10 \
  --output-dir out/     # JSON+Markdown；拒绝 exit 2

# 带显式查询窗口的重叠排除
.venv/bin/python -m tradingagents.evaluation.review_record \
  --file tests/fixtures/review_records/overlap.jsonl \
  --as-of 2026-01-01 --ticker 600519 \
  --instrument-type stock --limit 10 \
  --query-window-start 2024-11-05 --query-window-end 2025-05-05 \
  --output-dir out_overlap/
```

- 严格 AND 五门（决策/成熟/观测/发布/版本可用全部 ≤ as-of 才可见）；未知保持未知（null 不变 0，无默认费率）；到期必须显式（"3-6 months" 不映射日期）；重叠排除仅显式 query-window（闭区间）；生产记忆零改动。
- 记录 schema 与校验规则见 `docs/F1_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`。

## F2 历史经验投影（可选，默认关闭）

把 F1 格式记录的**同标的同类型**历史经验以只读投影注入决策提示词（独立通道，不改 past_context）。

```bash
.venv/bin/tradingagents \
  --review-records tests/fixtures/review_records/records.jsonl
```

Web 侧栏「高级：历史经验投影（只读，可选）」→ 路径或上传。

- 先全量校验 → 同 ticker+instrument_type 过滤（limit 之前）→ F1 AND 检索 → ≤5 条；零合格记录输出非空限制块（"无合格≠历史无事件"）；异标的内容不进提示词。
- 恢复断点只读已保存投影（删除输入文件零影响）；模式双向切换拒绝；篡改投影/锚点拒绝且断点保留。
- 查看位置：报告 JSON `review_projection`；Web「📚 历史经验投影」；MD/PDF section（三态：未记录/零合格/有记录）。

## F3 离线配对评估（两阶段 CLI；**已按范围验收，仅作离线评估**）

```bash
# 阶段 1 prepare：特征+试验配置+切分 → 不可变计划（不触标签/模型）
.venv/bin/python -m tradingagents.evaluation.paired_eval prepare \
  --features tests/fixtures/paired_eval/features.jsonl \
  --trials tests/fixtures/paired_eval/trials.json \
  --splits tests/fixtures/paired_eval/splits.json \
  --output-dir plan/

# 阶段 1 带 embargo（可选，默认 0）
.venv/bin/python -m tradingagents.evaluation.paired_eval prepare \
  --features tests/fixtures/paired_eval/features.jsonl \
  --trials tests/fixtures/paired_eval/trials.json \
  --splits tests/fixtures/paired_eval/splits.json \
  --embargo-days 30 --output-dir plan_e30/

# 阶段 2 score：冻结计划 + 两臂预录预测 + 独立标签 → JSON+Markdown
.venv/bin/python -m tradingagents.evaluation.paired_eval score \
  --plan tests/fixtures/paired_eval/plan.json \
  --predictions tests/fixtures/paired_eval/predictions.jsonl \
  --labels tests/fixtures/paired_eval/labels.jsonl \
  --as-of 2025-07-01 --output-dir report/
```

- **计划身份匹配**：score 只接受同一 plan_digest 的预测与该计划内的四元身份（pair/feature/arm/repeat）；冲突/计划外拒绝；缺失输出计 missing、分母不缩水。
- 两臂配置差异**仅 `evidence_debate_enabled` 布尔**（类型敏感比较：True≠1、缺键≠null）。

## 诚实边界（必读）

1. **合成契约指标 ≠ 真实预测结果**：F3 全部指标来自合成 fixture 与预录输出——只证明工程契约（身份/purge/分母/成本口径正确），**不代表预测效果提高**。所有报告固定附此声明。
2. **Brier/校准 = not_applicable（未实现）**：与"分母为 0 的 null 指标"（如某臂数值错误率 0/0）语义不同——前者是功能未实现，后者是无合格样本。报告均已区分标注。
3. **证据/面板/投影的可信边界**：C1 引用合法≠论断被支持；D 面板 availability=declared_only 不升级为已验证；F2 投影盈利≠推理正确。文案均已内嵌。
4. **验收状态**：A/B1/C1/C2/D（含 D1/D2）/E/F1/F2 已由 Codex 独立验收（全量回归通过）；**F3（F3a 计划 + F3b 评分）已完成限定范围的独立验收**；合成示例不代表实际模型预测效果。
5. 可选关闭类功能（D/E/F2）**默认不注入**；不提供参数时对应注入路径保持关闭（真实 prepare→工厂对比测试锁定该路径零差异）。本指南不声称整个分析系统与本队列变更前逐字节一致——C1/C2 是自动启用的，各批次也有各自的既有测试。
6. **F3 保守行为（fail-closed）**：未知/缺失 `feature_available_at` 的特征在 prepare 阶段**整批拒绝**（不排除个别行后继续）——这是当前实现的保守选择，不是"逐行排除并保留 ID"的行为；指南不以未实现的排除语义描述。
7. **Brier/校准 = not_applicable（未实现）**：F3 报告将 Brier 标为 not_applicable 并注明"不支持"，与"分母为 0 的 null 指标"语义不同。合成样例的臂间差异**不构成任何效果证据**（固定免责文案）。
8. **Schema 合法 ≠ 已验证真实历史数据**：F1/F3 接受的是 schema 合法的**调用方提供记录**——通过校验只证明格式/摘要/时点一致，不证明记录内容是真实历史数据；availability 字段保留 `declared_only/verified_snapshot` 区分，声明不升级为已验证。
