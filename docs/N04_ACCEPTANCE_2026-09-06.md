# N04 离线评测验收记录

任务书：[N04_OFFLINE_EVALUATION_PLAN_2026-09-06.md](N04_OFFLINE_EVALUATION_PLAN_2026-09-06.md)。编码由 ZCode 完成，Codex 负责方案、独立审计与最终验收。

当前状态：**N04 离线阶段已通过 Codex 独立验收**。最终全量 1009 passed、14 skipped、52 subtests passed、5 warnings。范围是冻结案例和离线报告评分/比较；真实模型执行器、模型效果实验与 N05–N08 尚未实施。

使用入口：[离线评测使用说明](OFFLINE_EVALUATION_GUIDE.md)、[可运行的合成示例](../examples/evaluation/README.md)。

## 最终验证结果

2026-09-06，在项目现有 Python 3.12.12 虚拟环境执行：

| 验证 | 实测结果 |
|---|---|
| 默认全量 `python -m pytest -q` | 1009 passed、14 skipped、52 subtests passed、5 warnings；45.82 秒，exit 0 |
| 新增测试构成 | ZCode 36 项 + Codex 独立 108 项；原有 865 项继续通过 |
| `ruff check tradingagents web cli tests docs/audit_*.py` | 通过，exit 0 |
| `git diff --check` | 通过，exit 0 |
| 合成示例基线、候选 `run` | 各返回 0，生成 JSON 与 Markdown |
| 候选比较 + `--fail-on-regression` | 返回 1，仍生成含 7 个具体退化检查的结果 |
| 基线自比较 | 返回 0，无退化 |
| 重用既有输出目录 | 返回 2，原结果保留 |
| `digest` | 返回 0，与案例清单绑定的规范 JSON 摘要一致 |
| 文件完整性 | 181 个源码/测试/示例/配置文件在全量前后 SHA-256 一致；14 份独立审计文件未被 ZCode 修改 |

14 项跳过与基线一致：13 项 Claude Agent SDK、1 项 Gemini 的可选依赖缺失。5 条警告为既有 Anthropic 模型名提示，没有新增警告。本轮未安装依赖。Python 3.10 的 Z 时间解析兼容分支已读码检查，未在 3.10 或 Windows 上实跑。

最终日志：`/tmp/codex-n04-final-20260906.log`、`/tmp/codex-n04-final-ruff-20260906.log`、`/tmp/codex-n04-example-final-20260906.log`。冻结清单：`/tmp/codex-n04-final-manifest-20260906.json`。独立真实 CLI 脚本：`/tmp/codex_n04_example_acceptance.py`。

最终 CLI 产物目录：`/var/folders/1k/6mfcfpgd38bgh4s31fz6wsv00000gn/T/codex-n04-final-cli-r07erufv`，其中 `acceptance-summary.json` 保存命令退出码、完整指标、退化明细与输入文件哈希保持不变的检查结果。CLI 在最小环境、临时工作目录运行，使用 sitecustomize 拦截 socket 连接和域名解析。

## 实际交付行为

- 新增 `tradingagents/evaluation/` 纯评分/校验/比较包及 `python -m tradingagents.evaluation` 命令；支持 `run`、`compare`、`digest`。
- 8 项确定性检查复用 N01 质量规则、核对 N02 运行档案。失败、坏报告和遗漏均保留在固定分母；旧报告缺失信息标为 unknown，不能充当通过。
- 人工标注绑定报告摘要、原文引用和冻结证据，并保留时区与微秒边界。声明统计与全量指标使用共享汇总函数；导入比较先验证明细与缓存统计一致。
- 比较逐项扫描全部配对尝试。整体已经失败的案例出现新退化也会显示；候选缺失的原 pass 检查显示 absent。
- 导入延迟/token/费用区分未记录和实际 0；费用按币种分组，跨币不做配对。
- 提供 6 类、每类 2 次的合成冻结示例、首页命令入口、使用指南和 ZCode 交接文档。

