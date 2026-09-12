# D1 财务面板独立审核

## 最终独立验收（2026-09-09）

D1 按“确定性计算 + 离线 CLI + 报告出口”范围验收通过；D2 决策接入另行验证。

- 五份 Codex 独立审计：49 passed in 1.89s，exit 0。
- `.venv/bin/python -m pytest -q`：**1260 passed / 14 skipped / 52 subtests / 5 existing warnings**，52.39s，exit 0。
- 发现新增测试 E731（赋值 lambda），已交 ZCode 改成同义 def，断言未变；修复后 `.venv/bin/ruff check .` 通过，`tests/test_financial_panel.py` 64 passed in 1.52s，exit 0。`git diff --check` 通过。
- 283 文件快照核对：全部生产文件不变；仅 D2 设计文档和上述测试风格修复变化。未以这次新小改重复完整套件，已按影响做定向验证。
- 实际正常 CLI → JSON+Markdown，营收同比 20%、净利润率25%、OCF/净利润80%、限定净现金13000000000元，与固定fixture期望一致；情景正常生成。另用显式标的 header 构造不匹配，CLI exit 2 且不生成报告。最初将无 header 的fixture传不同可信标的仍成功是允许行为，不列为产品缺陷。
- 最终 production hash `949eb7dcb07baa3bb75b327ef0920b390e11df5c87f4501f2b9f7c65c5927b77`；test hash `c778bed40dabfacbd5be072b9629ee4f4a7d87ac095f33a83a46b7cc45b9575f`。HEAD仍d269ba43047fe23c2e60307d5ca83425578f8080，未提交/推送。

D1不支持真实vendor取数、重述链选择、FX/DCF/季度推导；未接模型决策、不证明预测或收益提升。十四项跳过为可选 SDK/Gemini 依赖。已批准 ZCode 依据 `D2_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md` 继续 D2a/D2b，不等待用户再次确认。

## R1（23:43）：编码中提前反馈，7 failed / 1 passed

本批尚无最终交接。Codex 对已落地计算入口 `compute_financial_panel` 新增独立脚本，未改业务实现或 ZCode 测试。

```text
.venv/bin/python -m pytest -q docs/audit_d1_financial_2026_09_08.py --tb=short
7 failed, 1 passed in 0.05s (exit 1)
.venv/bin/ruff check docs/audit_d1_financial_2026_09_08.py
All checks passed! (exit 0)
git diff --check
通过 (exit 0)
```

1. **口径覆盖及顺序依赖（2 例）**：同年合并 revenue=100/profit=20 与母公司 revenue=50/profit=5 均合法，但 margin key 不含 scope，最终只保留一条，且 manifest 顺序反转后 margin 在 0.2/0.1 间改变，digest 却相同。必须以 metric + scope + 完整期间身份构造结果 ID；全面检查 YoY/OCF/净现金同类 key。排序只是稳定覆盖顺序，不能修复丢失结果。合法两种口径都要保留。
2. **TTM 不足整期（1 例）**：2024-01-15~2024-12-31 被月份计数函数当成完整 12 个月。要求连续完整起止窗口，明确日历周年/闰年边界规则；TTM fiscal_year/window 与 period_end 必须一致，不凭“涉及十二个月份”断言覆盖一年。
3. **EPS(TTM) 接受单季（1 例）**：metric=eps_ttm + kind=single_quarter/Q4s 正常过载入校验。EPS(TTM) 必须对应声明的完整十二个月期间；其他 metric 的合法季度窗口不能自动成为 EPS(TTM) 的合法窗口。
4. **存量种类被静默改写（1 例）**：cash 输入 kind=cumulative + period_end 被直接变成 snapshot。校验声明的 kind，拒绝矛盾输入，不能归一化成看似正确的口径。
5. **混合精度制造时间（1 例）**：revenue 披露于 2025-01-29T15:00+08，profit 仅披露日 2025-01-30。结果写 2025-01-30T00:00+08 且 datetime 精度，凭空造出凌晨已可用的含义。最晚依赖只有日期精度时结果须保持日期精度，不因其他依赖有精确时间而升级。可在内部用保守日终比较；不能在公开 provenance 写成真实发布时间。
6. **零基数崩溃（1 例）**：YoY denominator<=0 路径引用未定义 prior_slot，实际 NameError。应返回 not_applicable 并保留原始输入值。

