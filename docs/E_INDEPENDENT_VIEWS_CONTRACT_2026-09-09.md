# E 独立初判、分歧定位与有界反证补查：实施契约（ZCode 起草，待 Codex 审核）

日期：2026-09-09（R1 修订：按 Codex 口头修正五点——转载去重键、引用有效≠语义支持、
单工具调用硬限、增量调用如实计数、首版仅新闻 artifact 工具 + 参数/日期硬校验 + 恢复预算持久化）。
上游任务书：`docs/DECISION_ACCURACY_PLAN_2026-09-08.md` E 节；Codex 将另发最终 E 契约，以其为准。
状态：**仅契约，未编码**。D2 生产与测试在本契约审核期间保持不动。

## 0. 目标与红线

**目标**：多空研究员在看到对方观点**之前**，基于同一时点证据各自形成结构化初判；研究经理产出证据链接的分歧清单与待核实问题；对**最多 2 个问题、每个恰好 1 次工具轮**做有界反证补查；补查结果回流后进入**原辩论机制**（不替换）。全部离线可测、预算由代码强制。

**红线（任务书原文落实）**：

- 工具只从**当前已配置**的数据工具中选择——不扩大网络权限、不新增数据源；
- 达到补查上限后分歧保留 `unresolved`，**不能无限辩论**；
- 来源转载与重复报道**不投票**；官方披露优先只是**来源权重规则**，不保证完整或无修订；
- **冲突不强行平均数字**——数值分歧保留双方原始值与出处；
- 单独 **A/B 比较**与原辩论机制：E 的开关不与模型/数据源变更混在一起，保证可归因；
- 不宣称投资准确率提升；评估只报契约性指标（§8）。

## 1. 开关与默认行为（受控比较的前提）

新增配置键（单一开关 + 固定常量，不做可调旋钮）：

```python
config["evidence_debate_enabled"] = False   # 默认关闭
```

- **默认 False = 图拓扑与今天完全相同**：不加初判节点、不加分歧节点、不加补查工具轮；现有辩论/决策/风控路径零改动（回归"零改动证明"：默认下 state 无 E 新字段、节点执行序列与现版本一致）。
- 开启后仅在 **bull/bear 辩论开始之前** 插入 E 阶段；辩论轮数、条件路由、RM/Trader/PM 全部沿用现状。
- 2 个问题 / 每问题 1 次工具轮是**代码常量**（`RECHECK_MAX_QUESTIONS = 2`、`RECHECK_TOOL_ROUNDS = 1`），由图拓扑与节点逻辑强制，不依赖提示词自律，不可经配置放宽。

**预算（显式、可观测、如实计数——E 开启即增量）**：

- **增量 LLM 调用（E-on 相对 E-off 的真实新增，写入 usage 并在报告中如实呈现）**：
  2 次独立初判（bull/bear 各一）+ 1 次分歧定位 + 每问题 1 次工具调用构造 = **最多 5 次 LLM 调用**；
  既有辩论/RM/Trader/PM 调用数不变。不声称"无新增调用"。
- **工具调用硬限：每个问题恰好 1 次工具调用（不是无界 ToolNode 批）**——由"结构化工具调用 schema → 代码构造恰好一个 tool_call"实现（§4），收集节点断言本问题恰好 1 条 ToolMessage；
- **首版工具面：仅既有新闻 artifact 工具**（`get_news` / `get_global_news`），并做**硬参数/日期校验**（§4）；
- 输出预算：复用既有输出长度限制机制并对 E 节点显式配置；超支如实记录 `budget_exceeded` 观测字段而非静默截断；
- **预算持久化**：E 的 `usage`（已耗调用/工具轮/已消费问题槽位）随 state 进 checkpoint；断点恢复**不重跑已消费的问题**、不重置已计数的预算（§4 恢复语义）。

## 2. 阶段一：独立初判（bull/bear 先隔离后互见）