实际合成示例中，基线完成 10/12、失败 1、遗漏 1、契约通过 8；候选完成 11/12、失败 1、无遗漏、契约通过 9。配对 2 项改善、1 项退化、7 项均通过、2 项均未通过；指数 trial 2 丢失 7 个 pass 检查，因此总通过率上升仍触发退化退出码 1。

基线人工标注 4 条声明，支持率 75%、时点违规率 25%；候选无标注，因此两个事实指标无法计算，共同覆盖为 0，不能据此宣称事实改善。基线合成费用有 CNY 和 USD，候选对应指数尝试为 USD，跨币配对正确地没有 `cost_comparison`。

## 基线与范围隔离

上一轮已验收 865 passed、14 skipped、52 subtests、5 warnings。代码均未提交，不能拿 Git HEAD 当作本轮基线。

本轮编码前只读源码/测试/文档快照：`/var/folders/1k/6mfcfpgd38bgh4s31fz6wsv00000gn/T/tradingagents-n04-baseline-a4i5oxfs`，其中 manifest.json 记录 180 个文件哈希；不包含真实缓存、记忆、断点或凭据。

与此快照逐文件核对，既有业务源码和旧测试断言保持不变。已有文件中的编码变化只有 `tests/conftest.py` 追加 N04 测试辅助函数，以及 README/路线图的本轮文档更新；其余为新增 N04 文件。工作树保持未提交、未推送。

## 独立审计

`audit_n04_core_2026_09_06.py` 和 `audit_n04_workflows_2026_09_06.py` 由 Codex 维护，已纳入默认 pytest 收集。当前冻结 108 项，使用临时报告、人工合成证据和网络禁用的真实 CLI 子进程。未调用真实模型或行情，也未导入用户真实研究数据。

初次在 ZCode 写文件期间遇到未完成语法，40 项导入错误仅是中间态，不计业务缺陷。可导入版本首轮 48 passed/16 failed；补齐 10 项字段边界后退回 R1。R1 修复后原 77 项全部通过，后续扩展 77 passed/11 failed，再补两项写出所有权和遗漏报告明细，进入 R2。

问题与复現细节见 [N04_REVIEW_2026-09-06.md](N04_REVIEW_2026-09-06.md)。重要日志：

- `/tmp/codex-n04-independent-r2.log`：48 passed/16 failed。
- `/tmp/codex-n04-core-boundaries-r3.log`：核心边界 38 passed/12 failed。
- `/tmp/codex-n04-independent-r4.log`：77 passed/11 failed。
- `/tmp/codex-n04-targeted-r3-final.log`：R3 首批修复后 126 passed（98 独立 + 28 ZCode）。
- `/tmp/codex-n04-r3-consistency.log`：同类统计一致性的 10 个新增反例均失败，要求评分和导入校验改为共享汇总实现；不能以首批全绿代替方案完整性。

本轮主要发现集中在固定分母、逐项退化遗漏、未知值与无标注、坏报告隔离、路径和写出所有权、导入产物的结构与统计一致性。每项均记录复现和修复要求，最终状态以本文最终验收结果为准。

**审核结论：R1–R3 及同类遗漏、最终类型分支、混币示例、错误测试字段均已交由 ZCode 修复并复验通过，无遗留阻断项。** Codex 最终逐段复核评分/导入共享口径，未只依据 ZCode 自报通过。可选的派生声明数字段未记录时不参与计分，声明覆盖与比率以实际明细为准。

## 证据边界

评测的自动检查衡量报告与冻结规则是否符合。人工事实标注只覆盖提供了标注的声明，时点判断只覆盖显式引用的冻结证据；未标注的事实或隐藏未来数据无法由此自动发现。时间/token/费用是清单导入值，本轮未实现真实调用采集。

trial 是清单声明的尝试，本工具不证明真实执行或多次运行相互独立；真实实验应逐次保存独立 run_id 的输出，避免重复导入同份报告。合成案例用于验证工具行为，不能用于证明某个模型更准确、稳定或更便宜。

`--fail-on-regression` 用作运行/工程检查退化门控；人工事实比率和导入成本提供可比较范围内的明细，不附加未设计的自动质量/费用阈值。
