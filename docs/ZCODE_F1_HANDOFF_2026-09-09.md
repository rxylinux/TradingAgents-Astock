# F1 交接：离线不可变复盘记录与严格 AND as-of 检索（ZCode → Codex）

日期：2026-09-09。权威契约：`docs/F1_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`（十条 + 已决事项，未改动；设计已按契约调和：`docs/F1_REVIEW_RECORD_DESIGN_2026-09-09.md`）。E 已独立验收（1356 全量）后开工。无生产记忆写入/迁移、无数据抓取、无付费调用、无 LLM。

## 交付物

**`tradingagents/evaluation/review_record.py`**（纯 stdlib，argparse CLI）：

- **加载校验（规则 2/3/4/9）**：必填结构/身份（record_id 独立 run_id；同 run 多期限多版本并存）/决策/结果逐项校验；数值仅有限非 bool（bool/NaN/Infinity 拒绝，null 保持 null 无默认费率）；`expired_unresolvable` 必须显式带 reason（无自动 30 交易日过期）；到期显式给定且不早于窗口结束、窗口 start≤end；标注必须带来源+时间；naive datetime 拒绝；外部 digest 无快照标 `external_unverified`（verified_snapshot 必须携带快照）。
- **摘要与身份（规则 2）**：`record_digest` 对全记录规范 JSON（排除自身）确定性复算——声称失配拒绝；同 ID 异内容拒绝（不隐式覆盖）；同内容去重并报告 `_load_notes.duplicates`。
- **深不可变**：加载即深拷贝隔离；retrieve 输出为独立拷贝——嵌套修改输入或输出不污染下一次调用（测试覆盖）。
- **严格 AND 五门检索（规则 5/6/7）**：`retrieve_as_of(records, ticker, instrument_type, as_of, limit, ranking_version, query_window)`——决策/成熟/观测/发布/版本可用**全部** ≤ as-of 才可见；任一未知 → `unverifiable_<字段>`（不推导补齐）；maturity 未来但 publication 过去仍排除；pending 区分 not_yet_mature / overdue_unresolved / unknown_maturity；expired 不作已验证经验。时间严格解析：date → 上海 EOD 保守锚点（当日任何时刻都早于 date-EOD 锚，跨日才可见——测试锁定）；datetime 带必需 offset；Z 归一；不截前十位。
- **v1 排序（规则 7）**：同标的 > 同类型 > decided_at 降序 > record_id 字典序；先过滤后排序后 limit；截断单独报告不计时间违规；无合格记录返回完整排除清单；参数非法（bool/0/字符串 limit、未知 ranking_version）抛 `RecordValidationError`。
- **重叠边界（规则 8）**：仅显式 `query_window` 时做同 instrument/type 闭区间排除（边界同日算重叠）；未提供 → `overlap_filter=not_requested` 明示；记录窗口未知不给"不重叠"结论；不做记录间贪心去重。
- **CLI（规则 1/10）**：`python -m tradingagents.evaluation.review_record --file X.jsonl --as-of ... --ticker ... --instrument-type stock --limit N [--query-window-start/--query-window-end] --output-dir OUT` → `review_retrieval.json` + `review_retrieval.md`（参数/版本/输入文件 digest/选择/排除/截断全量；诚实免责文案）；拒绝 → 退出码 2 + stderr 错误清单，**不生成成功报告**（独立输出目录断言）。文件上限 2MB/2000 条、UTF-8 严格解码。

**Fixtures**：`tests/fixtures/review_records/records.jsonl`（15 条：基准合格/三未来门/AND 探针/pending 两态/未知两态/null 保真/晚决策/expired/同 run 双版本/最新排序/异标的）+ `overlap.jsonl`（内侧/边界同日/后置三窗口）。

## 真实 CLI 示例（本次实跑）

```bash
.venv/bin/python -m tradingagents.evaluation.review_record \
  --file tests/fixtures/review_records/records.jsonl \
  --as-of 2025-06-01 --ticker 600519 --instrument-type stock --limit 5 \
  --output-dir /tmp/f1_out
# exit=0 → selected=4 excluded=11（含 future_publication_time/
# future_record_available_at/not_yet_mature/overdue_unresolved/unknown_maturity/
# unverifiable_publication_time 等全部可审计原因；排序 rr-newest 首位）

.venv/bin/python -m tradingagents.evaluation.review_record \
  --file tests/fixtures/review_records/overlap.jsonl \
  --as-of 2026-01-01 --ticker 600519 --instrument-type stock --limit 10 \
  --query-window-start 2024-11-05 --query-window-end 2025-05-05 \
  --output-dir /tmp/f1_ov
# exit=0 → 仅 rr-ov-after 入选；rr-ov-boundary（起点=查询终点）按闭区间判重叠

坏 schema / 损坏 digest → exit=2 + stderr 清单，无 JSON/MD 产物（测试断言）
```

