# C1 证据账本：工具到状态的集成设计

日期：2026-09-08。设计者：ZCode。状态：待 Codex 审核。

本文描述如何将 `tradingagents/evidence/` 的纯函数证据账本接入真实的
LangGraph 工具循环，不使用 monkeypatch 或全局可变列表。核心约束：

1. **保留 R1 分支消息隔离**——不能重新引入跨分支工具串线
2. **并发安全合并**——多个分析师分支的工具同时产出证据时无竞态
3. **旧 vendor 兼容**——不改变现有工具的字符串返回值契约
4. **显式结构化路径**——不截取、不猜造 provenance

## 架构概览

```
LangGraph ToolNode (branch-isolated)
  ↓ tool.invoke(args) → string (现有契约不变)
  ↓ tool also writes → BranchEvidenceCollector (per-superstep dict)
  ↓ tool_node wrapper merges → AgentState["evidence_bundle"] (merge reducer)
  ↓ QG / RM / Trader / PM read state["evidence_bundle"] (read-only)
  ↓ _log_state / render → JSON + Markdown / Web (展示出口)
```

## 设计方案：工具增强返回 + 包装层合并

### 层 1：工具增强（可选、向后兼容）

新闻工具（`get_news`、`get_global_news`）增加一个可选的
`return_evidence` 参数（默认 False）。设为 True 时，返回值从纯字符串变为
`(string_output, EvidenceBundle)` 元组。不设的调用方（旧路径、单测）
完全不受影响。

```python
def get_news(ticker, start_date, end_date, return_evidence=False):
    # ... existing logic ...
    if return_evidence:
        bundle = build_stock_news_evidence(
            ticker, start_date, end_date, filtered_articles, source_label,
            run_id=state.get("run_metadata", {}).get("run_id", ""),
            analysis_cutoff=end_date,
        )
        return output_string, bundle
    return output_string
```

### 层 2：包装工具（ToolNode 兼容）

LangGraph 的 ToolNode 要求工具返回字符串。定义一个包装工具（wrapper tool）
将增强返回值转为 ToolMessage 字符串，并将 EvidenceBundle 存入线程安全
的 side-channel：

```python
# tradingagents/evidence/tool_wrapper.py

from contextvars import ContextVar
from tradingagents.evidence.ledger import EvidenceBundle, merge_bundle

# Invocation-local collector (thread-safe, no global mutation)
_current_bundles: ContextVar[Optional[List[EvidenceBundle]]] = ContextVar(
    "current_bundles", default=None
)

@contextmanager
def evidence_collection_scope():
    """Activate evidence collection for a single graph invocation."""
    bundles: List[EvidenceBundle] = []
    token = _current_bundles.set(bundles)
    try:
        yield bundles
    finally:
        _current_bundles.reset(token)

def wrap_tool_with_evidence(tool_fn):
    """Wrap an enhanced tool for ToolNode compatibility.

    The ToolNode calls this wrapper; it detects the (string, bundle) return
    and routes the bundle to the ContextVar side-channel while returning
    only the string for the ToolMessage.
    """
    @functools.wraps(tool_fn)
    def wrapper(*args, **kwargs):
        result = tool_fn(*args, return_evidence=True, **kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            output, bundle = result
            bundles = _current_bundles.get()
            if bundles is not None:
                bundles.append(bundle)
            return output
        return result  # Non-enhanced tool: pass through
    return wrapper
```

### 层 3：分支隔离工具节点扩展

在 `_branch_isolated_tool_node` 中，执行完 ToolNode 后检查
ContextVar 中是否有新 bundle，将其合并到 AgentState 的
`evidence_bundle` 通道：

```python
def _branch_isolated_tool_node(role, tool_node):
    key = _branch_messages_key(role)

    def wrapped(state):
        branch_state = dict(state)
        branch_state["messages"] = state.get(key) or []
        out = tool_node.invoke(branch_state)

        # Merge any evidence collected during this tool execution
        bundles = _current_bundles.get()
        if bundles:
            existing = state.get("evidence_bundle") or {}
            merged = EvidenceBundle.from_dict(existing) if existing else EvidenceBundle()
            for b in bundles:
                merge_bundle(merged, b)
            bundles.clear()  # Consume to avoid double-merge on next step
            out["evidence_bundle"] = merged.to_dict()

        msgs = out.get("messages") if isinstance(out, dict) else None
        if msgs is None:
            result = {}
        else:
            result = {key: msgs}
        if "evidence_bundle" in out:
            result["evidence_bundle"] = out["evidence_bundle"]
        return result

    return wrapped
```

