# G1：财报原始快照契约（ZCode 首批编码）

日期：2026-09-10。依赖总计划 `NEXT_PHASE_IMPLEMENTATION_PLAN_2026-09-10.md`。本批只交付真实请求边界与可复验快照，不修改 D1 财务公式，不接入生产默认分析，不自动运行模型。

## 用户可见结果

用户显式抓取沪深 A 股三张财报，得到一组不可覆盖的来源快照及抓取诊断；之后断网仍能校验并查看这次拿到的确切数据。来源内容是否完整、是否含披露时间、是否官方原公告分别说明。**快照存在不意味着满足 D1 面板输入条件。**

## 代码落点与兼容策略

- 新增 `tradingagents/dataflows/financial_snapshot.py`，允许拆独立 store/provider 模块；尽量使用现有 requests 与 stdlib，不安装依赖。
- 复用 `a_stock.py` 已观察到的 Sina 端点、参数、错误语义与 ticker 规范化。首版明确仅沪深六位 A 股，北交所/指数/港美股返回 unsupported，不能用当前 helper 的 sh/sz 默认逻辑误路由。
- 读取现有 `_get_financial_report_sina`、`robust_api_call` 和相关 temporal/retry 测试。若抽取共享底层请求，旧函数输出、异常、缓存与历史过滤保持既有测试；新快照拿的是**过滤/排序/head(8) 之前**的同次响应，不从 DataFrame 反推原始内容。
- 底层传输可注入 fake session/transport；时间与 store 可注入，公开 API 仍验证参数，不能只靠 CLI 校验。网络与文件内容不能发到模型。

建议公共接口（ZCode 可按仓库风格调整名称，语义不可省略）：

```python
fetch_financial_snapshot(instrument, report_type, *, transport, clock,
                         request_policy) -> CapturedResponse
save_snapshot(captured_response, *, output_root) -> SnapshotManifest
load_snapshot(manifest_path) -> VerifiedSnapshot
```

## 三类产物与身份

1. `raw/<body_sha256>.bin`：应用层 HTTP body 原始字节（明确是 requests 可见、解压后的 body，不声称 TLS/传输压缩原包），在 JSON 解析和筛选之前计算摘要。不落 request headers、cookies、代理 URL 或认证 query。首版只支持固定公共 GET 端点与明确参数白名单。
2. `snapshots/<capture_id>/manifest.json`：schema_version（int 非 bool）、provider、instrument、instrument_type、report_type、endpoint、白名单请求参数、HTTP 状态、UTC aware 抓取起止、body 大小/摘要/相对路径、parser_version、状态、覆盖范围与限制、manifest_digest。capture_id 是一次实际抓取身份；内容重复可以共享 raw body，但抓取时间与事件不能覆盖。
3. `snapshots/<capture_id>/records.json`：从原 body 解析出的来源记录和每条记录的 JSON Pointer；保留原字段、行顺序、源披露/修订原值，不填业务默认值。不持久化被猜造的标准化财务指标。文件摘要纳入 manifest 身份。

最终 manifest_digest 必须在全部持久字段确定后计算；规范化 JSON 排序键/编码/数字规则固定，不包含自身摘要。读回重新校验全部摘要、schema、标的/表类型与来源路径，不只验证 manifest 自身。重复写同 capture_id 内容相同幂等、不同内容明确冲突，不能 last-write-wins。

## 状态与时间语义

- 捕获结果至少区分 ok/empty/partial/failed；unsupported/invalid_input 在请求前拒绝。HTTP 失败、业务错误、坏 JSON、缺预期 data/table 结构不能冒充 empty。
- 合法 table=[] 才是 empty；schema 合法但分页/截断/覆盖不足为 partial。当前源参数只取有限条记录，不能据此宣称拿到完整历史；保存 requested_limit/returned_count 和已知／未知分页信息。
- 抓取时点来自应用可信 clock，源字段单独保存；不得把抓取日或报告期当披露日。来源时间缺失可保存原快照并标 unknown，不能因此虚构可以生成有效面板。
- 本批只记录版本，不选择“历史正确版本”。今天抓取到旧报告不证明过去可得；availability 固定说明 observed_at_capture，来源披露字段仅 source_declared。不得由导入文件自述把状态提升到“外部真实历史已验证”。
- import/离线 fixture 记录 origin=imported/synthetic；由传输抓到的标 origin=captured。摘要验证只叫 integrity_verified，避免与内容真实性混淆。

## 资源、存储与失败处理

