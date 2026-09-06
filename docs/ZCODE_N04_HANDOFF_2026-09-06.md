# ZCode N04 离线评测交接

日期：2026-09-06 · 分工：Codex 方案与独立验收，ZCode 编码
基线：N03 后 865 passed（含全部未提交修复）。本批**未提交**。

Codex 终审回填：本批已通过独立全量验收，**1009 passed、14 skipped、52 subtests passed、5 warnings**。下方计数与交付文件已按最终产物核对；完整结果见 [N04 验收记录](N04_ACCEPTANCE_2026-09-06.md)。

## 交付范围

`tradingagents/evaluation/` 包（数据校验、确定性评分、汇总/对比、CLI）
+ `examples/evaluation/`（静态冻结示例）+ `tests/test_evaluation.py`（36 例回归）+ README 入口与 `examples/evaluation/README.md`。Codex 终审补充 `docs/OFFLINE_EVALUATION_GUIDE.md` 和验收记录。N03 的 `REPORT_COMPARISON_GUIDE.md` 是既有功能文档，不属于本轮新增实现。不做真实模型执行器（N04 后续），不做 N05–N08。

## 模块结构

| 文件 | 职责 |
|---|---|
| `__init__.py` | 公开接口（canonical_digest 等 7 函数 + EvaluationInputError） |
| `schema.py` | 套件/提交/报告/评测结果的完整字段校验 + 路径安全 + 时区解析 |
| `grader.py` | 8 项确定性检查 + 人工标注处理 + evaluate_suite |
| `comparison.py` | compare_evaluations（逐项退化扫描 + 语义校验） |
| `render.py` | evaluation/comparison Markdown 渲染 |
| `cli.py` | run/compare/digest 子命令 |
| `__main__.py` | `python -m tradingagents.evaluation` 入口 |

## 核心设计决策

### 分母与比率（任务书 R1）

- expected = 案例数 × trials_per_case；所有 trial（含 failed/invalid/
  missing）都在分母中。
- **事实支持率按声明总数汇总**（非 trial 均值）：supported 总数 / reviewed
  claims 总数；时点违规率 = future 总数 / evidence_backed 总数。零分母
  → null（不当零）。
- 配对比较中 null 比率不当作 0——双方均非 null 才统计变化。

### 八项确定性检查（4 节）

1. identity（缺类型→unknown，不推断）
2. team（顶层/质量卡/run_metadata 三源核对，全缺→unknown）
3. decision_present
4. rating（复用 parse_rating，缺→fail 不默认 Hold）
5. quality_status（按案例团队重算，不信自报）
6. quality_record（保存卡 vs 重算逐字段对比；unknown 卡→unknown；额外/
   重复角色→fail）
7. limitation_notice（重算受限→当前完整通知必须在场；只含标题→fail）
8. run_metadata（N02 完整结构 + created_at 有效 + 指纹匹配 + 与案例一致；
   缺失→unknown，损坏→fail）

### 人工标注与时点（5 节）

- review 绑定当前完整报告 digest；quote 必须出现在该字段文本中。
- `available_at > as_of` → future_evidence（微秒精度、Z 时区兼容 Py3.10）。
- 未来证据即使标为 supported 也不计入有效 supported。

### 比较安全（6 节）

- compare 入口先做**完整结构+语义校验**（字段集、trial 唯一、固定八项
  检查仅 completed 必须完整/非 completed 须空+pass=false、contract_pass
  与检查重算一致、状态计数与 trial 重算一致、provided 与非遗漏数一致、
  比率在 expected>0 时必须非 null 且 [0,1]、trial 级 observations 非负
  有限、review 结构与计数比率合理）。
- 逐项退化扫描**所有配对 trial**（含已 fail 的），missing trial 的原
  pass 检查列为 absent。
- `--fail-on-regression` 门控退化检查列表（非仅整体分类）。

### 路径与输出安全

