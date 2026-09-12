# F1 离线不可变复盘记录与纯 as-of 检索：设计（已按 Codex 实施契约调和）

日期：2026-09-09（R1 调和版）。**权威契约：`docs/F1_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md`（十条，Codex 维护）——本文与其冲突处以契约为准；以下设计已逐条对齐。**
上游：`docs/F_MEMORY_EVALUATION_CONTRACT_2026-09-09.md`（F 草案，其与 F1 契约冲突条款被取代）。
状态：**仅设计，未编码**（E 验收后按契约开工，无需再等用户确认）。E 源码保持稳定。

## 0. 范围（契约 #1）

- 离线模块：纯校验/检索函数**零 IO/网络/模型**，仅消费显式传入的 JSONL 文本或已解析列表；
- **真实 CLI 为首批交付物**：读用户显式文件 + `--as-of/--ticker/--instrument-type/--limit/--query-window(可选)` → 输出 JSON 与 Markdown（选择理由/排除清单/未知项/规则版本全量呈现）；拒绝 → 退出码 2 且不生成成功报告；
- 不改生产 `TradingMemoryLog`/用户历史/缓存/记忆；测试用临时 fixture；不计算新收益、不扩展 `_fetch_returns`、不自动分类错误原因（F1 契约 #1 红线，取代 F 草案中 F1 段的 MAE/错误分层内容——那些移到 F2/F3 范围）。

## 1. ReviewRecord schema（契约 #2/#3/#4）

```json
{
  "schema_version": 1,
  "record_id": "rr-<独立于 run_id 的稳定身份>",
  "identity": {
    "run_id": "...", "ticker": "600519", "instrument_type": "stock",
    "window": {"start": "2024-11-05", "end": "2025-05-05"},
    "version": 1
  },

  "decision": {
    "rating": "Buy",
    "prediction_horizon": "3-6 months",        // 仅描述性自由文本
    "decided_at": "2024-11-05T18:00:00+08:00",
    "thesis_digest": {"value": "sha256:...", "verification": "external_unverified"},
    "evidence_version": {"manifest_digest": {"value": "sha256:...", "verification": "external_unverified"},
                         "evidence_event_count": 12}
  },

  "outcome": {
    "status": "pending | resolved | expired_unresolvable",
    "maturity_date": "2025-05-05",             // 显式给定；不可从 horizon 推导
    "observed_at": null,
    "publication_time": null,                   // 与 observed_at 严格分离
    "raw_return": null,                         // ratio:fraction；有限非 bool
    "alpha_return": null, "max_adverse_excursion": null,
    "fees_assumption": null,                    // 明确 bps 或 null；无默认费率
    "benchmark": "000300.SH"
  },

  "annotations": {
    "error_type": null,                         // 仅显式外部标注（带来源+时间）
    "correct_risk_flags": []                    // 同上；C2 状态/数据缺口不自动解释为推理错误
  },

  "record_available_at": "2025-05-06",          // 版本可用时间锚点（独立字段）
  "availability_source": "declared_only",       // declared_only | verified_snapshot
  "record_digest": "sha256:..."                 // 规范 JSON 全记录摘要（排除自身）
}
```

**身份与不可变（契约 #2）**：

- `record_id` 独立于 run_id：同 run 多期限/多版本并存；唯一身份至少 = run + instrument/type + 明确窗口 + 版本；
- `record_digest`：对**全记录**（除 digest 自身字段）规范 JSON 的确定性摘要——加载时复算；**同 ID 不同内容拒绝**（显式错误，不隐式覆盖）；同 ID 同内容去重并报告；
- **深不可变**：load/retrieve 不修改输入或内部可共享子对象（返回深拷贝或不可变结构）；文件只读 ≠ 对象不可变——测试覆盖嵌套修改与两次调用隔离。

**未知保持未知（契约 #3）**：数值仅有限非 bool；收益 `ratio:fraction`、费用 bps 等闭合单位；禁止 null→0、字符串数字强转、NaN/Infinity；缺费用/基准/MAE 保持 null + 原因；不从 rating 推收益或正确性。

**到期显式（契约 #4）**：`maturity_date` 与 `window.start/end` 只来自显式输入——**不把 "3-6 months" 映射为日期**；窗口校验 start ≤ end 且到期不早于窗口结束；`pending` 永不进已验证经验（区分 not_yet_mature / overdue_unresolved / unknown_maturity）；**无自动 30 交易日过期**（F 草案的该条款被取代）——`expired_unresolvable` 只能是显式带原因状态，不套周末日历猜交易日，也不作为已解决事实。

## 2. 完整可知时间门槛：AND 而非 OR（契约 #5，取代本稿初版的"或"语义）

一条记录作为**已验证经验**可见，必须**同时**满足（全部以 as-of 比较）：

