# ZCode 第 3 批交接（A05 运行隔离 / A06 记忆事务 / A13 东财串行）

日期：2026-09-06 · 分工：ZCode 编码，Codex 方案与逐批独立审核
基线：工作区（含第 1/2 批未提交修复），本批改动**未提交**。
五份独立脚本均未改动（repros / temporal / run_isolation / memory_process /
context_cleanup，后者三例均为 Codex 本批边写边审新增）。

## 变更明细

### A05 — 运行级配置快照（P1）

**`tradingagents/dataflows/config.py`** 重写：

- 运行快照存于 ``ContextVar``：``set_run_config``（深拷贝，嵌套
  ``tool_vendors``/``data_vendors`` 一并隔离，杜绝 dict 共享引发的跨运行
  修改）→ token → ``reset_run_config``。
- **传播依据（实测）**：LangGraph sync invoke 的并行节点在 worker 线程上
  正确读到调用线程的快照、其他线程不受污染（LangGraph/langchain 的
  executor 提交包装了 contextvars）——这使 ContextVar 方案可行。
- ``set_config``：合并语义（部分 dict 不丢周围键）+ **线程局部**（两线程
  各自 set 互不覆盖，修复原始 A05 复现）；``set_default_config``（新）：
  只更新进程默认、无上下文副作用——图构造用它，**构造另一个图不改调用方
  正在进行的运行上下文**（Codex 第 6/7 例断言）。
- ``get_config``：快照优先、返回深拷贝（改返回值不污染活运行）；
  ``run_cache_dir``：区分「运行拥有的目录」与「环境默认」。

**`tradingagents/graph/trading_graph.py`**：

- ``run_context`` / ``bind_run_context`` / ``release_run_context``：运行入
  口线程绑定本图配置快照，覆盖 prepare→invoke→finalize→close 全序列；
  嵌套包裹幂等（内层 reset 回外层快照）。``propagate``/``prepare_graph_run``
  /``finalize_graph_run``/``close_graph_run`` 各自包裹（支持分步调用）。
- ``__init__``：``self.config`` **深拷贝**调用方配置（嵌套 dict 修改不串
  入）；构造对调用方上下文与进程默认**零永久修改**（Codex 最终收口：
  ``set_default_config`` 注册也被移除——它仍会把进程 fallback 改成本图
  配置；运行期由 run_context 显式绑定，构造链路不读 get_config，新增读
  取需求须临时绑定并 finally 恢复）。
- ``_run_graph``：prepare 移入 try（抛错也 close，Codex 退回修正）。

**CLI（`cli/main.py`）/ Web（`web/runner.py`）**：全序列外层绑定快照；
prepare 纳入保护；**token 恰好释放一次**（停止路径与 finally 不重复
release——真实 ContextVar token 二次 reset 会 RuntimeError，Codex 退回修
正）；close 自身出错不得阻止上下文恢复与 CLI 消息装饰还原（finally 内分
层 try/finally）。

**`cache_utils.cached_data`**：

- 缓存身份 = 行为指纹（运行快照的 ``data_cache_dir`` + ``tool_vendors`` +
  ``data_vendors`` 的稳定哈希；不含凭据、不被日志打印）——**同目录不同数
  据源也不互相命中**（Codex 第 6 例：Web 会话共用默认目录）。
- 磁盘按运行快照目录建立独立实例（自定义 ``data_cache_dir`` 真正生效）；
  **无运行快照时沿用全局单例**（直接工具调用与既有测试隔离不变）；
  ``use_disk=False`` 不创建磁盘实例/目录（Codex 退回修正）。

**SDK 双线程桥（`claude_agent_sdk_client._run_async`）**：已有事件循环时
另起的 ``threading.Thread`` **不会自动传播 contextvars**（该跳丢失）；
``asyncio.to_thread`` 本身会传播调用时刻的上下文。修复的是前一跳：
``copy_context()`` 后在新线程内 ``ctx.run(asyncio.run, coro)``，使新
loop 及其 to_thread 工具调用整条链都能读到运行配置（Codex 用例不装 SDK
即可验证）。

