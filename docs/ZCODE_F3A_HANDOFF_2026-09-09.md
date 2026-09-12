# F3a 交接：离线配对评估计划阶段（ZCode → Codex）

日期：2026-09-09。权威契约：`docs/F3_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`（F2 已验收后开工）。纯离线：prepare 不接受标签路径、不打开标签文件、不读 F1 完整记录、不触模型/网络、不改生产与记忆。

## 交付物

**`tradingagents/evaluation/paired_eval.py`（阶段 A：prepare）**：

- **特征 schema（规则 #2）**：白名单严格含嵌套（完整 F1 记录失败；outcome/annotation 关键名即拒）；`feature_available_at` 为特征版本可知时间——缺失拒绝（不得用 decided_at 替代/倒推），与 decided_at 都不得晚于 prediction_at；F1 严格时间原语（naive/垃圾拒绝）；NaN/Infinity/非法 Unicode 递归拒绝；`build_plan` 入口对特征**再次逐条复验**（不信任调用方）。
- **配对身份（规则 #3）**：两臂**原样 normalized_config** 入计划并复算 config_digest；差集必须恰为 `{evidence_debate_enabled}` 且 bool false→true（预算/模型/种子差异、int 1、pop 缺失、baseline True 全拒）；repeat_count 正整数非 bool 且 ≤100；全部预定重复入计划——`planned_arm_runs` 固定分母；重复 (pair, feature, repeat) 身份拒绝；同 feature 跨 pair 合法且身份独立。
- **切分与 purge（规则 #4）**：显式互斥有序 train/validation/holdout（按 prediction_at 分配，边界外记 `excluded_unassigned` 不缩分母语义——它们本就不入计划）；**同 instrument+type 分组**，对**全部跨切分组合**闭区间 target_window 相交（含 train×holdout 非相邻、边界相等）→ 从较早切分剔除并记录**冲突两侧 ID**（`purged_overlaps`）；判定基于原始全集、**反序输入计划逐字节一致**；跨组（跨标的）不 purge；embargo 默认 0、非负整数日历日（正值=较后窗口 start 前推 N 个上海日历日）；切分/embargo/窗口全部参与 plan_digest。
- **CLI**：`python -m tradingagents.evaluation.paired_eval prepare --features --trials --splits [--embargo-days] --output-dir` → `eval_plan.json`（含 plan_digest/input_digests/planned_arm_runs/purge/unassigned 全量）；坏输入 exit 2 + stderr 无 Traceback、无成功产物；文件上限 2MB/1000 特征/100 重复/20000 arm-runs 展开前检查。

**Fixtures**（`tests/fixtures/paired_eval/`）：8 特征覆盖相邻重叠、非相邻边界相等、跨标的不 purge、未分配、两重复配对。

## 实际 CLI 示例

```bash
.venv/bin/python -m tradingagents.evaluation.paired_eval \
  --features tests/fixtures/paired_eval/features.jsonl \
  --trials tests/fixtures/paired_eval/trials.json \
  --splits tests/fixtures/paired_eval/splits.json \
  --embargo-days 0 --output-dir /tmp/f3a
# plan_entries=10 planned_arm_runs=20 purged=2 unassigned=1 -> eval_plan.json  (exit 0)

坏特征（含 outcome 字段/完整 F1 记录/未来版本时间/非 bool 重复数/额外配置差异）→ exit 2
```

## 验证（定向，未跑全量）

```bash
.venv/bin/python -m pytest tests/test_paired_eval.py    # 24 passed
.venv/bin/python -m pytest tests/test_paired_eval.py tests/test_evaluation.py tests/test_review_projection_integration.py tests/test_review_record.py
# 171 passed（触达回归）
.venv/bin/python -m ruff check …                        # All checks passed!
```

覆盖：完整 F1 记录/额外键/outcome 标记拒绝；版本时间独立（缺失/晚于 pred/decided 晚于 pred；当日 avail 合法控制）；唯一 bool 差异四类违反拒绝；重复计数与全重复入分母；fixture 计划的相邻+边界相等 purge 与跨标的不 purge；非相邻 train×holdout purge；embargo 30 日历日触发；反序输入逐字节一致；真实 CLI 正常/坏输入 exit2；NaN 边界拒绝；prepare 参数面无标签路径。