- report_path 预检：绝对路径/`..`/符号链接逃出/非 .json → 拒绝整份清
  单（exit 2）。用 `Path.relative_to`，不做字符串前缀。
- 输出目录必须新建；已存在拒绝。写入前注册文件路径（写到一半的也清
  理）；失败只删本次文件 + rmdir 空目录（**禁止 rmtree**——其他写者的
  文件必须幸存）；清理失败不掩盖原错误。

## 验证记录

```
定向: Codex 独立 108 例 全部通过
      ZCode tests/test_evaluation.py 36 例 全部通过
Codex 最终默认全量: 1009 passed, 14 skipped, 52 subtests passed, 5 warnings
示例 CLI 端到端:
  run baseline → evaluation.json + evaluation.md ✓
  run candidate → 同 ✓
  compare --fail-on-regression → exit 1（7 个检查级退化）✓
  digest → 64 位 hex ✓
ruff check tradingagents/evaluation tests/test_evaluation.py: 通过
git diff --check: 通过
```

全程离线（requests + socket 拦截）；未动真实数据/凭据/全局环境。

## 示例说明

`examples/evaluation/` 含 6 案例 × 2 trial：正常个股（+具体合成事实标
注：营收 42 单位/毛利率 45.2%）、资料受限+未来证据、严格多数不足、指
数五位、旧格式报告（→unknown）、负例（无评级）。基线含遗漏/失败/混币
（index_normal t1 合成 CNY、stock_complete t2 合成 USD；候选同 index_normal t1 改为合成 USD——跨币不产生 cost_comparison）/未来证据标注；候选补齐遗漏但引入新退化（指数 trial 2 由
完成变失败，7 个 pass→absent（另 1 项 not_applicable 不计退化））。**全部合成数据，非模型实测**。

## R3 最终语义复核修正

- null_rate 补完整：expected > 0 时三项比率的 null 拒绝分支补齐。
- review 比率与声明重算一致：从 processed claims 重算计数与两种比率。
- config_snapshot=null 不修复：null 是显式损坏（fail），不做隐式修复。
- run_metadata.schema_version=True 不等价 1：bool 显式检查拒绝。

## R3 最终 checklist 修正

- `_process_review` 实际复用 `summarize_review_claims`（不再手写汇总）。
- claim 必填字段严格先验类型（claim_id 非空唯一字符串、field 非空字符串、
  verdict 字符串枚举、evidence_ids 字符串列表、has_future_evidence 必须
  布尔——null/int 不接受）。
- check_id 先验字符串再做 set 成员检查（list 不再 TypeError）。
- metrics 整数计数字段显式拒绝 bool（`_is_int`，True ≠ 1）。
- observations mean：有观测值时必须非 null 且与共享汇总一致（双向验证：
  null→有值拒绝、有值→null 拒绝）。
- cost_by_currency 项先验 dict 类型，mean 用同一 nullable 数值比较。
- 新增 8 个相邻分支回归（`TestSharedConsistencyRegressions`），其中最终修正了 reviewed_claims_count 的真实字段测试，并增加布尔值及未标注状态分支。
- README.md 添加离线评测入口（命令 + examples 链接 + 契约通过率≠事实
  准确率说明）。
- 交接修正：N03 REPORT_COMPARISON_GUIDE 误列为 N04 交付、示例 absent
  计数更正为 7（另 1 项 not_applicable）。

## 已知限制

- 事实支持率仅覆盖人工标注的声明——不是整篇报告的事实准确率，也不预
  测收益。`evidence_backed_claims` 含 contradicted/未来引用，不等于得
  到支持（渲染文案已区分）。
- 评级解析/允许集合符合性不是收益方向正确率。
- 观测值为导入测量值（延迟/token/成本），评分器不产生运行时计量。
- 本轮不提供真实模型执行器——离线阶段仅评估已保存的报告 JSON。
- 人工标注的 verifier 信任审核者输入（reviewer/reviewed_at/verdict），
  无法证明标注本身正确。
