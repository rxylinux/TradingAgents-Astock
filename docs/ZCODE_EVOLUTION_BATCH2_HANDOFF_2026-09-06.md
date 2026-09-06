# ZCode 演进第二批交接（N03 历史报告对比）

日期：2026-09-06 · 分工：Codex 计划与验收，ZCode 编码
基线：第一批已验收（824 passed）。本批**未提交**。
最终已由 Codex 独立验收：865 passed，详见 [验收记录](AGENT_EVOLUTION_ACCEPTANCE_2026-09-06.md)。
Codex 独立脚本四份由 Codex 维护（`audit_evolution_comparison` 17 例、
`audit_evolution_web` 4 例、`audit_evolution_runs`/`quality` 38 例）。

## 变更明细

### 核心模块 `tradingagents/report_comparison.py`

- `compare_reports(left, right) -> dict`：纯计算（零模型/零网络）。同
  ticker + 同 instrument_type 才可比（不同日期允许）；不同标的或个股/
  指数混用 → `MismatchedReportsError`。旧报告缺 type 时用**指数注册表**
  可靠推断，不确定写 `unknown`——不默认 stock。
- 交易员 canonical（`trader_investment_plan`）优先、legacy 回退且只比
  一次。角色「未运行」（不在启用集合）与「已启用但报告空白」可区分；
  旧报告团队未知时标「团队未知」而非已启用/未运行。
- 评级原值展示（不默认 Hold，缺失标「未记录」）；unknown 质量不可作已知
  状态比较（`_quality_of` 的 `known=False`）；不输出模型能力/收益判断。
- `render_comparison_markdown(comparison) -> str`：完整 diff **不截断**
  （Codex 退回 #2：长报告末尾的结论性变化不能静默丢失）；Web 下载与 CLI
  共用。
- CLI `python -m tradingagents.report_comparison L.json R.json
  [--output F.md]`：`--output` 不得与任一输入指向同一文件（resolve 处理
  符号链接/相对路径、`samefile` 处理硬链接，Codex 退回 #1）；输出写入
  失败清楚报错不留成功提示。

### Web 入口（`web/app.py`）

- **空闲首页 + 历史报告页**均有「🔄 对比两份历史报告」按钮（Codex 退
  回 #5：查看历史后也能进入对比并返回原报告）。
- 对比页两个 selectbox 以**稳定路径**（非列表索引）为值——历史列表变动
  后不会误指（Codex 退回 #6）。右侧默认 index=1（与左侧不同版本）。
- 结果绑定生成时的 `(left_path, right_path)` 身份——选择变化后旧结果与
  下载按钮消失（提示重新对比）；同一份报告、比较错误同样清空旧结果。
- 侧栏点击单份历史按钮 → 自动退出对比模式（清空 comparing/
  cmp_result_md/cmp_identity）。
- 返回单份报告按钮 → 清空全部对比状态。
- 后台分析进行中，历史页对比按钮禁用；独立运行态 AppTest 通过。

### 第一批收口（随本批一并交付）

- `validate_run_id` / `is_run_metadata` 改 `fullmatch`（拒绝末尾换行）。
- `history._read_run_info` 非法 UTF-8 单条跳过 + 警告。
- 交接措辞纠正：13 × claude-agent-sdk + 1 × langchain-google-genai。

## 回归（tests/test_report_comparison.py，20 例）

核心：同日不同配置、不同日同配置、不同 ticker 拒绝、stock vs index 拒
绝、全字段相同、评级不默认 Hold、canonical 优先、未运行 vs 空白、旧格
式未知、不输出能力判断、质量限制变化。CLI：子进程成功 + 冲突退出码 1 +
坏 JSON 退出码 2 + 直调。Web 渲染：核心 section 完整、配置差异提示、零
网络验证。真实 UI 的身份绑定、默认不同版本、错误清空和返回，由 Codex AppTest 覆盖。

## 验证记录

```
默认完整回归: 865 passed, 14 skipped, 52 subtests passed, 5 warnings
  本批前基线 824 → 净增 41 = ZCode N03 回归 20 + Codex N03 独立审计 21
  （comparison 17 + web 4，含分析运行中对比入口 disabled 实证）
跳过: 13 × claude-agent-sdk + 1 × langchain-google-genai（Gemini，#87）
ruff check tradingagents web cli tests: 通过
git diff --check: 通过
```

全程离线（requests + yfinance/curl_cffi 拦截 + socket 拦截）；未动真实
数据/凭据/全局环境；未提交未推送。

## 涉及文件

| 文件 | 变更 |
|---|---|
| `tradingagents/report_comparison.py` | 新：compare/render/CLI（含四组退回修正） |
| `web/app.py` | 对比状态区（身份绑定/清空/返回）+ 历史页入口 |
| `web/components/sidebar.py` | 侧栏历史按钮退出对比模式 |
| `tradingagents/run_records.py` | 收口：fullmatch |
| `web/history.py` | 收口：UTF-8 跳过 |
| `tests/test_report_comparison.py` | N03 回归 ×20（新文件） |

## 已知限制

- N03 只做「差异展示」，不做跨股评分排名、不做模型评测（N04 路线）。
- CLI `--output` 的防覆盖检查在写入前执行；若用户在检查与写入之间替换
  文件为 symlink 指向输入报告，仍可能覆盖（竞态窗口极窄，CLI 一次性使
  用场景可接受）。
- Web 对比结果的身份绑定基于**点击时的路径快照**——它能防止用户在同一
  会话内改选择后旧结果错配，**不监控外部文件变化**（列表刷新后路径可能
  不同，此时旧结果会被清空提示重新对比）。

## 最后收口（Codex 第三轮退回修正）

- `web/history.py` `_read_run_info` 与 `_load_incomplete_index` 补
  `UnicodeDecodeError` catch——非法 UTF-8 文件此前导致整个历史列表抛异常。
- `tests/test_report_comparison.py` 删除最后 `TestComparisonUIIdentityBinding`
  三个只搜索源码/AST 字段的无效测试——真实 UI 行为已由 Codex 的
  AppTest（`audit_evolution_web` 最终 4 例）覆盖默认不同/改选择/同一份/比较异
  常/历史入口/返回。
- 对比正文：交易员段落去掉「canonical 优先、只比较一次」的实现说明（用
  户不需要知道内部机制）；质量 section 标题改为「结构化质量状态（报告完
  整性，非事实准确率）」。
- 新增 `docs/REPORT_COMPARISON_GUIDE.md`（Web 步骤 + CLI 命令 + 比较条件）。

## Codex 审核结论

1. 接受完整 diff 直接展开：下载包含全部差异行，长页面的折叠属于后续展示优化。
2. 接受只在输出存在时检查 samefile：不存在的输出尚无 inode，不可能已是输入文件的硬链接。路径替换竞态限制已如实记录。
3. 最后一项运行态按钮问题已修复；Codex 全量 865 passed、14 skipped、52 subtests，ruff（含审计脚本）与 diff check 通过。无剩余待审核请求。