输入不变性控制例通过。修复后保持控制例、原有单位和缺失值红线；不编辑这份独立脚本。

同一路径的必要相邻检查：情景现在从 eps_inputs[0] 和按 metric 覆写后的倍数字典选择，多个期间/口径 EPS 或倍数可能被任意混搭。明确选择规则/显式组合或拒绝歧义，不允许“第一/最后一个”隐式胜出；快照结果与流量结果的 key 必须区分完整身份。D1 可选择拒绝多组情景输入以保持小范围。

继续完成自己的定向测试、实际离线 CLI、三出口与 stable handoff；此时不重复全量。C1/C2 前批验收不自动覆盖新增 D1 文件。

本次 SHA-256：

```text
306e467ab4a74f88cfc2662e10e66a420fa879bcf66dee10afd2f1a5682f5d00 tradingagents/dataflows/financial_panel.py
e0bbcafeb2529bfde25b890fd7439dca9b7434ced715c49aa0b422ec6eb75298 docs/audit_d1_financial_2026_09_08.py
```

## R2（23:57）：R1 8/8 已修复，同比选择仍有 3 项失败

实测 `.venv/bin/python -m pytest -q docs/audit_d1_financial_2026_09_08.py --tb=short` 输出 **3 failed, 8 passed in 0.06s**。新增三例独立行为边界：

1. TTM 2023-10-01~2024-09-30 与 2023-01-01~2023-12-31 被当成同比配对，仅 fiscal_year 差一年不足以证明可比。应匹配完整窗口相隔一年的起止月日语义，闰年处理明确；错配不能 ok。
2. 两组完整 TTM（9 月末和 12 月末）都有各自去年对照，只产一条同比，反转输入又改变选中窗口。分组须含窗口身份，不能只按 kind/window='TTM' 分组后 next/first。两组独立结果都保留，canonical digest 相同的输入排列不影响计算。
3. 原有 FY2024/FY2023 收入同比 20% 正常；仅添加一项尚未披露的 FY2025 输入，FY2024 结果就消失。当前 max(fiscal_year) 在可得性过滤前执行，违背先 as-of 后选择。未来/未知输入可以单列排除，但不能淘汰当时已可计算的结果。

R1 修正的结果 key 解决了 scope 覆盖，但上游选数仍有 first/max 造成的选择漏洞。将可得性过滤、窗口选择、前期匹配分开处理，保持当时有效结果和明确缺口；不通过删除 TTM/多期间支持绕过现有契约。D1 仍不支持重述链，此处都是各自合法的不同报告期间。

实际离线 CLI 独立验证（同真实模块入口，临时输出目录）：

- stock_normal：exit 0，JSON+MD 均生成；营收同比 0.2、净利润率 0.25、OCF/净利润 0.8、限定净现金 13000000000 元；缺两项同比前年数据使 overall partial，未假称全完备。
- stock_gap_missing_debt：exit 0，净现金 missing_input，其他可算项保留。
- stock_future_disclosure：exit 0，overall unknown，无 ok 指标。
- index：exit 0，overall not_applicable，metrics 为空，无个股财报计算。

这是 fixture 算术/入口验证，不是实际股票判断或收益表现。Codex 同时让 ZCode 继续 D2 文档设计，D1 先修 R2 后稳定交接；此轮不重复全量。

R2 SHA-256：

```text
75af79932cdc58ab81beea0c3790fe58d99d8cd4e205880f93ed49b94bda719d tradingagents/dataflows/financial_panel.py
9575764cf251f57f34703a0074e37a83d105e030ccca19e21deca187d6c91d56 docs/audit_d1_financial_2026_09_08.py
```