**输入（同一时点证据，双份相同）**：两研究员各获得 C1 证据索引投影（`evidence_context_for_prompt`，含来源状态/覆盖限制/排除说明）+ 分析师报告。两人在此阶段**互不可见对方任何输出**（不同节点、无共享通道——复用 R1 分支隔离模式，不引入新的共享可变状态）。

**结构化初判 schema（`InitialView`）**——注意：初判是**增量**结构化调用（在两研究员既有辩论调用之前各多一次），计入 §1 的 usage 与报告：

```json
{
  "position": "bull | bear",
  "direction": "long | short | neutral",
  "top_claims": [
    {"claim": "一句话论断",
     "evidence_ids": ["ev-..."],
     "confidence": "low | medium | high"}
  ],
  "key_assumptions": ["..."],
  "would_change_mind_if": ["可观察条件（自由文本，标 manual_review）"]
}
```

**代码确定性后处理**（沿用 C2 模式，模型自述不覆盖）：

- 引用校验：`evidence_ids` 经 `classify_reference` 分 dangling/future/unknown_time/unverifiable_cutoff——悬空与未来引用**剔除出主张列表**并计为缺口（不凭空补证据）；
- 空 `top_claims` 或全部引用失效 → `view_status: "degraded"` + 原因（不伪造主张）；
- `confidence` 只是模型自述标签，卡片层标 `uncalibrated`（同 C2 语义，不显示胜率）；
- 引用数量上限（如每主张 ≤5、每方 ≤10 主张），超出截断明示。

## 3. 阶段二：分歧定位（研究经理产出 disagreement 清单）

研究经理（既有 RM 节点前的独立确定性步骤 + 一次结构化调用，或 RM 既有调用内扩展——实现时二选一，倾向独立小节点以保持 RM 提示词稳定）同时看到两份初判，产出：

```json
{
  "disagreements": [
    {
      "topic": "冲突主题",
      "bull_claim": {"claim": "...", "evidence_ids": [...]},
      "bear_claim": {"claim": "...", "evidence_ids": [...]},
      "conflict_kind": "fact | interpretation | missing_data",
      "period_mismatch": "双方所指期间不一致时的说明（无则空）",
      "decision_impact": "该分歧的答案会如何改变结论（必填，空则该问题不得进入补查）"
    }
  ],
  "recheck_questions": [
    {
      "question": "待核实问题（必须可由已配置数据工具回答）",
      "disagreement_index": 0,
      "answer_changes": "哪个答案会改变结论（必填）",
      "proposed_tool": "已配置工具名（如 get_news）",
      "status": "pending"
    }
  ]
}
```

**代码确定性规则**：

- `recheck_questions` 硬上限 **2**：超出部分保留在 disagreements 但标 `not_rechecked`（优先级由模型排序，代码不重排）；
- 每个问题的 `proposed_tool` 必须在**当前图实际注册的工具集**内（股票图与指数图各自校验）——未注册工具名 → 该问题直接 `unresolved`（reason: `tool_not_available`），不得因 E 扩大工具面；
- `decision_impact`/`answer_changes` 为空 → 问题退回 `unresolved`（不浪费补查预算）；
- **数字冲突**：`conflict_kind == "fact"` 且双方带数值 → 保留双方原始值与出处，不做平均、不做仲裁（补查证据呈现后仍冲突 → 双值并存的 `unresolved`）；
- **来源规则（R1 修正）**：C1 的 `evidence_id` 是**来源特定**的（ID = source + digest 前缀）——跨来源转载**不会**自动同 ID。去重键改用**内容摘要**（`content_digest`，不含 source）：同 title+content+published_at 的跨来源记录视为同一篇被转载，只计一次"证据事实"；轻微改写导致 digest 不同则检出不到——如实标注为 best-effort 限制，不做语义去重。官方披露优先只是呈现顺序权重，不作为"更真"的证明。

## 4. 阶段三：有界反证补查（≤2 问题 × **恰好 1 次工具调用**）