## 验证（定向，未跑全量）

```bash
.venv/bin/python -m pytest tests/test_review_record.py     # 31 passed
.venv/bin/python -m pytest tests/test_review_record.py tests/test_evaluation.py tests/test_evidence_debate.py
# 113 passed（触达模块定向回归）
.venv/bin/python -m ruff check tradingagents/evaluation/review_record.py tests/test_review_record.py
# All checks passed!
```

契约 #10 矩阵覆盖：真实 CLI 四出口（正常/全排除/坏 schema/坏 digest）；两次加载与反序输入稳定；同 ID 异内容拒绝（自洽摘要下构造）；同 run 多期限/版本共存；publication/observed/version 三种未来各排除；maturity 未来 + publication 过去仍排除；日中 as-of 与 date 精度 EOD 语义（当日任何时刻不可见、跨日可见）与 offset 跨日；pending 到期/未到期分别报告；未知收益/费用仍 null；bool/NaN/非法窗口/naive datetime 拒绝；闭区间重叠（边界同日）与 not_requested 分别呈现；嵌套输入不变/输出改动不污染；**生产 memory 源码未被触碰**（`memory.py` hash 7b2ce647… 断言 + 无 F1 补丁属性）。

## 限制（如实）

- 合成 fixture 只证明契约语义——不代表预测效果提高（CLI/MD 固定免责文案）。
- `availability_source` 首批全部 `declared_only`——不冒充 point-in-time 验证（契约 #5）。
- F2 生产接入（past_context 投影）与 F3 配对评测未开始——设计已就绪待审（`docs/F2_PRODUCTION_INTEGRATION_DESIGN_2026-09-09.md`）。

## 文件清单（SHA-256）

```
85daf1065dd7c3c82b2613051d64ea9aa30f2ed09ae9edb706b87752f2aefa58  tradingagents/evaluation/review_record.py
16589c308339dbfdc4fa462223c1ef6e978382f57a6c89e82d521b4da0b46f4f  tests/test_review_record.py
04f5000a4054afca4d28d1c47c3d506f095aabdfe19ec2133154171fd0c7ff9d  tests/fixtures/review_records/records.jsonl
b7ee710c3fec2ea1cf690d6ce13d9075fc7834b561876a00c508d366c415d0df  tests/fixtures/review_records/overlap.jsonl
```

---

## R1 修正附录（2026-09-09 02:2x，对应 docs/F1_REVIEW_2026-09-09.md R1）

Codex 独立审计 11 failed / 1 passed → 修复后 **12/12**（脚本未改动）。六组修复 + 相邻完成项：

1. **加载校验完整化（4 例）**：`schema_version` 必须等于实现版本（999 拒绝）；`identity.run_id/ticker/record_id` 非空字符串（空串拒绝）；`identity.window` 必须是对象（list 拒绝——原仅在 dict 分支内校验、坏类型绕过）；`retrieval_meta.ranking_version` 显式比对已知版本（bool 不冒充 1）。缺失/unknown/坏类型分别报错，不靠"取 key 不报错"声称已验证。
2. **非有限费用拒绝（2 例）**：`fees_assumption.bps` 复用有限非 bool 数值校验（NaN/Infinity/bool 拒绝）；规范 JSON `allow_nan=False`（不再 default=str 掩盖非法值）；`return_unit` 声明冲突拒绝（收益固定规范 `ratio:fraction`，不靠量级推断口径）。
3. **未来标注泄漏修复**：标注版本一致性——`annotations.annotated_at` 晚于 `record_available_at` = 矛盾输入，加载期拒绝（未来标注不得借旧版本进入历史检索）；标注形状（correct_risk_flags 列表、error_type/source/时间）完整校验。
4. **verified_snapshot 真实验证**：快照摘要按规范 JSON 确定性复算并与声明 `digest` 比较——失配拒绝；无快照必须 `external_unverified`（自称 verified 不升级）。evidence/thesis 同规则。
5. **去重不再破坏自身摘要**：去重说明移到 `VerifiedRecords` 载体属性（list 子类的 `duplicates`）——记录本体零污染，`load(load(x)) == x` 自洽可再验证；反序输入的去重元数据与输出稳定。
6. **排除清单稳定**：`excluded` 按 `(record_id, reason)` 排序——反序输入的完整结果（含排除报告与 limit 元数据）逐字节一致（测试断言 JSON 规范化相等）。
7. **真实 CLI 契约出口**：schema999 等一切加载/参数拒绝 → 退出码 2 + stderr 清单 + **不生成成功报告**（12 号审计探针实测）；合法记录控制例保持正常演示。

**相邻完成项（review 列出，本轮一并落实）**：

