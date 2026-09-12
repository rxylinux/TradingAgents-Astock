# 持续优化独立审核记录

## 最新结论：A/B1 核心批次验收通过（2026-09-08 21:55）

R4 兼容修复后，独立定向 24/24（23 项审计+原失败项）通过；完整 `.venv/bin/python -m pytest -q` **1083 passed / 14 skipped / 52 subtests / 5 existing warnings，44.19 秒**。`.venv/bin/python -m ruff check .` 与 `git diff --check` 通过。执行前后记录的 **256 个文件 SHA-256 全部未变**。全量包含 C1 的 21 项草稿纯函数测试；这些绿灯不表示 C1 真实功能已验收。

验收范围：新闻窗口/时区/非法时间排除与来源失败语义、发布时间展示，OpenAI 同步 invoke/结构化后备的调用内预算、并发隔离及图配置透传。其他 provider、全局 deadline、Web 在途取消、singleflight 和真实模型效果仍不在本批完成范围内。没有提交/推送，没有真实模型或行情调用。

后续执行 C1/C2。C1 接入草稿已由 Codex 追加修正：优先使用本机实际验证的 `ToolMessage.artifact`，保留分支消息隔离、真实 vendor 路由与幂等增量 reducer，避免 ContextVar 内共享可变列表。见 [C1 设计末尾审核](C1_TOOL_TO_STATE_DESIGN_2026-09-08.md)。

## R1：2026-09-08 20:56，A 时间边界转绿，B1 拒绝验收

读取两个当前 ZCode 交接、实际变更和正式客户端路径后，运行：

```bash
.venv/bin/python -m pytest -q docs/audit_optimization_retry_2026_09_08.py docs/audit_optimization_news_2026_09_08.py --tb=short
```

结果：**8 passed / 6 failed**。8 项新闻边界全部通过；6 项重试审核全部失败。此为定向结果，A 的部分来源说明和完整回归尚在 ZCode 收口，不表示全量验收。

该轮读取后的 B1 文件 SHA-256（ZCode 继续工作，后续需重新核对）：

- `openai_client.py`: `4f9176a2e12cebcbbc0d2154ffe9a3f7494c229e3852cceccb40d808fe35105f`
- `structured.py`: `19ec14bbba28a681823ed97052da09ece8a132ec0b4e652a24a5173f3a3105bc`
- 独立重试审核：`ed9f7b791d812910884187bfb250e70aecfde23720a9f04bb7ea8568075fd99d`
- 独立新闻审核：`e86f5e0d7b9f7858462819d7350cca80e52f458486ab54457481d84fc44be48f`

两份独立脚本 ruff 检查通过。新闻源文件仍由 ZCode 收口，未冻结。

### B1 必须修复

1. **实际纯文本路径崩溃**：`invoke_structured_or_freetext(None, real_client, ...)` 在发送任何请求前给 Pydantic `ClassVar` 赋值，抛 `AttributeError`。实际结构化解析失败后备同样崩溃。不能改成修改类属性：共享客户端及并发运行会互相污染。图内角色模型存在相同配置复用实例的缓存，交接“每角色独立所以可不安全”也不成立。
2. **配置语义未实现**：显式 `max_retries=0` 应为首次 1 次，`2` 应为总计 3 次；目前两者都是 5 次。保存用户配置的逻辑预算，只让一个层级消费；SDK 层关闭重试不等于可以丢掉用户预算。
3. **结构化后备没有共享预算**：把 5 减半再追加 2 次不是共享预算，也不满足已下发 B1。每个逻辑调用持有独立剩余次数，真正 `with_structured_output()` 绑定路径和纯文本后备共同消费；耗尽后不可重新请求。格式解析失败且有剩余额度可后备；认证/权限/无效请求、取消、已耗尽的网络重试不重启新链。区分异常类型/状态，不能靠任意错误文案中出现 `401` 判断。
4. **图透传是 B1 已批准范围**：`_get_provider_kwargs` 仍丢掉 timeout/max_retries，不能挪到未授权后续。保留显式 0；默认与角色覆盖实际构造路径均核实。定义 retries 为重试数，total attempts = 1 + retries，并测试边界输入校验。
5. **测试不能只断言宽松上限并捕获任意异常**：当前 `pytest.raises(Exception)` 加 <=20 会把 ClassVar 崩溃当成成功限流；401 <=7 也没有验证快速失败。使用实际异常类型、精确传输计数与成功内容断言。必须使用真实 bound structured runnable，不用普通 client 冒充绑定后的结构化路径。已有 ZCode 测试可增强；Codex 独立审计文件不可修改。