**工具面（首版收窄，R1 修正）**：仅**既有新闻 artifact 工具** `get_news` / `get_global_news`——它们经 C1 通道产出结构化 artifact；不开放 K 线/指标/财务类工具，不注册任何新工具，不扩大网络权限。

**硬参数/日期校验（代码级，先于执行）**：

- 工具名必须在允许集内（模型给出的其他名字 → 该问题 `unresolved(tool_not_allowed)`）；
- 参数经既有校验：ticker 走 `news_data_tools` 的 A 股代码校验；`start_date/end_date/curr_date` 走严格日期解析并经 **C1 可信截止截断**（越界请求收窄到分析时点并留痕）——补查不得引入分析时点之后的正文。

**单次调用硬限（不是无界工具批）**：

- 提问节点不直接让模型自由发 tool_calls；模型产出**结构化 `ToolInvocation`**（`{tool, args}`），代码校验后**构造恰好一个 tool_call** 交给既有 ToolNode——批量/并行多工具调用在结构上不可能发生；
- 收集节点断言本问题新增 ToolMessage 恰好 1 条；之后**无条件推进**（没有回到提问节点的边——轮次由边结构锁死）；
- 无第二问题（或问题提前 `unresolved`）→ 条件边跳过。

**结果回填（R1 修正：引用/时点有效 ≠ 语义支持——代码不做任何 resolved-side 裁决）**：

```json
{
  "question": "...",
  "status": "evidence_collected | inconclusive | unresolved",
  "evidence_ids": [...],           // 本次调用产生的合格证据（经 C1 引用/时点校验）
  "content_digests": [...],        // 内容摘要（跨来源转载去重键）
  "observation": "工具返回的事实性摘要（代码不解释、不仲裁）",
  "human_review": true,            // 恒真：证据供人工/后续复核，分歧是否被"解决"不由代码判定
  "usage": {"tool_invocations": 1, "llm_calls": n, "tokens": {...}}
}
```

- **移除 `resolved_side`**：ID 存在且时点合规只说明"证据可用"，**不能**判定主张被支持或分歧已解决；一般文本主张一律保留 `unresolved`/`inconclusive` + 待人工复核的证据清单；
- 证据不足/无新记录 → `inconclusive`（计入 unresolved 大类）；
- 数字冲突仍**双方原始值并存**（不平均、不仲裁）；
- 补查产生的 artifact 经**既有 C1 通道**进入证据账本（事件 `role=recheck_q1/q2`），受可信截止约束；
- 两个问题耗尽后仍有分歧 → 原样进入辩论，标 `unresolved_after_recheck`——**辩论轮数不因此增加**。

**恢复与预算持久化**：E 段落的 `usage` 与已消费问题槽位（q1 已跑/q2 未跑）随 checkpoint 保存；恢复只读持久化状态——**不重跑已消费的问题、不重置预算计数**，未消费的问题按剩余槽位继续（同 D2 恢复纯读语义）。

## 5. 状态、持久化与兼容

- 新 state 字段（默认缺失 = 旧行为）：`initial_views`（dict：bull/bear 两份 + 校验结果）、`disagreement_report`（§3/§4 合并产物）、全部 JSON 可序列化、随 checkpoint 保存/恢复（恢复只读，不重算——同 D2 恢复语义）；
- `_log_state` 增两键（旧 JSON 无键 → null = 未记录）；Web/MD/PDF 三出口共用渲染（`render_debate_evidence_md`：初判主张+引用、分歧清单与状态、补查结果与预算使用）；
- E 产物不改变 C2 假设卡、质量卡、证据账本、D 面板的任何既有语义；RM 的 `investment_plan` 流程不变。

## 6. 图拓扑与默认路径的隔离实现

- `setup.py`：`evidence_debate_enabled=True` 时在辩论起点前插入 E 节点链（复用分支隔离/条件路由既有模式）；False 时**不注册任何 E 节点**（拓扑字节等价于现状——测试断言编译图节点集合与现版本一致）；
- E 节点的 LLM 均走既有 `bind_structured`/`invoke_structured_or_freetext` 通道（共享重试预算 B1 语义）；无新增 provider 逻辑。