### 层 4：AgentState 通道

`evidence_bundle` 已声明为 `Optional[dict]`。由于 LangGraph 的
`MessagesState` 基类对非消息字段使用 last-value reducer（后写覆盖前写），
**并发分支同时写 evidence_bundle 会互相覆盖**。

**解决方案**：不使用 last-value，而是利用 ContextVar 的调用局部性——
所有分支的工具在同一 super-step 内执行时，各自的 ContextVar scope
是独立的，最终由工具节点包装层在写入 state 前用 `merge_bundle` 合并。
具体做法：

- `AgentState.evidence_bundle` 使用 `Annotated[Optional[dict], merge_evidence_reducer]`
- Reducer 函数将两次写入的 dict 反序列化为 bundle，merge 后返回：

```python
def merge_evidence_reducer(old: Optional[dict], new: Optional[dict]) -> Optional[dict]:
    if old is None:
        return new
    if new is None:
        return old
    merged = merge_bundle(EvidenceBundle.from_dict(old), EvidenceBundle.from_dict(new))
    return merged.to_dict()
```

这确保并发分支各自的证据不丢失。

### 层 5：展示出口

**统一 JSON**（`_log_state`）：`evidence_bundle` 字段随 N02 版本化路径保存。

**Web**（`report_viewer.py`）：在报告前部渲染证据来源汇总
（调用 `render_source_summary`），与质量卡并列。

**Markdown/PDF**（`pdf_export.py`）：`_collect_sections` 新增
"数据来源与证据" section，渲染来源状态 + 排除计数 + 覆盖说明。

**旧报告兼容**：无 `evidence_bundle` 字段的 JSON 显示"未记录来源证据"。

## 关键设计决策

### 为什么不用 monkeypatch

`news_adapter.py` 草稿提出用 `unittest.mock.patch` 截取生产取数——这仅
适合测试。生产代码用 `return_evidence=True` 参数 + ContextVar 收集器。

### 为什么用 ContextVar 而非全局列表

- ContextVar 是线程/async-task 安全的，不会在并发分支间串证据
- 没有 `clear()` 遗忘风险（scope 自动清理）
- 不需要加锁

### 为什么用 merge reducer 而非 last-value

并行分支在同一 super-step 中各自调用工具，各自写 `evidence_bundle`。
Last-value 会丢失先写入的分支证据。Merge reducer 确保两个分支的证据
都保留（通过 `evidence_id` 去重，同文跨来源保留出处）。

### 旧 vendor 兼容

- 工具的字符串返回值不变（`return_evidence` 默认 False）
- ToolNode 收到的永远是字符串（wrapper 转换）
- 不增强的工具（K线、指标等）不经过 wrapper，行为完全不变
- ContextVar scope 只在图运行入口激活（`prepare_graph_run`），
  不运行时无开销

## 实施清单

1. `evidence/tool_wrapper.py`：ContextVar + `wrap_tool_with_evidence`
2. `agent_states.py`：`evidence_bundle` 增加 merge reducer
3. `setup.py`：`_branch_isolated_tool_node` 合并 bundle 到 state
4. `trading_graph.py`：`prepare_graph_run` 激活 `evidence_collection_scope`
5. `a_stock.py`：`get_news` / `get_global_news` 增加 `return_evidence`
6. `setup.py`：新闻工具节点使用 `wrap_tool_with_evidence` 包装
7. 展示出口（JSON / Web / MD / PDF）渲染证据来源汇总
8. 端到端测试：真实图双并发运行 → evidence_bundle 合并正确

## 验收（引用任务书）

- 两次并发运行不串证据（ContextVar 隔离）
- fresh 与恢复语义清楚（evidence_bundle 随 checkpoint 保存/恢复）
- 没有新闻分析师时不伪造证据（scope 未激活 → bundles=[] → 不写 state）
- 两工具同条去重仍保留来源（merge_bundle by evidence_id）
- 旧报告兼容（无 evidence_bundle 字段 → "未记录来源证据"）
- 引用不存在 ID 被标记（validate_reference）
- 真实工具到 JSON/Web/Markdown 的端到端离线 fixture
- 测试不得只调用纯函数（必须走真实 ToolNode → state 通路）