- **纯 API 入口验证**：`retrieve_as_of` 逐条复验结构与摘要（调用方改写 record 无法绕过——字符串数字等在入口被拒）；
- **排序语义恢复（F1 契约规则 7）**：同标的优先、同类型次之是**排序不是排除**——异标的记录排后仍可入选（测试断言 rr-index 垫后而非消失）；"仅同标的同类型"的投影过滤属 F2 接入层（设计已按此分工修订）；
- **事件有效期消费**：显式 `event_expires_at` 过期 → `event_expired` 排除；未声明不伪造过期；
- **入口资源界限**：`MAX_RECORDS`（去重前原始条数）与字节上限在纯 `load_records` 入口强制（不再只靠 CLI 末端）；`limit` 设实际上限 1000；
- **Markdown 全量化**：逐条排除身份（record_id + 原因，稳定排序）与输入文件摘要进入 Markdown（不再只在 JSON）。

验证（定向，未跑全量）：`tests/test_review_record.py` **47 passed**（新增 `TestF1R1Mirrors` 12 项 + 排序/去重载体更新）；Codex F1 审计 **12/12**；触达回归（F1+evaluation+E）**129 passed**；ruff 全绿；CLI 复测（selected=5 excluded=10，rr-index 按排序入列）。一处既有测试按排序语义更新并注明（同标的三条稳定平局 + rr-index 垫后）。

### R1 后文件清单（SHA-256，以此为准）

```
6952c889dc797546caec392606b2d26ab4f46fd8e737916236efe4f144935200  tradingagents/evaluation/review_record.py
242c31afc936361555f49a32dd3e93943808e12295c26ed105f329c00e342aa4  tests/test_review_record.py
04f5000a4054afca4d28d1c47c3d506f095aabdfe19ec2133154171fd0c7ff9d  tests/fixtures/review_records/records.jsonl
b7ee710c3fec2ea1cf690d6ce13d9075fc7834b561876a00c508d366c415d0df  tests/fixtures/review_records/overlap.jsonl
```

---

## R2 修正附录（2026-09-09 02:3x，对应 docs/F1_REVIEW_2026-09-09.md R2）

Codex 独立审计 6 项新增失败 → 修复后 **18/18**（12 旧 + 6 新，脚本未改动）。四组修复 + 同类边界：

1. **版本类型严格（2 例 + 同类）**：`schema_version` 与 `retrieve_as_of(ranking_version=…)` 两入口统一"已知正整数且非 bool"校验——`True` 不再宽松等号当 1，**浮点 1.0 与字符串 "1" 同拒**（`isinstance(int)` 显式排除 bool，`!=` 比较）；record.retrieval_meta 入口此前已严。
2. **重叠排除仅同 instrument/type（1 例）**：恢复 F1 排序语义后 overlap 条件仍作用于全部记录——现按契约 #8 仅对 `ticker == 查询标的 且 instrument_type == 查询类型` 的记录做闭区间排除；异标的记录照 F1 排序可作为经验入选，不受本查询窗口排除（F2 的严格同标的过滤另做）。
3. **快照存在即复算（2 例）**：摘要复算不再依赖自述标签——`snapshot` 字段**存在**即复算并与声明 `digest` 比较（verification 缺失/None/external_unverified 时带快照同样必须一致，否则拒绝）；无快照 + verified_snapshot 声明 → 拒绝（自称不升级）；无快照且无/弱标签 → 规范化为 external_unverified。
4. **JSON 解析边界的受控拒绝（1 例 + 同类）**：新增 `_check_json_legal` 递归合法性检查在 `validate_record` 入口执行——树中任意位置的 NaN/Infinity 浮点、非字符串字典键、无法 UTF-8 编码的字符串、非法类型全部收集为 `RecordValidationError` 明细（含字段路径），不再等到 canonical digest 才抛裸 ValueError——真实 CLI 对扩展字段 NaN 实测**退出码 2、无 Traceback、无成功报告**。

验证（定向，未跑全量）：`tests/test_review_record.py` **58 passed**（新增 `TestF1R2Mirrors` 11 项：三形态版本类型两入口、异标的不受重叠排除而同标的照常、两种弱标签下快照存在即复算、CLI NaN 扩展字段受控 exit2、递归深层 Infinity 拒绝）；Codex F1 审计 **18/18**；触达回归（F1+evaluation+E）**140 passed**；ruff 全绿。

### R2 后文件清单（SHA-256，以此为准）

```
2ebec79242290a1d594f1f308c1403a1096fa82c96fa3b2830e66a88c84d66f3  tradingagents/evaluation/review_record.py
717727c9e6f59cfd6e33c71cff2ee60ee6c84c626036d59131bc754bf49bc112  tests/test_review_record.py
```