## 7. 评估与 A/B（可归因）

- **同模型、同数据配置、同随机种子策略**下 E-on/E-off 成对运行（离线 fixture 图，不跑真实模型——用录制/替身 LLM 保证两侧输入一致的对照）；
- 指标（契约性，非投资效果）：初判引用合规率（无悬空/未来）、分歧覆盖率（辩论结论是否回应了 listed disagreements）、补查完成率/预算使用率、unresolved 率、**增量 LLM 调用与工具调用计数（E-on 实测新增 ≤5 调用 + ≤2 工具调用，如实报告而非声称无新增）**、输出预算超支率（budget_exceeded 计数）；
- 报告明确"E 未改变模型与数据源，只改变流程结构"——差异可归因于 E 本身。

## 8. 离线真实图测试矩阵（验收前置）

1. 默认关闭：编译图节点集合 == 现版本；state 无 E 字段；辩论序列不变（零改动证明）。
2. 独立性：bull/bear 初判节点的输入 state 中互不含对方输出（断言两侧 prompt 投影）；初判引用经 C1 校验（悬空/未来剔除 + degraded 状态）。
3. 分歧上限：模型返回 5 个问题 → 只有前 2 个进入补查，其余 `not_rechecked`；空 `decision_impact` → 退回 unresolved。
4. 工具面约束：`proposed_tool` 不在注册集 → unresolved(`tool_not_available`)；不注册任何新工具。
5. 单调用硬限：真实 StateGraph + 真实 ToolNode + 离线 vendor 替身——每问题**恰好 1 条 ToolMessage**（结构化 ToolInvocation 保证；构造模型试图返回多个工具的 fixture 也只能产生 1 次执行）；出边后不可回流；第二问题缺失时跳过。
6. 工具面与参数：`proposed_tool` 不在 {get_news, get_global_news} → unresolved(tool_not_allowed)；非法 ticker/日期 → 拒绝执行并留痕；越界日期被可信截止截断。
7. 补查入账：artifact 进入 `evidence_bundle`（role=recheck_q*），受可信截止约束。
8. 无代码裁决：输出**不含 resolved_side 字段**；一般文本主张只有 evidence_collected/inconclusive/unresolved + human_review 证据；数字冲突双方值并存。
9. 转载去重（R1 修正）：同一文章经两个来源（不同 evidence_id、**相同 content_digest**）只计一票；轻微改写（digest 不同）检出不到——断言该限制如实标注。
10. 恢复与预算持久化：q1 消耗后崩溃 → 恢复只跑 q2；usage 计数延续不重置；已消费问题不重跑（断点前后的 usage 差恰为 q2 的增量）。
11. 出口：三出口渲染新旧两态；`_log_state` 落键；usage 计数与实际调用/工具调用一致（stub LLM 计数器对照）。

## 9. 实施顺序（获批后）

1. `tradingagents/agents/debate_evidence.py`：schema（InitialView/DisagreementReport/RecheckResult）+ 确定性校验/上限/回填 + 共用渲染。
2. `setup.py`：可选 E 节点链（默认不注册）+ 条件路由；`trading_graph.py` 配置透传。
3. 研究员/RM 提示词的最小扩展（初判调用与分歧调用；不动既有辩论提示词语义）。
4. `tests/test_debate_evidence.py`：§8 矩阵（真实图 + 离线替身）。
5. handoff（hash/真实测试/限制）→ Codex 独立审计。

## 10. 待 Codex 决策点

1. 分歧定位节点：独立小节点（倾向）还是扩入 RM 既有结构化调用？
2. `recheck_questions` 的 2 个上限是否允许配置低于 2（收紧）但永不高于 2？
3. 补查工具范围：仅新闻类工具（get_news/get_global_news），还是全部已注册工具均可被 proposed？
4. （已按 R1 修正移除 resolved_side）后续若引入机器可核事实（如 D 面板数值型主张）的自动判定，是否单列"可核主张"白名单而非放开一般文本？