## 限制（如实）

- 本阶段只交付 prepare；score（规则 #5-#8）为 F3b 下一批。
- 合成 fixture 只证明工程契约——不代表预测效果提高（报告免责由 F3b 落实）。
- Brier/校准未实现——按契约 #7 明确"不支持"，不声称 F3 完整包含校准。

## 文件清单（SHA-256）

```
93478dd376cd5dbba87e8d2bc909766cfb65f94accc6eae2a3650124bfb7a404  tradingagents/evaluation/paired_eval.py
f66c4b7be3606b7525dd1be29e7a1746e3fb41a8cebd6d2220826449c01f5303  tests/test_paired_eval.py
3b774808ba432b2aa13b63da69a37bebc93f454a704c0ce7ea671897d01fd55c  tests/fixtures/paired_eval/features.jsonl
49cf61a17b0a607d6eb296a89b1fa8e2bcab4a0cf8d913869ea6f64a066fdee8  tests/fixtures/paired_eval/trials.json
203eb8626e16f4df578828a1248b628b060697c46d0e0f1d441dea14071b18b2  tests/fixtures/paired_eval/splits.json
```

---

## F3a R1 修正 + F3b 交付附录（2026-09-09 05:1x）

### F3a R1（docs/F3_REVIEW_2026-09-09.md）：10 失败 → **审计 11/11**

1. **严格嵌套 schema**：`binary_event` 键集恰为 `{event, resolve_by}`（`future_answer` 等任意额外键拒绝）；`decision` 键集同样精确——字段边界不靠 outcome 关键字枚举。
2. **类型敏感配置比较**：`_typed_config_diff` 以 (类型标签, 规范 JSON) 三元组比较——缺失 ≠ null、bool True ≠ int 1；声明的 `config_digest` **必须复算比对**（未提供才生成）；坏 arms 对象受控拒绝；pair_id 非空字符串。
3. **绝对时间切分**：全部比较用解析后 datetime（杜绝字符串字典序）；train/validation/holdout 必需且按绝对时间递增；**闭区间端点相等拒绝**；时区反向欺骗拒绝；时间原语**直接 import F1**（`review_record._parse_datetime_strict`）而非复制。
4. **全输入身份与稳定性**：feature_id 唯一性在**任何快照展开之前**全量检查（同 ID 异 ticker 即使都不入切分也拒）；excluded/purged/entries 全部稳定排序——反序输入 plan_digest 逐字节一致；排除对象保留 ticker/type/原因可复核。
5. **预算预检**：合格特征 × Σ repeats × 2 在**展开前**计算与拒绝；MAX_FEATURES 在纯 API 也强制。
6. **CLI 最终摘要**：`input_digests` **在哈希前**传入 `build_plan`——CLI 产物对自身完整文件摘要自验通过；score 复验完整文件摘要（含输入摘要字段）。

**CLI 兼容**：原 flag 调用（无子命令）保持；新增 `prepare`/`score` 子命令双阶段。

### F3b（score）交付

- **冻结计划验证先行**：score 入口复验完整文件摘要 + 试验配置（唯一 bool 差异再执行）——坏/篡改计划在指标统计前拒绝。
- **精确身份**：预测按 (pair, feature, arm, repeat) 四元绑定 + plan_digest/feature_snapshot_digest/config_digest（臂匹配）三元摘要；冲突重复拒绝、计划外拒绝、generated_at ≥ prediction_at（date 精度按当日开始比较）。
- **固定计划分母**：planned/missing/failed/refused/limited/completed——缺失输出计 missing 不缩分母；completed=结构化结论产出 ≠正确≠非拒答。
- **指标**：每指标 {numerator, denominator, value(0→null)}——fact_support（主张级）、numeric_error（引用级）、temporal_violation（有违规运行数/已审核）、E unresolved 与 inconclusive 分计（分母=已列出分歧；未启用→null）、direction_hit（Buy/Hold 不映射涨跌）；Brier **明确 not_applicable**（未实现不冒充）。
- **标签**：独立 schema + 声明摘要复算；四元归属 + claim_id/claim_digest 精确匹配；错臂/错摘要/失败输出标签隔离 `invalid_label`；outcome 类标签三时点门（未来→pending_maturity；缺失→unverifiable_*）；fact_support 不套到期门。
- **重复汇总**：全量单次保留；均值 + **总体标准差（÷N）**；有效N/计划N 同报；无置信区间。
- **成本**：**Decimal 按币种分桶**（0.012×19=0.228 精确）；usage 四类不混算（unknown 计数保全，不以 0 冒充实耗）。
- **确定性**：同输入两次评分逐字节一致；真实 CLI 双出口（正常 JSON+MD；坏计划 exit 2 无 Traceback 无产物）。

