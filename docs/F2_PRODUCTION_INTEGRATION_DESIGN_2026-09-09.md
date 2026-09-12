# F2 生产只读集成：F1 检索投影进入决策提示词（R2 调和版设计，待 Codex 审核）

日期：2026-09-09（R2：按权威契约 `docs/F2_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md` 十条逐条调和——冲突处以契约为准；R1 草案的拼接/排序遗留全部取代）。
状态：**仅设计，未编码**。F1 全量验收运行中——生产与测试保持稳定。

## 0. 范围（契约 #1）

- 单一可选配置 `config["review_records_path"]`，缺省对 **stock/index 一律关闭**（无提示词块、无 state 字段、报告"未记录"、拓扑/调用零差异）；
- fresh 准备期、**模型调用前**读取显式文件：沿用 F1 的**原始字节/条数上限**、UTF-8 严格解码与**全部记录校验**（含 JSON 边界合法性）——坏输入 **fail-fast**，绝不降级为空文件/空投影继续跑；
- CLI/Web 提供与 D2 同风格的显式入口（CLI 旗标 / Web 高级项 + 上传临时存储）；不写全局配置、不自动扫描历史目录；
- 不改生产记忆格式、不迁移旧数据、不新增模型/网络调用；F3 另审。

## 1. F1/F2 分层：先校验、后过滤、再检索、再 limit（契约 #2，R1 草案"排序自然排除"与"先检索后过滤"全部取代）

固定顺序，不可颠倒：

1. **整体验证**：`load_records`（F1 完整校验 + digest + 去重载体）；
2. **可信过滤（在 limit 之前）**：`identity.ticker == 可信标的 且 instrument_type == 可信类型`——**先过滤再限量**，绝不能先截断 limit 再过滤而漏掉合格记录；
3. **对匹配集合调用 F1 时点检索**（`retrieve_as_of`，AND 五门 + 排序在匹配集内进行）；
4. **投影最多 5 条**。

- `as_of`、`ticker/instrument_type`、`ranking_version` 全部取**可信运行准备上下文**（trade_date / company_of_interest / config instrument_type / F1 已知版本常量），文件与记录不可覆盖；
- 异标的/异类型单列计数（"已按投影策略排除 N 条"）——其标题/观点/收益**不进提示词**；
- F2 **不传 query_window**：`overlap_filter=not_requested` 明示，不臆造预测期限。

## 2. 独立 state 与规范摘要（契约 #3，R1 的 result_digest 空锚问题修正）

`AgentState.review_projection`（独立持久 dict，**不把文本拼入 past_context**）：

```json
{
  "schema_version": 1,
  "run_binding": {"run_id": "..."},
  "trusted_context": {"ticker": "...", "instrument_type": "...", "as_of": "..."},
  "projection_policy": "same_ticker_and_type_only",
  "ranking_version": 1,
  "input_digest": "sha256:<原始文件字节>",
  "selected": [ {"record_id", "decided_at", "rating", "maturity_date",
                 "raw_return", "return_unit", "availability_source"} ],   // ≤5，经校验快照
  "excluded_stats": {"not_yet_mature": n, ...},
  "excluded_cross_instrument": n,
  "limit_truncated": n,
  "limitations": [...],
  "payload_digest": "sha256:<规范 payload 摘要>"
}
```

- **摘要精确覆盖全部规范 payload（除自身）**——R1 草案"text 单列、digest 只盖结果"的空锚修正：**不持久自由 text 字段**；提示词文本由**经过校验的 selected 快照确定性渲染**（同输入同渲染）——任何会进入提示词的字节都在被摘要覆盖的 payload 内；
- 渲染只含 F1 合规选中记录与必要来源/时间/单位/availability——不发送 raw 记录表或被排除内容。

## 3. 独立元数据锚定（契约 #4）

fresh 在 `run_metadata` 写独立比较锚（与 D2 面板、E 模式锚并列互不干扰）：

```json
run_metadata["review_projection"] = {
  "enabled": true, "schema_version": 1, "input_digest": "...",
  "payload_digest": "...", "ticker": "...", "instrument_type": "...", "as_of": "..."}
```