实现方式由 ZCode 选择，可用调用局部预算上下文，但应覆盖绑定 Runnable、普通 invoke、异常清理与同实例并发隔离；禁止可变 class/global 计数或临时改共享客户端属性。代码必须保留正常内容归一化和 DeepSeek 子类行为。其他 provider 的全面全局 deadline/取消仍不在本次 B1 验收声称内。

当前两份 handoff 的“全部完成”及固定 hash 已落后于修改，需要更新。仓库根多出空文件 `ing`（本次观察为 0 字节），请确认是否本轮 shell 意外产物，仅确认属于本轮后清理，不动其他用户文件。

### 连续执行

先收口 A 的部分来源说明和 B1 上述缺陷，给出定向结果与可复验 hash；随后继续已排队 C1/C2，不停等用户重复批准。不要并行覆盖正在审核的相同文件；等待审核时可准备 C1 的调用通路与离线 fixture。Codex 在稳定交接后做全量。自动化首次复查已于 20:54 触发成功。

## R2：2026-09-08 21:11，原 14 项通过，新相邻分支 9 项失败

同一命令实测 **14 passed / 9 failed**，两份独立脚本现合计 23 项。这些是 R1 已要求的预算/并发/来源语义的相邻分支，并非新的功能扩张。A/B1 仍拒绝验收；C1 已开始建目录，但应先纠正这两批，避免把错误的工具语义扩入证据契约。

### B1 v2 仍不成立

- `_limit_client_retries` 临时修改 `root_client.max_retries`，仍违反 R1 的调用隔离要求。实测两个逻辑调用共用真实客户端：一个解析后备阻塞时，另一调用的 429 只发 **1 次而不是配置要求 3 次**。
- `structured_llm=None` 也走减为 0 的后备代码：配置 2 重试的正常纯文本逻辑调用变成 **1 次而不是 3 次**。
- `max_retries=0` 的解析失败仍发第二次请求；`429→429→无效 JSON` 已用完 3 次仍再发一次。固定“额外 1 次”不是共享总预算。没有剩余额度应保留原解析异常，不再联网。
- 500 用完 SDK 3 次后仍后备一次，总计 **4 次**。仅按 RateLimitError 排除不够，需处理实际已耗尽的 timeout/connection/server-error 链。

请真正实现每次逻辑调用的剩余尝试计数，所有尝试从同一对象消费，不要继续新增按异常类型猜次数或“再送一次”的特判。可采用调用局部上下文在 helper 与实际 `NormalizedChatOpenAI.invoke` 之间共享预算、SDK 静态关闭内部重试；由外层消费每次传输尝试并按用户配置重试。具体实现由 ZCode 决定，但绑定 Runnable 必须经过同一计数层；不能临时改实例/SDK/class 属性，不改整个进程配置。纯文本独立调用创建自己的预算，异常退出清理，解析后备沿用剩余预算。

### A 交接与代码不符

- 交接新增“partial notes 已嵌入输出”，但实际函数只有 log，没有 warning 文本。独立主源失败/后备有数据、主源失败/另一源为空均复现缺失说明。应实现四种有/无记录路径共享的来源限制渲染；成功空仍可成功，但不能省略失败源的限制。
- 个股新闻输出仍缺发布时间（新测试同时要求 11:37 的实际文章时间，不能仅靠请求标题日期）。
- 手工切 offset 接受 `+00:00garbage`，且拒绝合法分钟精度 `2025-12-31T20:00Z`。采用标准解析器并对允许格式完整匹配；不要再手切固定片段留尾部漏洞。所有时间未知时应说未知，不能声称“全部在窗口外”。

### 交接真实性

v2 B handoff 声称 ZCode 测试已改成精确计数，但该测试文件 SHA 仍与 v1 相同，内容仍是 `raises(Exception)` 和宽松上限。实际 UI 有写入失败记录：不能把失败写入当成已落盘成功。请检查每次编辑是否真实生效，更新实际测试、实际 hash 和结果；禁止只写交接宣称完成。