Windows 锁说明（限制保留）：file_lock 的 POSIX 分支已在本机实测
（含跨进程受控竞争与符号链接归一）；msvcrt 分支仅静态实现，Windows 实
机未验证。

### A06 — 记忆日志事务（P1）

**`tradingagents/agents/utils/file_lock.py`**（新文件，零依赖）：

- 进程内按**规范化路径**共享 ``threading.Lock`` + 跨进程 advisory 锁
  （POSIX ``fcntl.flock`` / Windows ``msvcrt.locking``），Windows 兼容。
- **无静默降级**（Codex 退回修正）：锁文件创建/打开失败直接抛原异常，
  事务在无 OS 锁状态下绝不继续；仅竞争性忙（POSIX EWOULDBLOCK/EAGAIN；
  Windows 在 fd 已可写打开时的 EACCES）按重试处理，其他底层错误透传。
- **路径归一**（Codex 第二次退回修正）：``resolve()`` 解析符号链接/相对
  路径，锁文件名取 **resolved** 的 name——real.md 与其别名共用一把锁
  （首版用别名 name 造成两把锁）。

**`tradingagents/agents/utils/memory.py`**：

- ``_log_path`` 在构造时 ``resolve()``：读写经符号链接直达**真实文件**，
  原子 ``os.replace`` 不会把符号链接替换成独立新文件（Codex 退回修正）。
- 所有读写在 ``_transaction()``（file_lock）内：追加（幂等检查+追加同事
  务 + fsync）、单条/批量回填（读-改-写-轮转全事务，提交时仍为 pending
  才结算）、读取（读者见不到部分追加）。公共入口恰好加锁一次、内部用
  ``_*_unlocked`` helper——公共方法间不嵌套调用（防自锁死锁）。
- 临时文件名含 pid+tid（并发写者互不覆盖）；反思（LLM）保持在锁外
  （``_resolve_pending_entries`` 先反思后批量提交，原有结构已满足）。

### A13 — 东财串行限流（P2）

``_em_get``：「检查间隔 → 等待 → 请求 → 更新时间戳」全程在
``_EM_THROTTLE_LOCK`` 内；时钟改 ``time.monotonic()``（不受系统回拨影
响）；``try/finally`` 保证成功/超时/异常都释放锁。只约束东财数据源临界
区，未触碰 LangGraph 节点并发。

## 测试与验证

新增守护回归 ``tests/test_run_isolation_and_locks.py``（13 例，默认收
集）：set_config 线程局部、快照深拷贝、get_config 防污染、无快照回退、
同目录不同供应商不共享缓存、use_disk=False 零磁盘副作用、受控交错下追
加不丢失、重复决策幂等、双回填只结算一次、锁打开失败透传、符号链接单锁
+ 跟随真实文件、东财串行化。

``tests/conftest.py``（隔离接缝更新，Codex 要求）：autouse 清理
``_run_config`` ContextVar 快照 **与 ``_SCOPE_DISK_CACHES`` 注册表**——
防止上一用例的作用域磁盘实例（可能指向其 tmp 或真实目录）被后续用例复
用；未删除任何缓存命中/重试断言。

``tests/`` 既有测试的接缝适配（接口重构，业务断言不变）：4 处使用
MagicMock/桩图直接调 ``prepare_graph_run`` 的测试，补挂
``run_context``（nullcontext）与 ``_prepare_graph_run``/``_finalize_graph_
run``（MethodType/partial 指回真实实现）——否则整段逻辑被 MagicMock 自动
属性吞掉（返回值不可解包）；``_OfflineGraph`` 桩补 ``bind/release_run_
context``；``test_update_atomic_write`` 断言适配唯一临时文件名（原断言基
于固定 ``.tmp`` 会被覆盖的旧行为）。

```
全量: 657 passed, 14 skipped, 52 subtests passed, 5 warnings
五份独立审核: 44 passed / 6 failed（A09/A10/A11/A12/A14×2，全部属第 4 批，保留失败）
  — 本批相关: repros A05/A06/A13、run_isolation 7/7、memory_process 4/4、
    context_cleanup 3/3 全部通过
ruff check tradingagents web cli tests: 通过
git diff --check: 通过
```