- 三个 report_type 为白名单；一次 CLI 最多抓 3 表，每表默认最多 3 次总 HTTP 尝试，包含所有重试；不叠加 SDK/robust_api_call 预算，429/5xx/超时可重试，其他 4xx、业务结构错误不自动重试。
- 单请求 timeout 默认 15 秒，单表总 deadline 默认 45 秒；总重试等待计入 deadline。返回每次尝试状态和实际请求数，重试上限为受控配置，不接受 bool/负数/非有限值。
- 单响应默认上限 10 MiB，按读取过程限制，超限返回明确错误，不无限读取后才检查。存档大小、记录数与解析嵌套设合理上限并文档化；不添加通用下载器。
- 显式 output_root；路径由程序构造，不用源字符串拼文件名。读回拒绝绝对路径、路径穿越与逃逸目录的符号链接；不读 manifest 指向的任意外部文件。
- 写入采用同目录临时文件与原子发布，manifest 最后写；途中失败不可出现“有效 manifest + 缺 body”。并发不同 capture 相互隔离，同 body 去重安全；不完整产物返回诊断，不覆盖已有正确文件。
- 网络失败没有 body 时仍可保存失败 manifest，不能伪造 body/records 摘要；非 JSON 或业务失败体若保存也须受大小上限与固定公共来源约束。错误输出只保存分类和安全摘要，不回显认证信息。
- 不复用/修改用户默认缓存，测试所有写入在临时目录。存储失败退出失败，不能拿纯内存成功结果当持久交付成功。

## 拟定 CLI（实现后必须以真实 --help 复核）

```bash
.venv/bin/python -m tradingagents.dataflows.financial_snapshot fetch \
  --instrument 600519 --report-type income --output-dir ./out/financial_snapshots

.venv/bin/python -m tradingagents.dataflows.financial_snapshot inspect \
  --manifest ./out/financial_snapshots/snapshots/CAPTURE_ID/manifest.json
```

report-type 使用 balance/income/cashflow/all 明确映射。fetch 默认需要用户显式调用；inspect 严格离线，不访问来源 URL、不自动修复或重新抓取。exit 0=请求表均成功含真实 empty；exit 1=partial/failed 或存储失败；exit 2=参数/输入校验拒绝。all 部分失败保留各表结果并 exit 1，不能写“全部成功”。CLI 输出实际文件路径和限制，不硬编码 fixture 成功信息。

## 独立验收矩阵

1. 正常三表响应分别保存，body bytes 摘要与原始 fake response 一致；records 可沿 JSON Pointer 找回原字段。
2. 真空列表、结构缺失、业务错误、HTTP 500/401、坏 JSON 各状态不同。
3. 连续 429/超时、成功前两次失败：实际 HTTP 次数与预算一致，等待消耗 deadline，无额外隐藏 fallback。
4. 未知披露、多个版本、未来字段全部原样留档并给限制，不能宣布历史可得；G1 不把原始未来内容注入模型。
5. num=20 但无法证实完整历史时标覆盖有限，不声称全量；反序来源 records 保留原顺序。
6. 同 body 两次抓取两个 capture，各自时间正确；两股票两线程互不串数据。
7. body/records/manifest 各自篡改、缺文件、错误标的/表身份、不支持 schema、bool 版本号、路径穿越与 symlink 越界均拒绝。
8. 写入中断和重试、同 capture 冲突、并发去重，不产生可加载的半成品或覆盖正确结果。
9. 超大响应、非法 ticker/北交所/指数、负数与 bool 限额在正确边界拒绝；非法输入不联网。
10. 真实 CLI fetch（注入或子进程本地假传输）→ inspect → 复算最终产物摘要；inspect 测试拦截所有网络且删除原传输仍可读回。
11. 如抽取 a_stock 共享 helper，旧 temporal/retry/财报工具缓存路径定向回归。只加新纯函数绿测不代表旧生产入口兼容。

先写有意义的失败控制，再实现；ZCode 自身测试不修改 Codex 独立审计。真实公网 smoke 可在网络策略允许时仅对一个股票一张表执行，保存实际来源/时间且注明覆盖有限；不能访问时记录线上未验证，不把合成输入装成实采。

## G1 交接必须包含

`docs/ZCODE_G1_HANDOFF_2026-09-10.md`：实际接口和文件列表、请求路径图、字段/状态样例、真实测试命令/退出码、失败修复记录、涉及文件 SHA-256、CLI 产物、未覆盖范围。G1 自测完成后等 Codex 对 G1 独立验收；可整理 G2 映射表，不改处于冻结验收的 G1 代码。用户批准的是开发，本批未授权付费模型、真实交易或修改全局环境。