### 实际 CLI 示例（本次实跑）

```bash
.venv/bin/python -m tradingagents.evaluation.paired_eval prepare \
  --features tests/fixtures/paired_eval/features.jsonl \
  --trials tests/fixtures/paired_eval/trials.json \
  --splits tests/fixtures/paired_eval/splits.json --output-dir plan/
# plan_entries=10 planned_arm_runs=20 purged=2 unassigned=1  (exit 0)

.venv/bin/python -m tradingagents.evaluation.paired_eval score \
  --plan tests/fixtures/paired_eval/plan.json \
  --predictions tests/fixtures/paired_eval/predictions.jsonl \
  --labels tests/fixtures/paired_eval/labels.jsonl \
  --as-of 2025-07-01 --output-dir report/
# baseline: missing=1 refused=0 completed=9；candidate: refused=1 completed=9；
# invalid_labels: pending_maturity×1, claim_digest_mismatch×1；
# usage: llm=57 http=unknown×19；CNY: 0.228 (Decimal)  (exit 0)
# 篡改计划 → exit 2 无产物
```

### 验证（定向，未跑全量）

`tests/test_paired_eval.py` **35 passed**（F3a 24 + F3b 11：固定分母/篡改计划/冲突与计划外预测/错臂标签/待期标签/unknown 与币种/÷N 标准差/逐字节一致/真实双出口）；Codex F3 审计 **11/11**；触达回归 **182 passed**；ruff 全绿。

### 修正后文件清单（SHA-256，以此为准）

```
00180425bbcd440888dd7ead85c73e3b68547d40151db55e2cf7353a8ea6a45f  tradingagents/evaluation/paired_eval.py
370147aaee7674e7471555c95901d86a287e6be914b28ba226b78b7e7f2e90c5  tests/test_paired_eval.py
794316113d48b53c66c21d9e7a07cecd5c66b86aa68e5efd2b8f493f69feb956  tests/fixtures/paired_eval/plan.json
078c94630fd6b8f5cedfc26e6d5e7cd8f4e1b87386178baa2ff519860d2c9ff7  tests/fixtures/paired_eval/predictions.jsonl
1ca0f550251dc4b2b7f91356fe284707dec13428c0c03b191b54ff36e804b31b  tests/fixtures/paired_eval/labels.jsonl
```

---

## F3b R1 修正附录（2026-09-09 06:0x，对应 docs/F3_REVIEW_2026-09-09.md F3b R1）

Codex 独立评分审计 14 失败 → 修复后 **15/15**（脚本未改动）。八组修复按"先冻结输入与对象归属，再修指标/成本/覆盖率"：