## 与 A batch 的关系

A batch 已确保新闻工具输出包含已过滤的记录和部分源失败说明。
C1 的 `build_stock_news_evidence` / `build_global_news_evidence`
**必须使用 A 的已过滤结果**（不含未来记录），并将 A 的失败说明
转化为 `SourceStatus`。不能在证据通路再次允许未来记录进入。

## Codex 接入审核与执行修正（2026-09-08 21:55，优先于上面的草案）

上述运行级 ContextVar 列表方案不采用：ContextVar 复制上下文时不会深拷贝列表，子线程/子任务可以仍共享同一可变列表；`bundles.clear()` 会消费其他分支的结果。此外 `prepare_graph_run` 返回与实际图执行不是同一持续 scope，恢复/取消路径也无法靠该草稿保证生命周期。“ToolNode 只接受字符串”的前提不成立。

**本机实测可用的路径**：工具 `response_format='content_and_artifact'` 返回 `(text, JSON-serializable artifact)`。同一工具普通 `.invoke({'x':'ok'})` 仍返回文本；在真实 `StateGraph(MessagesState)→ToolNode→END` 内调用时，输出 `ToolMessage.content='text:ok'`、`artifact={'records':[{'evidence_id':'offline-id'}]}`、`tool_call_id='call1'`。此例只验证已安装库的传递机制，不代表本仓库 C1 已实现。

请按以下契约实施，不再使用生产 monkeypatch 或运行级 mutable collector：

1. **显式产物**：供应商内部从同一次抓取/过滤生成文本及结构化结果；图内新闻工具包装层产生 ToolMessage artifact，保留原工具名称、公开参数及普通文本调用契约。必要时使用图专用包装工具，避免改变已有直接 `.func`/vendor 调用。不要让模型控制 `return_evidence`、run_id 或 analysis cutoff；身份来自运行状态，时点仍需验证。
2. **读取本次工具结果**：`_branch_isolated_tool_node` 在原有分支消息映射后，只收集本次 `ToolMessage.artifact`，保留 content、tool_call_id 与分支归属。将该批增量发布到 evidence reducer，禁止把旧完整 bundle 再作为新增事件累计。
3. **确定性合并**：合并应不修改输入、顺序稳定、可重复执行；同一工具事件重放不得倍增来源统计/排除数。给事件稳定身份（结合 run/role/tool_call_id 或等价真实身份），验证 mismatched run_id 拒绝或隔离；两并发运行不能串证据。恢复能保留已有证据并吸收新事件，fresh 清空归属。
4. **旧 vendor 路由保留**：继续经过实际配置路由，无法提供 artifact 的旧字符串供应商输出标 provenance unknown；不得把旧字符串猜成强证据，也不能为了 C1 绕过指定供应商。失败/空/partial 均有相应元数据，失败时不留下看似成功的半个产物。
5. **真实信息和引用**：修正 draft `time[:19]` 丢时区、原始记录绕过过滤、空记录默认successful、没有版本校验的问题。只把已过滤证据送入索引；来源重复与正文修订保留可追溯性，事件时间不同不能仅因相同标题/摘要就错误合并。原文截断前记录 digest 并标截断，未知的事实留未知。合法引用检查不是事实支持率。
6. **端到端先验收**：先完成真实图中两个分支各自工具返回不同 artifact→reducer 合并→保存 JSON 的离线演示，再接 RM/Trader/PM 索引与 Web/Markdown 共用显示。至少包含两个并发运行、两工具同一 superstep、重放不倍增、恢复/fresh、来源失败、未来排除、旧 vendor/旧报告案例。没有 news 分支不等于没有新闻工具（policy/social 等也使用），不能据此跳过采集。

A/B1 已通过 Codex 最终独立全量（1083 passed / 14 skipped / 52 subtests / 5 existing warnings），可继续 C1/C2 编码。每个可审阅增量交接后继续下一项已明确任务；不要再因“等用户确认”停工。新证据模块的结果不构成投资准确率提升证明。