## 过程中的失败证据（如实记录）

1. **file_lock 两轮退回**：首版在 EACCES/EPERM/EROFS 时 ``fd=None`` 继续
   yield（静默降级无锁事务）→ 改为直接抛出；第二轮发现锁文件名用了别名
   的 name（resolve 后又拼回 ``Path(target).name``）→ real.md 与 alias.md
   两把锁，``_log_path`` 未 resolve 导致 replace 替换符号链接——两者均已
   修正并有审核用例。
2. **context_cleanup 三例退回**：Web prepare 在 try 外（抛错残留他人配置
   且不 close）、停止路径与 finally 双 release 同一 token（真实
   ContextVar 二次 reset 抛 RuntimeError）、``_run_graph`` 的 prepare 同样
   在保护外——均已按「prepare 入保护 + token 恰好一次释放 + close 失败
   不阻恢复」修正。
3. **缓存作用域两轮演进**：首版 scope 只有目录 hash（同目录不同 vendors
   共享，Codex 实测返回 vendor_a）→ 指纹升级为目录+供应商；同时发现
   ``_DISK_CACHE`` 测试替身不覆盖 per-dir 注册表（可能写真实目录）→
   conftest 增加注册表清理 + ``use_disk=False`` 零磁盘实例。
4. **图构造污染上下文**（Codex 第 7 例，两轮）：``set_config`` 合并进当
   前快照使「构造另一个图」改掉调用方正在进行的运行 → 第一轮拆出
   ``set_default_config``（仅进程默认）；Codex 再验指出进程 fallback 仍
   被构造永久改写 → 最终移除构造期全部配置注册，实现零永久修改（复刻
   验证：调用方快照保持 5，进程默认不再变 90）。
5. 全量途中曾出现 6 个测试失败，全部为 mock/桩图缺少新生命周期接缝
   （上述适配），非生产缺陷；适配后全量绿。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `tradingagents/dataflows/config.py` | 重写：ContextVar 快照/深拷贝/合并/set_default_config |
| `tradingagents/dataflows/cache_utils.py` | 行为指纹作用域 + per-dir 磁盘 + use_disk=False 零实例 |
| `tradingagents/dataflows/a_stock.py` | A13：限流锁 + 单调时钟 |
| `tradingagents/graph/trading_graph.py` | run_context/bind/release、图配置深拷贝、构造去污染、prepare 入保护 |
| `tradingagents/agents/utils/file_lock.py` | 新文件：跨进程锁（无降级、错误分类、路径归一） |
| `tradingagents/agents/utils/memory.py` | 事务化读写、resolve 真实文件、唯一临时文件、提交核对 |
| `tradingagents/llm_clients/claude_agent_sdk_client.py` | `_run_async` 双线程桥 ctx.run |
| `cli/main.py` / `web/runner.py` | 全序列快照 + token 单次释放 + 分层恢复 |
| `tests/conftest.py` | autouse：快照 + 磁盘注册表清理 |
| `tests/test_run_isolation_and_locks.py` | 本批守护回归 ×13（新文件） |
| 4 个既有测试文件 | mock 图生命周期接缝适配（断言不变） |

## 请求 Codex 审核

1. ``set_config`` 的合并语义（部分 dict 合入当前快照）是否符合预期用途
   ——它与 ``set_run_config``（整体快照）并存，前者服务直接调用方/线程
   局部覆盖，后者服务运行入口。
2. 缓存行为指纹当前取 data_cache_dir + tool_vendors + data_vendors；
   ``output_language``/``market_lookback_days`` 等不改变**取数结果**的配
   置未纳入（同配置不同语言共享缓存是预期——缓存的是原始数据）。
3. file_lock 在 Windows 上仅静态实现（fcntl 分支为主）；本环境无法运
   行 Windows 实测，msvcrt 分支请按需复核。