恢复校验（`_validate_resumed_review_projection`，模型调用前）：state 与 run_metadata 双侧——可信 run 非空且 `run_binding.run_id` 一致；标的/类型/as_of 与本次恢复上下文一致；两个 digest、schema/ranking 版本、策略一致；`payload_digest` 按 payload 复算；嵌套结构合法。无锚、异 run、错类型、未知版本、正文/记录/统计篡改**一律拒绝**（断点保留、不清空、不伪装缺失）。fresh 新 run 身份建立绑定；并发 run 不串。

## 4. 真正恢复纯读（契约 #5）

- `_safe_resume_checkpoint` 校验持久结构，**绝不重读 `review_records_path`、不按当前文件重检索/刷新旧投影**（删改文件零影响——SQLite 关闭重开 + prepare + `invoke(None)` 实测：续跑完成、已完成节点不重跑、payload 原样）；
- 模式锚点：断点含 F2 而当前关闭（或反向）→ 明确拒绝；真正无 F2 的旧断点在**关闭模式**下兼容；**不用恢复时才提供的文件补历史投影**；
- 存在但损坏 → 拒绝且断点原样保留。

## 5. 五工厂共用验证 + 提示词入口自验证（契约 #6）

- 同一 helper（验证 + 从 selected 快照渲染）服务 RM、stock Trader/PM、index Trader/PM **五个真实工厂**；
- **直接调用工厂**也不能拼接未验证 state 的 text：helper 对无效/无锚/摘要失配的 payload 返回**受控说明（无任何数值与历史标注正文）**；准备/恢复遇坏投影 fail-fast；
- 模型调用次数与原流程相同（计数断言）。

## 6. 三态与预算（契约 #7）

- 关闭 → 无块；开启零合格 → **非空限制块**（排除分类 + "无合格不等于历史无事件"）；有合格 → ≤5 行；
- 预算：单行 160 字符、总 1200 字符**含标题、限制与截断说明**——限制优先预留，长记录不得冲掉限制；值为 null 明确标 unknown；模型自述/外部标注不当校准胜率；`availability_source=declared_only` 不升级为 verified 历史事实；"盈利≠推理正确"固定文案。

## 7. 既有记忆语义保留（契约 #8）

`past_context` 由生产记忆按原流程生成/刷新/拒绝陈旧——F2 独立通道在**提示词组装层**合并；投影不覆盖记忆限制、记忆刷新不清除投影、恢复时分开验证。`TradingMemoryLog` 源码与用户记忆内容零改动（hash 断言）。测试用真实一致的历史上下文替身（非 None/空串）避免 fixture 差异掩盖 F2 路径。

## 8. 出口与可复核性（契约 #9）

统一 JSON 保存 payload 与锚点；Web/MD/PDF 共用渲染三态（输入/投影摘要、策略、时间边界、选择/排除统计、限制）；本机路径不进提示词；原始文件只读；**正常 CLI/Web 配置输入经真实 prepare 进真实工厂与报告**——不能只写离线 helper 声称集成完成。

## 9. 验收矩阵（契约 #10，获批后据此写测试）

默认 stock/index 关闭零新增调用；混合标的且超 limit 时先过滤再限量仍正确选择；五种 F1 时间门/未来标注/零记录限制在**实际提示词**可见/不可见断言；直接工厂遇错 run/摘要/text → 无数值；fresh 坏文件零模型调用；两个 fresh 同文件数值相同 run 不同；真实 SQLite 恢复删文件零读取、执行完成、payload 保留；开关/版本/身份/嵌套篡改拒绝且断点保留；past_context 更新不覆盖投影；五工厂调用计数一致；三出口三态；CLI 正常与坏输入；生产 memory 源码 hash 不变。

## 10. 实施清单（F1 验收后即刻）

1. `tradingagents/evaluation/review_projection.py`：加载→过滤→检索→快照→payload（规范摘要）→确定性渲染 + 三态。
2. `agent_states.py` 字段 + `trading_graph.py` fresh 注入/锚点 + `_validate_resumed_review_projection`（纯读）。
3. 五工厂接入共用 helper + `_log_state` + 三出口。
4. CLI 旗标 / Web 高级入口（D2 风格）。
5. `tests/test_review_projection_integration.py`：§9 全矩阵 + handoff。
