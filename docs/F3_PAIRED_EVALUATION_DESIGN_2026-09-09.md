# F3 离线配对评测：设计（已按 Codex 实施契约调和）

日期：2026-09-09（最终调和版）。**权威契约：`docs/F3_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`（十条 + 已决事项）——本文与其冲突处以契约为准；本设计仅保留契约的结构化展开，供实施参照。**
状态：**仅设计，未编码**。F2 R2 独立复验期间生产与测试保持稳定；F2 验收后按契约立即实施（F3a 计划/身份/切分 → F3b 评分/报告连续两段交接）。

## 0. 两阶段离线流程（契约 #1）

- **prepare（计划阶段）**：读显式特征文件、试验配置、时间切分 → 输出**不可变评估计划**。**不接受标签路径、不打开标签文件、不读 F1 完整 resolved 记录、不调用模型/取数、不改生产默认流程与记忆**。
- **score（评分阶段）**：读已冻结计划 + 两臂**预录预测文件** + 独立标签 → 输出 JSON + Markdown。评分不重写输出、不产生新预测。
- 实际 CLI（模块入口）两阶段；合成示例用**预录两臂输出**演示——不把替身差异称为真实 E 开关实验；实模型生成/调参/自动上线不在 F3。

## 1. 特征 schema 与版本时间（契约 #2）

```json
{"feature_id": "...", "ticker": "...", "instrument_type": "stock|index",
 "prediction_at": "...", "feature_available_at": "...",
 "decision": {"rating": "...", "prediction_horizon": "...", "decided_at": "..."},
 "target_window": {"start": "...", "end": "..."},
 "binary_event": null}
```

- 白名单键**严格**（含嵌套）——额外键拒绝；传入完整 F1 记录直接失败；outcome/
  annotations/labels 及派生统计禁入特征；字符串/版本/有限非 bool 数值/UTC 偏移/
  上海 date 精度沿 F1 原语；
- `feature_available_at` 是**该特征版本的可知时间**——不得从 F1 结果版本倒推、
  不得用 decided_at 替代；`feature_available_at` 与 `decided_at` 都不得晚于
  `prediction_at`；未知时点排除并保留 ID/原因，坏格式拒绝；
- 特征摘要覆盖全部规范特征并**复算**（不信任声称值）；白名单不证明真实可用性
  ——报告保留 declared_only 边界。

## 2. 配对身份与计划分母（契约 #3）

- 计划条目 = `{pair_id, feature_id, repeat_index, baseline, candidate}`；
  `repeat_count` 显式正整数非 bool，**全部预定重复列入计划**；
- 两臂**原样 normalized_config 入文件**并复算 config_digest；配置差集**只允许
  顶层 `evidence_debate_enabled` 从 false 到 true（必须 bool）**——模型版本、
  预算、数据、种子策略全相同，任何额外字段差异拒绝；
- 共享 feature 快照摘要须一致且对实际内容复算；
- 拒绝：重复身份、声明摘要失配、额外臂/计划外预测；**缺失计划内输出计
  missing，分母不缩水**；无记录 → 合法空报告、所有率 null（不造 0）。

## 3. 时间切分与 purge（契约 #4）

- 调用方显式给 train/validation/holdout **互斥有序日期或时刻区间**，按
  `prediction_at` 分配；无随机切分、无跨界重复归属；边界约定写入报告；
- **同 instrument+type 分组**，对**全部跨切分组合**（含 train×holdout）检查
  闭区间 `target_window` 相交；从**较早切分**剔除并逐条记录冲突两侧 ID 与
  `purged_overlap`；判定基于原始全集，结果与删除顺序无关；**不同组不互相
  purge**（跨标的不 purge）；
- **embargo 默认 0、显式非负整数日历日**；正值 = 较后切分样本的
  target_window.start 向前扩展 N 个上海日历日再检查早侧窗口（禁止换算交易日）；
- 切分/embargo/窗口参与计划摘要；purge 只控制标签窗口重叠，**不宣称经济或
  统计独立**。

## 4. 冻结预测结构（契约 #5）

- 每个预测绑定 `plan_digest + pair/feature/arm/repeat + feature_digest +
  config_digest`；含 `generated_at`（不早于 prediction_at）、状态
  `completed/limited/refused/failed`、有限有界 claims、numeric_references、
  disagreements、direction 或 null、usage；
- **stable claim_id 与 claim 文本摘要绑定**——标签精确匹配，不靠数组下标；
- 相同身份冲突输出拒绝（不选最后/最好）；缺失输出由计划补 missing；
- 声明 generated_at 与摘要**只证明输入自洽**——不证明过去真的冻结或模型未
  偷看标签（报告明确说明）。