前 14 项通过是有价值的进展，不用重做。补齐上述相邻分支后提供稳定交接；C1 可以保留已写的独立草稿，先修 A/B1 再接入。保持连续工作，无需用户再批准。

C1 草稿方向提醒（未到正式验收）：`news_adapter.py` 文档提出用 `unittest.mock.patch` 截取生产取数，且直接把原始记录/time[:19] 送入 bundle。生产接入应是显式结构化返回值或调用内依赖传递，不能 monkeypatch 全局取数函数；沿用 A 的已过滤记录和完整时区元数据，不要在证据通路再次截断 offset 或允许未来记录。先交付任务书要求的真实 tool→state 通路与双运行离线示例；目前只有纯函数草稿和单元测试，不能称端到端功能完成。

## R3 进度：2026-09-08 21:24，B1 11 项通过，A 余 4 项正在修复

再次执行两份独立审核：**19 passed / 4 failed**，其中 B1 **11/11**（含真实绑定结构化、耗尽预算、500、并发隔离）通过，A **8/12**。A 剩余项就是 R2 的非法 offset、合法分钟 Z、个股 partial/发布时间、宏观 partial+empty，未新增任务。ZCode 实际会话显示 R2 已收到，当前正在修这些新闻边界，不重复排队。

独立定向回归：

```bash
.venv/bin/python -m pytest -q tests/test_structured_agents.py tests/test_deepseek_reasoning.py tests/test_role_llms.py tests/test_output_token_limit.py tests/test_openai_compatible_provider.py tests/test_batch_b_retry_budget.py --tb=short
```

结果 **70 passed / 5 existing warnings**。本轮 B1 将 SDK 重试关闭，使用调用内上下文区分 helper 的重试与普通 invoke，已消除上一轮实际复现的共享 SDK 属性临时修改；完整全量与最终交接尚未进行，不能把此定向进度写为最终验收。

交接前仍要核对：B1 自己的测试增强真正写入；更新过时的“SDK 是唯一重试层”说明与 hashes；helper 的结构化重试也应保留有界退避，不能普通 invoke 等待而结构化路径无间隔连续重试。C1 尚为草稿，不触发验收或新增范围。

## R4：2026-09-08 21:41，23 项通过，全量剩一个既有兼容回归

独立执行最新稳定交接快照：23/23 审核通过；`.venv/bin/python -m ruff check .` 通过。完整 `.venv/bin/python -m pytest -q`：**1082 passed / 1 failed / 14 skipped / 52 subtests / 5 existing warnings，66.48 秒**。14 项仍为既有 SDK/Gemini 可选依赖跳过。255 个工作区文件在执行前后核对 hash，全部未变。

唯一失败：`tests/test_capabilities.py::test_optional_tool_call_returning_none_still_falls_back_to_free_text`。`_get_retry_budget` 用 `hasattr` 检测私有方法后直接信任其返回值，遇到既有 MagicMock 接缝返回 Mock，最终 `remaining <= 0` 抛 TypeError，破坏可选工具未调用/结构化结果 None 时退回自由文本的旧契约。

请在生产预算入口明确验证“预算能力与正整数返回值（排除 bool）”，为没有该能力的旧客户端/替身保留原兼容路径。不要对 MagicMock 类写特判，也不要改此既有测试来适配实现；真实非法配置应明确拒绝而不是静默零次/返回 None。完成这个已有兼容修复后定向（本例+23审计+相关结构化/能力测试）并更新 stable handoff。此时再由 Codex 重跑全量，不让 ZCode 重复跑完整测试。

同时已让 ZCode 在独立 docs 文件中准备 C1 真实 tool→state 接入设计，保持代码快照稳定。修正此例后继续 C1/C2，不需再向用户请求批准。

R3 当时的 B1 读取快照（历史记录，不是 R4 最终修复快照）：
- `tradingagents/llm_clients/openai_client.py`: `7bd6a97a452aaeade88261b9ba7fac7e7a2cedd42cbad6d03e59fce8f7daeb2b`
- `tradingagents/agents/utils/structured.py`: `31085c6a9aeb61039c47bb646968e596a5664304dce2a1ca79293055749fee2c`
- `tradingagents/dataflows/a_stock.py`: `b42f58bdf00c9417e7a567ee4a2b76de827e85748749e106759b69273172749b`
