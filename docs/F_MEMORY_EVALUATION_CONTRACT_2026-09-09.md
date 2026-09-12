# F 记忆与效果评测：实施契约（ZCode 起草，待 Codex 审核）

日期：2026-09-09。上游任务书：`docs/DECISION_ACCURACY_PLAN_2026-09-08.md` F 节；既有基础：`tradingagents/agents/utils/memory.py`（R4 as-of/pending 复盘）、`tradingagents/evaluation/`（N04 离线评测："契约≠预测能力"口径）、C1 证据账本、C2 假设卡、D 面板。
状态：**仅契约，未编码**。E 批次与本契约互不阻塞。

## 0. 目标与红线

**目标**：（F1）复盘记录结构化扩容——当时 thesis、预测期限、可得证据版本、观察窗口、基准、费用假设、最大不利变动、错误类型；（F2）检索侧 as-of/相关性/过期纪律固化；（F3）在 N04 之上新增独立效果评测层（时间切分、基线配对、契约指标），全部离线合成先行。

**红线**：

- 未到期限 → `pending`，不当成功/失败；**盈利≠推理正确，亏损不自动删除正确风险判断**；
- 检索只用 as-of 时刻前**已成熟**（outcome 已 resolve 或事件已发生）的记录；旧事件过期；
- 重要性/衰减规则修改必须**版本化 + 实验记录**，不在生产记忆上试调；
- 研究代码不得读取留出集标签；禁止随机打散跨越未来的切分；重叠预测窗口隔离；
- 准确度必须与评估覆盖率同报——拒答更多不能只报留下样本的高准确率；
- 首批仅离线/合成契约演示；**不能用合成随机收益展示"提高准确率"**；真实数据快照与模型实验另列已授权范围核对。

## F1：复盘记录扩容（`memory.py`）

现有 pending 条目（`[date | ticker | rating | pending]` + reflection）扩为结构化 `ReviewRecord`（JSON 行，向后兼容旧格式读取）：

```json
{
  "schema_version": 1,
  "ticker": "...", "trade_date": "...", "analysis_run_id": "...",   // ← N02 run_id
  "thesis_card_digest": "...",                                        // ← C2 当时假设卡摘要（不整卡入记忆）
  "prediction_horizon": "...",                                        // ← C2 卡快照
  "evidence_version": {"manifest_digest": "...", "evidence_event_count": n},  // ← C1/D 可得证据版本
  "outcome": {
    "status": "pending | resolved | expired_unresolvable",
    "window": {"start": "...", "end": "...", "benchmark": "000300.SH"},
    "raw_return": ..., "alpha_return": ..., "max_adverse_excursion": ...,   // ← _fetch_returns 扩展 MAE
    "fees_assumption": {"bps": ..., "declared_in": "config_snapshot"},
    "benchmark_executable": "A 股开盘可成交口径声明（预先固定）"
  },
  "error_type": "data_missing | time_boundary | calculation | reasoning | execution | market_unforeseen | none",
  "reflection": "...",                                                // 既有反思文本保留
  "correct_risk_flags": ["..."]                                       // 亏损但正确的风险判断（不因亏损删除）
}
```

**确定性规则**：

- `error_type` 分层判定优先级固定（数据缺失→时点→计算→执行→推理→市场不可预见），代码可判的先判（时点违规来自 C2 gaps、计算错误来自 D 面板对照、执行来自 T+1/涨跌停约束），余项才由反思文本辅助归类并标 `assisted_by_reflection`；
- MAE（最大不利变动）在 `_fetch_returns` 现有窗口内顺带计算（同数据源，不加调用）；基准/费用假设来自**运行配置快照**（不是回填）；
- 退市/停牌/长窗口未成熟 → `pending` 保持或 `expired_unresolvable`（预先声明规则：窗口 +30 个交易日仍未成熟），不静默缩短（R4 语义保持）；
- **写入时机不变**：propagate 结束写 pending（零 LLM），outcome resolve 照旧批量；新增字段缺失（旧运行）→ null = 未记录，不伪造。

## F2：检索纪律（`get_past_context` 扩展）

- as-of 过滤既有（R4）不动；新增**成熟度**过滤：仅 outcome 非 pending 或事件成熟日 ≤ as_of 的条目参与；
- 相关性排序键固定并版本化（`ranking_version`）：同标的 > 同行业 > 市场状态相似 > 事件有效期未过；旧事件（有效期字段过期）不进上下文；
- 排序/衰减参数集中于带版本的 `RetrievalPolicy` 常量对象；任何修改 → 新版本号 + 实验记录文件（离线对照新旧检索输出），生产记忆文件永不回写试调；
- 记忆文件**只追加**语义保持（除既有 outcome 批量更新外无删除路径；`correct_risk_flags` 保证亏损条目不被清理）。

## F3：效果评测层（`evaluation/` 之上新增，不改 N04 口径）

**输入**：固定 fixture 集——股票×若干（牛/熊/震荡三类行情段）、指数、行业代表、缺失数据案例（vendor 失败/部分失败/证据空）；每案例带日期与预声明的可评价标签（方向、期限、基准、事件二元题）。**禁止随机收益合成冒充效果**：合成集只测契约指标与流水线正确性，报告必须标注"合成契约演示，非效果证明"。

**切分与配对**：

- 按日期切 train/validation/holdout，**禁止跨未来随机打散**；预测窗口重叠的样本归同一 fold（隔离泄漏）；
- 每次评估记录：数据快照 digest、公开配置、预算、模型版本、试验次数；候选与基线**配对同轮**（同快照同配置，只差被测开关——与 E 的 A/B 纪律一致）。

**指标（全部与覆盖率同报）**：事实支持率（独立标注）、时点违规率（C1/C2 缺口计数）、数值错误率（对 D 代码表）、冲突未解决率（E unresolved）、完成率（未拒答比例）、延迟与成本（LLM 调用/token/工具轮）。方向/收益评价：明确期限与基准，A 股可成交时间、T+1、涨跌停/停牌、复权/分红、费用、退市按**预先声明规则表**处理（无事后选择）。事件概率：仅对事先定义的二元事件算 Brier/校准曲线 + 置信区间；Buy/Hold 文案不算胜率。

**输出**：`evaluate_suite` 旁新增 `evaluate_decision_quality(fixtures, policy)`，报告渲染沿用 N04 的"契约≠预测能力"免责框架。

## 实施顺序（获批后）

1. `ReviewRecord` 扩容 + MAE + error_type 分层（F1）+ 旧格式兼容测试。
2. `RetrievalPolicy` 版本化 + 成熟度/过期过滤（F2）+ 新旧检索对照实验脚本（离线）。
3. `evaluate_decision_quality` + fixture 集 + 时间切分/配对（F3）。
4. `tests/test_memory_review_record.py` / `tests/test_decision_quality_eval.py`（全部离线）。
5. 分批 handoff（hash/测试/限制）→ Codex 独立审计。

## 待 Codex 决策点

1. `expired_unresolvable` 的宽限期（建议 30 个交易日）与退市样本处理规则；
2. `correct_risk_flags` 由谁标注（反思 LLM 提议 + 代码保留，或纯人工）；
3. F3 首批评测的 fixture 规模（建议每行情段 ≥3 案例）与是否纳入指数路径；
4. `RetrievalPolicy` v1 的排序键权重是否需要先做离线对照再定稿。