1. **两臂摘要串用**：config_digest 必须精确匹配**其声明臂**的摘要（baseline 填 candidate 摘要即拒）。
2. **标签精确绑定被评分对象**：fact_support 必须绑定真实 claim（id+digest，缺失即分母 0 不计分）；numeric_verification 必须绑定**真实 numeric_reference**（指向普通 claim 不算）；**同 (预测, kind, target) 冲突值拒绝**（supported/unsupported 不是两条观测）、完全重复拒绝（不能刷分母）；值枚举非法拒绝（不静默当负例）。
3. **指标分母改为对象集统计**：temporal_audit 以**运行**为对象（同运行多主张标签只计 1/1）；direction=None 有标签计 unassessable、分母 0；每指标新增 `eligible/unlabeled/unassessable` 覆盖率字段（JSON 与 MD 均展示）。
4. **缺失用量入 unknown**：usage unknown 计数**包含 missing 输出**（计划 2 臂运行只有 1 个无 usage 预测 → 四类 unknown 各=2）；**每臂分开报告** totals/unknown/planned/missing（`arm_usage`），总计仅为便利。
5. **计划逐项复验**：score 对 plan 每条 entry 复验特征 schema（嵌套白名单/时间原语）+ feature/config 摘要复算 + 唯一身份 + repeat 越界 + planned 总数一致性——自洽摘要不再豁免 schema；**公开 API 重验预测/标签**（status=bogus 等坏输入一律 ScoreValidationError，无裸 KeyError）。
6. **Decimal 非有限拒绝**：NaN/Infinity 字符串在 `is_finite` + 非负检查下拒绝。
7. **结果身份覆盖实际输入**：`input_content_digests`（predictions/labels 全量内容摘要）进入结果——只改 labeled_by 也改变 result_digest；同指标值不同来源不再同身份。
8. **统一时点约定**：generated_at 不得早于 prediction_at/**feature_available_at**/decided_at 三者的 F1 上海日终锚点（date 精度不再被重解释为 00:00——同日 08:00 生成 vs 20:00 才可用的特征被拒）；fixture 修正为次日生成。

**成本**：每臂 Decimal 币种分桶（baseline 0.108 / candidate 0.120 分开），部分已知保留 note 不冒充总成本。

验证（定向，未跑全量）：`tests/test_paired_eval.py` **35 passed**（错臂标签测试改为重复/冲突拒绝语义）；Codex **两份 F3 审计 26/26**（score 15/15 + plan 11/11）；触达回归 **182 passed**；ruff 全绿；真实 score CLI 复测通过。

### F3b R1 后文件清单（SHA-256，以此为准）

```
0399d7e196fb9f2d8c75dff04d64878cdddc8b8747d8e0ebcf236f8f6ff65eed  tradingagents/evaluation/paired_eval.py
7d5783151bf53b6ffc9c7fdeb71917d3717ad08a0b6ebd16dff68eaeb65005a8  tests/test_paired_eval.py
16c9ca8063bf92053736ee415e5fba67f1c37359d635593ffd953ca66b805a0d  tests/fixtures/paired_eval/predictions.jsonl
```

---

## F3b R1 剩余同类项 + R2 六项修正附录（2026-09-09 06:4x）

Codex 评分审计扩展至 21+ 项（脚本未改动）→ 当前 **22/22 通过**。本轮完成的 R1 同类项与 R2 六组：

### R1 同类项（上轮交接承诺的未竟项，全部完成）

- **重复汇总六指标全实现**：fact_support/numeric_error/temporal_violation/e_unresolved/e_inconclusive/direction_hit 全部按"**先在 repeat 内聚合 features** → 每 repeat 一行原始 num/den/value（含零效行）→ population 统计"—— = 该 pair 的**计划重复数**（不是 feature 数）； 逐次保留可复算；零有效重复输出"0/N——全部分母为 0，值 null 不造 0"。
- **completed 状态语义验证**：status=completed 但无非空 claims、无 direction、无 disagreements →  计数并按 limited 处理（指标不把它当 completed；原始状态在预测里保留）——MD 显示 mismatch 警告行。
- **E 未启用分歧语义**：disagreements[].status 枚举严格验证（unresolved/inconclusive/resolved；未知值拒绝，不默认 resolved）。
- **标签反序稳定**：同输入（标签顺序不同）评分结果逐字节一致（对象集统计天然稳定，测试锁定）。

### R2 六项

1. **重复 N 正确**：2 features × 2 repeats 的 planned_n=2（repeat 数），不再报 4。
2. **空 claims 无 direction 不算 completed**：completed_state_mismatch 降级（上项）。
3. **pending direction 标签保留覆盖**：eligible 含 pending 标签的运行、 字段单独计数、不入分母——invalid direction 标签按 (arm, pair, feature, repeat) 索引进入覆盖统计（kind 过滤，非 direction 类 invalid 不混入）。
4. **MD 展示覆盖率与实际币种值**：每指标行显示 ；每臂每币种的实际 Decimal 金额（如 ）不再空桶。
5. **plan schema bool 拒绝**： 不被宽松等号当 1—— 显式检查。
6. **entry 身份与快照身份一致**： 拒绝（自洽摘要不豁免身份一致性）。
7. **score CLI 自身摘要自验**：input_digests 在 result_digest 计算前补入——CLI 产物文件复验通过。