## 5. 独立标签归属与可知性（契约 #6）

- 标签独立 schema + 输入摘要：`label_id、标注者身份、label_available_at、
  完整 pair/feature/arm/repeat + claim/reference/disagreement ID`（运行级标签
  明确 run scope）；
- **精确匹配对应冻结对象**：冲突重复标签拒绝；孤立/错臂/错重复/错文本摘要
  → 拒绝或隔离为 `invalid_label`，不能应用到其他臂；
- 评分 `as_of` 显式传入：标签可用时间 ≤ as_of；**方向/binary outcome 类另需
  显式 maturity、observed、publication 全部 ≤ as_of**——未知/未到期分别
  `pending/unverifiable`；**事实支持标签不套 outcome 到期门**；
- 预测缺失/失败不允许标签伪造成功输出；模型自述不能充当独立事实标签。

## 6. 指标单位与覆盖率（契约 #7）

- 每臂按**完整计划**保留 planned/missing/failed/refused/limited/completed 计数
  ——completed = 产出符合 schema 的结构化结论（≠被评正确、≠非拒答）；
- 每指标输出 `{numerator, denominator, value(分母0=null), eligible/unlabeled/
  pending/unverifiable}`，定义不混用：
  - 事实支持率 = supported 主张数 / 已评估主张数；
  - 数值错误率 = 外部核验错误引用数 / 已核验引用数；
  - 时点违规率 = **至少一项独立时点违规的运行数 / 已审核运行数**（多违规不
    重复计分母）；
  - E unresolved 与 inconclusive **分别计数**（分母为已列出 disagreements；
    valid evidence ID ≠ resolved；未启用 E → 分母 0/null）；
  - 方向命中 = 方向一致数 / 成熟且可评估方向数（Buy/Hold 不隐式映射涨跌）；
- 率与**全量覆盖率同表**——拒答/无标签不被过滤；Brier/校准仅对**预测前已有
  二元事件定义 + 合法概率 [0,1] + 成熟二元标签**计算；无合格事件 =
  not_applicable；**Brier 若未实现则明确"不支持"，不声称 F3 完整包含校准**。

## 7. 重复与成本（契约 #8）

- 保留所有重复原始指标；汇总 = 同 pair/arm **有效单次** value 的均值 +
  **总体标准差（÷N）**；另报 有效N/计划N 与全部缺失计数——不以 0 填缺失、
  不挑最好一次、**不宣称置信区间或显著性**；
- usage：已知总和与未知条数**分开**；logical 模型调用/实际 HTTP/工具调用/
  token **不混算**；
- 费用 **Decimal 精确累计、按币种分桶**——混合币种不相加；部分已知不得命名
  完整总成本；缺耗时 = null（score 脚本耗时≠模型运行延迟）；缺失输出保留
  成本不可知；预算上限不冒充消耗。

## 8. 确定性与 CLI（契约 #9）

- 规范 JSON 摘要/稳定排序/schema 版本沿既有约定；结果摘要覆盖计划/特征/
  预测/标签/参数的实际内容身份——**非空假摘要拒绝**；
- 同输入同 as_of 两次输出**逐字节一致**（不写墙钟）；保存输入与逐次原始
  数据供复算；
- 显式上限（≥2MB/输入、1000 features、100 repeats、20000 planned-arm-runs、
  100 claims/输出）在展开前检查；NaN/Infinity/bool 版本/非法 Unicode 受控拒绝；
- 真实 CLI：坏输入 exit2 + 简明 stderr（无 Traceback/成功报告）；正常输出
  JSON+Markdown；输入只读；固定免责文案；实外部预测需来源声明与未验证边界。

## 9. 交付与验收（契约 #10）

- 实现集中在 evaluation 离线模块 + 测试 + 合成 fixtures；**不接入生产模型路径**；
- 必测：真实两阶段 CLI、生成阶段未读标签/未触模型探针、未来特征版本与未来
  标签分离、同配置唯一 bool 差异、计划缺失输出分母、跨臂错标签、三切分非
  相邻重叠与边界相等/跨标的不 purge、重复反序稳定、手算混合拒答/无标签指标、
  未知 usage 与异币种、空数据/坏输入 exit2、摘要篡改拒绝；
- **F3a（计划/身份/切分）→ F3b（评分/报告）**连续编码，两段分别定向交接。

## 10. 与既有批次关系

- F1：时间原语/闭区间/严格解析直接复用；F2：投影通道互不影响（F3 不进生产）；
- E/D/C2 的 usage/代码表原语作为指标输入来源，语义不改；
- 本批完成即**队列 A/B1/C1/C2/D/E/F 收口**——不自动扩展新功能。
