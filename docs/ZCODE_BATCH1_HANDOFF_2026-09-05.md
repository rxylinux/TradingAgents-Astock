# ZCode 第 1 批交接（A15 / A02 / A01 + Codex 补充边界）

日期：2026-09-05 · 分工：ZCode 编码，Codex 方案与逐批独立审核
基线：工作区（HEAD `3f82495` + 第二轮未提交修复），本批改动**未提交**，供 Codex 复核。
审核脚本 `docs/audit_repros_2026_09_05.py` 与 `/tmp/test_codex_batch1_extra.py` 均未改动。

## 变更明细

### A15 — SDK 内置工具显式移除（P1）

- `tradingagents/llm_clients/claude_agent_sdk_client.py::_build_options`：固定
  传 `tools=[]` 显式移除**全部**内置工具（Bash/Read/Write/Edit/…）。
  `allowed_tools` 保留为自动批准列表（原注释"no built-in tools"是错的——
  它不控制工具是否可用；`bypassPermissions` 下未列出的内置工具照常执行）。
  三条路径（纯文本 / 结构化 `output_format` / 投研 MCP 工具循环）都经此构造：
  前两者无任何工具；工具循环节点只能调用 `mcp_servers` 注册的投研 MCP 工具
  （MCP 白名单 `allowed_tools` 与 `max_turns` 逻辑不变，投研工具仍可用）。
- 回归：`tests/test_agent_sdk_provider.py` 新增 3 例（不依赖已安装 SDK：
  mock `ClaudeAgentOptions`/`create_sdk_mcp_server` 只验证实际传入参数）。

**已知限制**：当前环境未安装 claude-agent-sdk，"非授权内置工具实际不可调用、
MCP 实际可用"的子进程级动态验证未执行（同既有 `@requires_sdk` 跳过项）；已
用参数级回归覆盖三路径的 `tools == []` 断言。`permission_mode` 维持
`bypassPermissions`（在 `tools=[]` 前提下无可被批准的内置工具；是否进一步
收紧 permission_mode 留待 Codex 判断）。

### A02 — 通用 backend_url 不再绑定智谱（P1）

- `tradingagents/default_config.py`：`backend_url` 默认改为
  `os.getenv("BACKEND_URL")`（None），不再写死智谱 Coding 端点。
- `tradingagents/llm_clients/openai_client.py`：GLM 专属默认端点
  （`_GLM_CODING_DEFAULT_URL`，智谱 Coding 计划——项目默认模型 glm-5.3/
  glm-5.2 属该套餐）只在 `provider=glm` 且未显式配置 base_url 时生效，
  `GLM_API_BASE_URL` 可覆盖。优先级（各家一致）：显式 base_url（Web 侧栏 /
  CLI / `config["backend_url"]`） > `BACKEND_URL` 环境变量 > 供应商默认
  （glm 另有 `GLM_API_BASE_URL` 插在默认之前）。
- `web/app.py::_build_config` 无需改动：通用默认变 None 后"留空=官方地址"
  的提示即成为真实行为（测试验证）。
- 回归：`tests/test_provider_default_endpoints.py` 14 例——GLM/DeepSeek/
  Ollama 默认端点、OpenAI 官方默认、显式网关覆盖（4 供应商）、
  `GLM_API_BASE_URL` 覆盖、优先级链、DEFAULT_CONFIG 表达式 AST 重求值、
  Web 留空回落 None、`BACKEND_URL` 环境变量仍生效。

**已知限制**：本机用户 `.env` 显式配置了 `BACKEND_URL`（合法优先级最高，
测试中已隔离验证两种情形）；Google/Anthropic/Azure 客户端的端点逻辑未动
（它们不读该默认值路径）。

### A01 — CLI 接入统一运行生命周期（P1）

- `cli/main.py::run_analysis`：
  1. 初始化改经 `graph.prepare_graph_run(ticker, date, callbacks=...)`：
     记忆回填 + 按分析时点注入 past_context + checkpoint 模式下重编译
     （SqliteSaver）+ 断点恢复（`initial_state=None` 从 thread 续跑）。
  2. 流式循环读取共享 `messages` **和全部 `{role}_messages` 分支通道**
     （新增 `iter_analysis_messages`）——R1 隔离后分析师工具消息在分支通道，
     原实现完全看不到。
  3. 收尾用 `graph.finalize_graph_run(...)` 替代裸 `process_signal`：
     写统一 JSON 状态报告（Web 历史识别格式）、写入交易记忆决策、成功后
     清理断点、返回核心信号。
  4. `try/finally` 包住 Live 块：成功与失败路径都执行 `close_graph_run()`
     （释放 checkpointer/SQLite 连接）。