### 验证（定向，未跑全量）

```bash
.venv/bin/python -m pytest -q docs/audit_f3_score_2026_09_09.py docs/audit_f3_plan_2026_09_09.py  # 33 passed（score 22/22 + plan 11/11）
.venv/bin/python -m pytest tests/test_paired_eval.py      # 35 passed
.venv/bin/python -m pytest tests/（触达回归）              # 182 passed
.venv/bin/python -m ruff check …                        # All checks passed!
```

真实 score CLI 复测：每指标带覆盖字段（ 等）、每臂每币种金额（ / ）、零效指标 null。

### R2 后文件清单（SHA-256，以此为准）

```
ebd6d5848bf4d315a7b3660c0692cc826a84dec46ba6c6c67b98ec98fd25d26f  tradingagents/evaluation/paired_eval.py
7d5783151bf53b6ffc9c7fdeb71917d3717ad08a0b6ebd16dff68eaeb65005a8  tests/test_paired_eval.py
```


---

## F3b R1 剩余同类项 + R2 六项修正附录（2026-09-09 06:4x）

Codex 评分审计扩展至 22 项（脚本未改动）→ 当前 22/22 通过。本轮完成的 R1 同类项与 R2 六组：

### R1 同类项（全部完成）

- 重复汇总六指标全实现：每 repeat 先聚合 features（planned_n=计划重复数不是 feature 数）；raw_per_repeat 逐次保留；零有效重复输出 0/N null。
- completed 状态语义验证：status=completed 但无非空 claims/direction/disagreements → completed_state_mismatch 计数并按 limited 处理。
- E 分歧 status 枚举严格验证（未知值拒绝不默认 resolved）。
- 标签反序稳定（对象集统计 + 测试锁定）。

### R2 六项

1. 重复 N：2 features x 2 repeats 的 planned_n=2。
2. 空 claims 无 direction 不算 completed（mismatch 降级）。
3. pending direction 标签保留覆盖：eligible 含 pending 运行、pending 字段单计、不入分母（invalid direction 标签按身份索引 + kind 过滤）。
4. MD 展示覆盖率与实际每臂每币种 Decimal 金额。
5. plan schema bool 拒绝（isinstance int 显式检查）。
6. entry feature_id 必须与 snapshot feature_id 一致。
7. score CLI 自身摘要自验（input_digests 在 result_digest 前补入）。

### 验证（定向）

- score 审计 22/22 + plan 审计 11/11 = 33 passed
- tests/test_paired_eval.py 35 passed
- 触达回归 182 passed
- ruff 全绿
- 真实 score CLI 复测（覆盖字段/每臂金额/零效 null）

### R2 后文件清单（SHA-256）

```
ebd6d5848bf4d315a7b3660c0692cc826a84dec46ba6c6c67b98ec98fd25d26f  tradingagents/evaluation/paired_eval.py
7d5783151bf53b6ffc9c7fdeb71917d3717ad08a0b6ebd16dff68eaeb65005a8  tests/test_paired_eval.py
```


### R2 item 7 补记（score CLI 最终摘要）

score CLI 已与 prepare 同规则：`input_digests` 补入后 **pop 旧 result_digest 并重算**——最终 `eval_score.json` 文件对自身完整内容摘要自验通过（Codex 新增的 `test_actual_score_cli_final_file_digest_is_self_consistent` 通过；两阶段 CLI 均 exit 0、指标正确、文件身份有效）。当前审计 score 22/22 + plan 11/11 = 33 passed。


---

## F3b R3 修正附录（2026-09-09 07:2x）：统一对象分类器

Codex 5 项新增语义探针 → 修复后 **27/27 score 审计通过**（plan 11/11 保持）。按统一方案重构而非补计数器：

### 架构：单一分类来源