1. **决策形成**：`decided_at` ≤ as-of（晚决策不存在）；
2. **已成熟**：`maturity_date` 显式存在且 ≤ as-of（缺失 → unknown_maturity，不可见）；
3. **已观测**：`outcome.observed_at` 显式存在且 ≤ as-of；
4. **已发布**：`outcome.publication_time` 显式存在且 ≤ as-of；
5. **版本可用**：`record_available_at` 显式存在且 ≤ as-of。

- 任一未知 → 排除并给明确 `unverifiable_<字段>` 原因，**不从其他时间推导补齐**；
- publication 早但观察/版本晚 → 不可见；观察早但 publication 晚 → 不可见；
- `availability_source` 保留来源事实：首批 fixture 声明 `declared_only` 不冒充 point-in-time 验证；作者自述 publication 不是严格历史快照证明。

## 3. 时间严格解析（契约 #6）

- 接受 `YYYY-MM-DD`（date 精度）与带 Z/offset 的完整 datetime，保留精度；拒绝 naive datetime、无效尾部、非法日期、**不截前十位**；
- as-of 为日期 → 上海当日结束；datetime → 真实瞬时转换；date 精度可用时刻按当日结束**保守比较**（不伪造 00:00）；
- 同日带 offset 跨日与 as-of 日中场景必须验证（测试矩阵覆盖）；
- 决策原值与 outcome/标注分别受各自时点过滤——不用最终版 result 重写早期决策。

## 4. 检索函数（契约 #7）

```python
def retrieve_as_of(records, *, ticker: str, instrument_type: str, as_of: str,
                   limit: int, ranking_version: int = 1,
                   query_window=None) -> RetrievalResult
```

- 入参必填且校验：可信 ticker/instrument_type、as_of、**有限正非 bool limit**、已知 ranking_version；
- 顺序固定：**先完整校验/过滤，再排序与 limit**；limit 截断单独报告，不计为时间违规；
- v1 排序：同标的 > 同 instrument_type > decided_at 新到旧 > record_id 字典序稳定平局；其他相关性（行业/市场状态）只消费显式字段，未知不伪造；
- 每条排除带可审计原因；可同时保留多个缺口；无合格记录也返回完整限制清单；
- 纯函数：无 IO/状态/随机，输入深不可变（§1）。

## 5. 重叠查询边界（契约 #8，取代初版"记录两两去重"）

- **仅当调用方显式给 `query_window`** 时做同 instrument/type 的窗口重叠排除；查询窗口进函数签名与 CLI 参数，严格校验；
- **闭区间**语义：边界相同也算重叠；不从 ticker/as-of 猜 query.maturity；
- 未提供 → 结果明示 `overlap_filter=not_requested`；记录窗口未知不给"不重叠"结论；
- **不同历史记录之间不做贪心去重**伪称统计独立——F3 的 purge/embargo 与配对另定；`pair_id`/`arm` 字段保留但 F1 不评分，不认为两条不重叠就证明无泄漏。

## 6. 引用与摘要可信边界（契约 #9）

- 本记录 `record_digest` 可复算（加载校验）；
- **外部** thesis/evidence/config digest：未提供原始快照 → `external_unverified`（不声称内容自洽已验证）；提供明确快照则复算并拒绝失配；不得用 `evidence_event_count` 代替证据版本身份；
- CLI 输出含：输入文件 digest、规则版本、as-of 与全部参数、选择/排除清单；不读/不改生产配置或记忆路径。

## 7. 离线验收矩阵（契约 #10，获批后据此写测试）

真实 CLI（正常/全排除/坏 schema/损坏 digest 的退出码与产物）；两次加载与反序输入稳定；同 ID 异内容拒绝；同 run 不同期限共存；publication/observed/version 三种未来各排除；**maturity 未来但 publication 过去仍排除（AND 语义）**；日中 as-of/date 精度/offset 跨日；pending 到期/未到期分别报告；未知费用/收益仍 null；bool/NaN/非法窗口拒绝；闭区间重叠与未请求重叠分别呈现；嵌套输入不变/输出改动不污染下一调用；**生产 memory 源码 hash 不变**。合成 fixture 只证明契约——报告明确不代表预测效果提高。

## 8. 实施清单（E 验收后即刻开工）

1. `tradingagents/evaluation/review_record.py`：schema/校验/规范摘要/深拷贝加载/`retrieve_as_of`/CLI `main()`（argparse，退出码语义）。
2. `tests/fixtures/review_records/*.jsonl`：覆盖 §7 全场景。
3. `tests/test_review_record.py`：§7 矩阵 + CLI 子进程 e2e + 生产 memory hash 不变断言。
4. handoff（hash/测试/限制）→ Codex 独立审计。