- **Codex 补充边界**（同批处理）：
  - prepare 抛错 / stream 抛错：finally 同样关闭 checkpointer，且失败路径
    不调用 finalize（断点得以保留）——Codex `/tmp` 用例 1、2。
  - 分支通道的工具消息（含 ToolMessage 结果）写入 `message_tool.log` ——
    用例 3。
  - 日志装饰恢复：`save_message_decorator` 等原先**永久**替换全局
    message_buffer 的方法，同进程第二次运行会叠加旧闭包把新标的写进旧标
    的日志。现在备份原始方法、装饰移入 try 内、finally 统一还原——用例 4
    （该用例修复前失败，其余三例在我此前的生命周期修复后已通过）。
- 回归：`tests/test_cli_run_lifecycle.py` 3 例——
  1. `test_cli_crash_then_resume_from_real_sqlite_checkpoint`：**真实
     TradingAgentsGraph + 真实 SQLite**，market+news 两分析师，质量门控
     节点工厂注入崩溃（LangChain 会吞回调异常，回调注入不可用）；断言：
     崩溃后断点保留、崩溃前恰好 2 次 LLM 调用、无决策写入；第二次执行真
     实恢复后断点清理、统一 JSON 含最终决策、记忆有一笔 pending 决策、
     恢复阶段 LLM 调用数 == 完整运行基线 − 2（已完成分析师不重复执行；
     基线对照避免手算 structured-retry 的调用数）。
  2. `test_cli_message_log_includes_branch_channel_tools`：分支通道工具
     调用被 `iter_analysis_messages` 采集。
  3. `test_repeated_runs_do_not_pollute_previous_logs`：两次运行日志互不
     污染、装饰恢复（与 Codex 用例对齐，用最小离线图替身走真实方法签名）。

**已知限制**：`_run_fundflow_report`（资金流向轻量路径）不经图生命周期，
行为不变；CLI 交互 UI（questionary/rich）在集成测试中 mock，键盘交互本身
未自动化；`test_cli_crash_then_resume` 崩溃点选在质量门控（新图合法断点，
不触发 F4 旧断点拒绝路径——该路径已有独立回归）。

## 验证记录

```
本批定向:
  tests/test_cli_run_lifecycle.py + test_provider_default_endpoints.py
    + test_agent_sdk_provider.py + test_cli_default_command.py
    + test_cli_analysts.py + test_memory_log.py + test_graph_parallelism.py
  → 190 passed, 13 skipped（跳过均为未安装的可选依赖 claude-agent-sdk）

全量: 619 passed, 14 skipped, 52 subtests passed, 5 warnings
ruff check tradingagents web cli tests: 通过
git diff --check: 通过

审核脚本（未改动）: 20 failed → 16 failed, 4 passed
  （转绿的 4 项 = A15×2 / A02×1 / A01×1；其余 16 项属第 2-4 批，保留失败）
Codex /tmp/test_codex_batch1_extra.py（未改动）: 4 passed
```

跳过项均为未安装的可选依赖（claude-agent-sdk / google Agent SDK），不能视
为已验证。所有测试离线：MockChat 不联网、崩溃经节点工厂注入、持久化路径全
部指向 tmp。未删除任何真实缓存/断点/记忆，未修改凭据与全局配置，未提交。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `tradingagents/llm_clients/claude_agent_sdk_client.py` | A15：`tools=[]` + 注释纠正 |
| `tradingagents/default_config.py` | A02：通用默认 None |
| `tradingagents/llm_clients/openai_client.py` | A02：GLM 专属 Coding 默认 + env 覆盖 |
| `cli/main.py` | A01：prepare/stream/finalize/close + 分支通道采集 + 装饰恢复 |
| `tests/test_agent_sdk_provider.py` | A15 回归 ×3 |
| `tests/test_provider_default_endpoints.py` | A02 回归 ×14（新文件） |
| `tests/test_cli_run_lifecycle.py` | A01 回归 ×3（新文件） |

## 请求 Codex 审核

1. A15 的 `tools=[]` 语义确认（是否还需叠加 `disallowed_tools` 或收紧
   permission_mode——受 SDK 版本支持范围约束，未装 SDK 无法动态验证）。
2. A02 的 GLM Coding 默认放置位置（openai_client glm 分支 vs 配置层注入）。
3. A01 的装饰恢复策略（finally 还原实例方法）与崩溃注入点的代表性
   （质量门控，可再补辩论/风控阶段崩溃）。