`_classify_objects()` 建立 (pair, arm, repeat, feature, metric, object_id) 键控的**可评估对象全集**——先从预测对象构建（claim/reference/run/disagreement），再绑定标签并给每个对象**恰好一个状态**：scored / unlabeled / pending / unverifiable / unassessable。Overall、per-repeat、per-pair 汇总与 Markdown **全部消费同一分类结果**——不再有三套独立筛选逻辑。

### 五项修复

1. **跨 pair 隔离**：重复汇总按 (pair_id, arm, metric, repeat_index) 键控——每组的 features 在组内聚合、每 pair 只用自己 raw_per_repeat 有效 value；两 pair 各自 mean 不再混为 0.5。
2. **跨臂跨指标无污染**：pending/unverifiable 从分类器按 (arm, metric) 精确归属——candidate 方向 pending 不影响 baseline fact_support 覆盖字段。
3. **分母= scored 对象数**（直接计数，无减法猜测）：方向标签 target digest 不符 → `direction_target_digest_mismatch` 隔离 → S_UNASSESSABLE（不入 scored 分母）。
4. **无标签方向预测入覆盖**：有 direction=long 无标签 → eligible=1 / unlabeled=1 / den=0（对象全集从预测建立，非只看标签）。
5. **E 禁用臂拒绝**：`_classify_objects` 按计划 trials 的 `evidence_debate_enabled` 判定臂适用性——False 臂挂 disagreements → `ScoreValidationError`（矛盾输出拒绝，不作为比较观测）。

### 恒等式与混合控制

- **可复算门**：每指标 overall num/den == Σ raw_per_repeat num/den（测试参数化六指标 × 两臂全验证）。
- 双 pair / 双臂 / 双 feature / 双 repeat、不同标签状态（supported/unsupported/pending/invalid target/mixed）混合控制通过。
- E 未启用臂 e_unresolved/e_inconclusive 标 `applicable: False` + not_applicable note。

### 验证（定向，未跑全量）

- score 审计 **27/27** + plan 审计 **11/11** = 38 passed
- tests/test_paired_eval.py **40 passed**（新增 TestF3bR3UnifiedClassifier 5 项）
- 触达回归 **187 passed**
- ruff 全绿

### R3 后文件清单（SHA-256，以此为准）

```
08859f40bf3cf1213be20712c0b3a9aff1f258e9e15e14481fb8df9b81496712  tradingagents/evaluation/paired_eval.py
eb0b8a52c2e047833d3c77c5473d76264e08a79ed190c11d46c84770a640f308  tests/test_paired_eval.py
```


---

## F3b R4 修正附录（2026-09-09 07:5x）：E 状态分母与方向目标绑定

Codex 4 项新增语义探针 → 修复后 31/31 score 审计通过（plan 11/11 保持，脚本未动）。两组修复：

1. E 两个状态比例：每条合法且 E 启用的已列分歧在 e_unresolved 与 e_inconclusive 两个指标各建一个 scored 对象——hit = (status 匹配该指标命名状态)；已知其他状态是已观察 false 而非 unknown。分母 = 全部已列分歧（三种状态各 1 → 两个指标各 1/3）。overall 与 raw_per_repeat 同一对象集驱动，恒等式保持。E 未启用臂仍 applicable False 或矛盾拒绝。
2. 方向目标绑定：删除 wrong-placeholder 魔法字符串豁免。方向标签提供目标时 target_claim_id + target_claim_digest 必须成对绑定同一个实际 claim（非某 claim 有该 digest）；只提供一半 → direction_target_partial 隔离；不匹配 → direction_target_mismatch 隔离——均不入 scored 分母。无目标 run-scope 方向标签按原 run 语义评分。

验证（定向）：score 31/31 + plan 11/11 = 42 passed；tests 40 passed；触达 187 passed；ruff 全绿。

### R4 后文件清单（SHA-256）

```
a4350f12135ecceb63de021f360ab14953b93aa6f5f81ed6b78ae677c3665951  tradingagents/evaluation/paired_eval.py
eb0b8a52c2e047833d3c77c5473d76264e08a79ed190c11d46c84770a640f308  tests/test_paired_eval.py
```
