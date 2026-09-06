# N04：固定案例与离线报告评测——ZCode 任务书

日期：2026-09-06。Codex 方案与独立验收，ZCode 业务代码、测试和使用文档。当前基线 865 passed、14 skipped、52 subtests、5 warnings，包含全部未提交的 N01–N03 及上一轮修复。

**交付状态：本任务书的 N04 离线阶段已实现并独立验收，最终 1009 passed、14 skipped。** 见 [验收记录](N04_ACCEPTANCE_2026-09-06.md) 和 [使用说明](OFFLINE_EVALUATION_GUIDE.md)。下文保留编码任务书，文中基线为本轮开始前结果。

## 本轮交付与依据

交付可实际运行的离线评测工具：冻结案例和证据、导入已保存报告、逐条评分、统计覆盖与失败、比较基线/候选评测并输出 JSON 与中文 Markdown。由此发现换模型、提示词或流程之后哪些约束退化。

本轮是 N04 的离线阶段；真实模型执行器和模型效果实验留待后续，不在默认评测中调用模型、行情或自动读真实历史目录。不实施 N05–N08，不做 Web 新面板，先提供稳定 CLI 和纯核心接口。人工核验过的事实标注可以随报告导入，自动完整性检查不能冒充事实判断。

研究依据：评测需区分固定任务、重复 trial、评分器和真实结果；代码检查、人工判断各有边界。采用固定案例、逐项结果及独立人工标注，而非一个不透明总分。[Anthropic：Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。本文的具体数据契约是本工程设计决定。

## 1. 模块与接口

新增 `tradingagents/evaluation/`，至少分离数据校验、评分、汇总/对比、CLI；不依赖 `tests`/`docs` 中的代码，不创建 TradingAgentsGraph 或 LLM client。复用 N01 的 `assess_reports`、`render_limitation_notice` 和既有评级解析器，不复制硬检查算法。

建议并固定公开接口，供独立验收调用（从 `tradingagents.evaluation` 导出）：

```python
canonical_digest(value: dict) -> str
grade_report(case: dict, report: dict, review: dict | None = None) -> dict
evaluate_suite(suite: dict, submission: dict, *, report_loader) -> dict
compare_evaluations(baseline: dict, candidate: dict) -> dict
render_evaluation_markdown(result: dict) -> str
render_comparison_markdown(result: dict) -> str
```

`report_loader(path: str) -> dict` 由 CLI 提供，核心测试可注入。所有输入均不原地修改；输出只含 JSON 安全类型，不含 NaN/Infinity。提供明确的 `EvaluationInputError(ValueError)`。评分版本 `grader_version="1"`，改变评分含义必须升级；不同版本不能直接做增减判断，需重新评分两组原报告。

## 2. 冻结案例集 schema_version=1

```json
{
  "schema_version": 1, "suite_id": "research-contracts", "suite_version": "1",
  "title": "研究报告约束案例", "trials_per_case": 2,
  "cases": [{
    "case_id": "stock_complete", "ticker": "600519", "trade_date": "2026-01-15",
    "instrument_type": "stock", "as_of": "2026-01-15T15:00:00+08:00",
    "selected_analysts": ["market"], "expected_quality_status": "complete",
    "allowed_ratings": ["Buy", "Overweight", "Hold", "Underweight", "Sell"],
    "evidence": [{"evidence_id": "e1", "available_at": "2026-01-15T10:00:00+08:00", "text": "人工冻结的合成证据，非真实行情"}]
  }]
}
```

字段校验：案例非空、case_id 唯一非空字符串；trials_per_case 是 1–50 的整数，拒绝 bool；日期是真实日历日期；时间带明确时区且有效，as_of 的上海日期须等于 trade_date；类型 stock/index；分析师集合非空、已知且不重复，指数不允许 fundamentals/lockup。expected_quality_status 仅 complete/limited/insufficient（按指定团队重算）；allowed_ratings 为非空合法五档集合。evidence_id 同案例唯一，text 非空，available_at 为带时区时间，可晚于 as_of 以便明确识别未来证据。

SHA-256 覆盖整个规范 JSON 案例集（字典键稳定排序、UTF-8、拒绝非有限数字），包括证据内容、时间与规则。案例/证据改变即换 digest；不要只 hash suite_id/version。不得静默接受未知字段拼写，所有 schema 对象采取明确字段列表；可选字段也需校验类型。

## 3. 报告提交清单 schema_version=1

```json
{
  "schema_version": 1, "submission_id": "candidate-model-b", "suite_id": "research-contracts",
  "suite_version": "1", "suite_digest": "冻结案例集的SHA256",
  "runs": [{
    "case_id": "stock_complete", "trial_id": 1, "status": "completed",
    "report_path": "reports/run-b.json",
    "observations": {"latency_ms": 1500, "input_tokens": 100, "output_tokens": 200, "cost_amount": 0.01, "cost_currency": "USD"},
    "review": {"report_digest": "规范JSON报告的SHA256", "reviewer": "人工审核者", "reviewed_at": "2026-01-16T09:00:00+08:00", "claims": [{"claim_id": "c1", "field": "market_report", "quote": "报告中的原文片段", "verdict": "supported", "evidence_ids": ["e1"]}]}
  }]
}
```

`observations`、`review` 可省略。重复 (case_id, trial_id)、未知案例、越界 trial、digest 不符、非法结构在执行前拒绝整份清单。`status` 仅 completed/failed；failed 不得携带 report_path/review，仍占该 trial 并进入失败分母，可带 observations。completed 必须提供路径。所有案例预期 trial 都进入分母，遗漏项生成 missing 记录，禁止只统计成功报告。有效清单里的单份文件缺失、坏 JSON、非法编码、错误顶层/字段类型记录为 invalid，继续处理其他 trial；不得把 KeyboardInterrupt/SystemExit 等进程控制异常吞掉。

CLI 报告路径相对 submission 文件所在目录解析，只读该目录内显式指定的 JSON 普通文件；拒绝绝对路径、`..` 以及符号链接逃出根目录。文件不可读按该 trial invalid 处理，路径格式违规拒绝清单。不得递归扫描 results_dir 或读环境凭据。核心注入 loader 接口无需依赖当前工作目录。

observations 为导入的测量值，本轮不新增实际调用计量：latency_ms/cost_amount 为有限非负数；token 为非负整数，拒绝 bool；cost_amount 与 cost_currency 成对出现，currency 为三位大写字母。缺失是 null，不能当 0。按字段报告 observed_count 与 mean；货币分组汇总，不跨币相加。输出注明“导入测量值”，不能把评分器执行耗时称为模型延迟。

## 4. 确定性报告检查

输出 `checks` 列表，每项 `{check_id, status, detail}`，status 为 pass/fail/unknown/not_applicable。固定以下八项，所有适用项都 pass 才是该 trial 的 `contract_pass=true`，任何 unknown 不得当通过：

1. `identity`：company_of_interest 或 ticker、trade_date 与案例精确对应；顶层 instrument_type/run_metadata 中已知类型须一致。缺类型为 unknown（可用本地指数注册表可靠识别指数），明确冲突 fail。
2. `team`：顶层 selected_analysts、quality 卡、有效 run_metadata 中已记录的集合均须与案例一致；顺序无关，冲突 fail，全缺为 unknown。
3. `decision_present`：最终决策字符串非空。
4. `rating`：复用 `parse_rating(default="")`，解析值在 allowed_ratings 内。缺评级 fail，不默认 Hold。这个结果仅是解析/允许集合符合性，不是收益方向正确率。
5. `quality_status`：按案例指定团队重新 assess_reports，结果与 expected_quality_status 一致。不要相信报告自报 complete。
6. `quality_record`：报告保存的 N01 质量结构与重算的 status、集合、active_count/fail_count、逐角色 grade/detail/chars 及 limitations 一致。集合换序允许；旧报告缺质量卡/记录 unknown 为 unknown，不能补造旧记录。
7. `limitation_notice`：重算为 limited/insufficient 时，最终文本包含当前完整 render_limitation_notice；只含标题或旧提示 fail。重算 complete 为 not_applicable。旧报告缺卡仍按重算结果检查，但不会因此把 quality_record 的 unknown 变为通过。
8. `run_metadata`：有效 N02 schema、run_id、ticker/date/type、公开配置指纹与 config_snapshot 的规范 hash 匹配；缺失为 unknown，有结构但损坏/与案例冲突为 fail。输出只记录 run_id、公开 config_fingerprint，不复制完整原始配置或报告正文。

质量卡的存在与质量状态正确是两项不同检查；旧报告可以被载入评估，但缺失记录如实表现为 unknown，完整契约不算通过。合法 JSON 中报告字段的错误类型需转为 invalid trial，不能打断整个套件，也不能 `str(dict)` 后算正常文本。允许真实报告里存在与评分无关的扩展状态字段，不作任意执行/反序列化。

## 5. 人工事实标注与时点

review 必须绑定当前完整报告 digest；reviewer、reviewed_at 有效。claim_id 唯一；field 只允许分析师报告、canonical/legacy 交易员计划、最终决策；quote 非空且确实出现在该字段字符串里。verdict 仅 supported/contradicted/unverifiable；supported/contradicted 至少一条本案例已知 evidence_id，unverifiable 可无证据；引用 ID 不重复。

标注不匹配/引用不存在/时间非法，整份 review 标记 `review_status="invalid"` 并记录固定原因，不使工程契约变好，也不把事实分算成 0 或 100%。CLI 的格式结构错误在输入校验拒绝，报告 digest/quote 与实际内容的关联错误在该 trial 记录 invalid review。有效标注为 reviewed；缺标注 unreviewed；空 claims 是有效空标注但比率仍 null。

每条标注中，只要有任一引用的 available_at > as_of，就计为 `future_evidence`。等于 as_of 可用，不同 offset 必须比较同一时刻，保留微秒。有效 supported 数仅包含 verdict=supported 且无未来证据的声明。`factual_support_rate = supported / 全部已标注声明数`（含 contradicted/unverifiable/未来引用）；零声明为 null。`temporal_violation_rate = 使用未来证据的声明数 / 至少引用一条证据的声明数`，零分母 null。

输出同时有 supported/contradicted/unverifiable/future_evidence、reviewed_claims、evidence_backed_claims 与 reviewed_trials/invalid_reviews/unreviewed_trials，明确这些仅覆盖人工抽查的声明，不能称整篇报告事实准确率；无标注的隐藏未来引用不会被自动发现。未来引用即使人工标为 supported 也不能计作得到时点内支持。

## 6. 汇总、比较与命令行

结果 schema_version=1，包含 suite_id/version/digest、grader_version、submission_id、固定顺序的 trials（case_id/trial_id）、逐项检查、report_digest、汇总 metrics。除导入测量值外不加入墙钟时间，相同输入生成相同评分 JSON。metrics 至少 expected/provided/completed/failed/invalid/missing、contract_passed、completion_rate、failure_rate、contract_pass_rate、unknown_checks、事实标注覆盖及上述比率、分字段 observations。

分母固定：expected=案例数×trials；completion_rate=completed/expected；failure_rate=(failed+invalid+missing)/expected；contract_pass_rate=contract_passed/expected。completed 的报告检查失败不冒充运行异常；它通过 failure_rate 与 contract_pass_rate 的差异体现。每个分母随指标输出。

比较仅在 suite_digest、grader_version 和预期 trial 身份完全一致时成立；不同 submission/model/config 可以比较。按 case_id/trial_id 配对，输出改善、退化、无变化、双方不通过；重点列出具体从 pass 变 fail/unknown 的检查和缺失 trial，不能只给总体差值掩盖个别退化。事实支持与时点比率只有双方均有有效标注的配对 trial 才给配对统计，同时展示共同覆盖数；覆盖不同的总体比率不直接称提升。observations 均注明导入值，成本只在同币种/双方有值的配对 trial 比较。不得输出模型更准或收益提升的结论。

```bash
.venv/bin/python -m tradingagents.evaluation run --suite SUITE.json --submission SUBMISSION.json --output-dir NEW_DIR
.venv/bin/python -m tradingagents.evaluation compare --baseline BASE/evaluation.json --candidate NEW/evaluation.json --output-dir DIFF_DIR --fail-on-regression
```

run 写 evaluation.json/evaluation.md；compare 写 comparison.json/comparison.md。输出目录须新建，已存在则明确拒绝，不覆盖任何源案例/报告/已有评测；完整校验和渲染成功后才创建输出目录，写入错误清理本次自建文件并报错。CLI 返回码：0 成功（run 中有契约不通过仍是成功生成评测）；1 仅 --fail-on-regression 发现退化（仍生成完整对比文件）；2 输入/读取顶层文件/输出错误。无 traceback 堆栈冒充用户提示。命令行 --help 可用。

对 compare 导入的 JSON 做 schema/计数/身份结构校验，不能拿随意字典当评测结果；结果本身是文件记录，不是签名证明。

## 7. 固定示例与验收

仓库放 `examples/evaluation/`：suite.json、baseline.json、candidate.json、合成报告文件及 README，至少六类（正常个股、资料受限、严格多数不足、指数、缺质量/运行档案的旧报告、坏质量卡/无评级负例），包含两次 trial、遗漏/failed、已支持/无法核验/未来证据、混币/缺测量值、候选既改善又退化。数据和预期结果必须静态冻结，不在运行时调用当前评分器生成“标准答案”。注明合成演示，非模型实测；固定文件均无私人数据。

README 和 docs 使用说明解释如何保存自己真实研究的多次运行并编写清单、如何人工冻结案例和审核声明；复制示例后需显式更新 suite_digest/report_digest。提供 digest 子命令或清楚的纯本地计算命令，避免用户手工猜 SHA。示例可被直接执行，至少一组 --fail-on-regression 返回 1 并生成含具体退化项的报告。

ZCode 测试覆盖：schema/重复和缺失trial/分母、真实 N01 再评分/旧卡 unknown/输入不变、标注绑定及微秒/时区边界、缺失与混币指标、不可比 suite/version、真实 CLI 子进程与只读文件保护。不得用源码关键词或 AST 测试替代行为。

Codex 另外编写 `docs/audit_n04_*.py`，测试未知反例和 CLI，ZCode 不修改这些审计脚本；如发现夹具错误报告理由。完整交接写 `docs/ZCODE_N04_HANDOFF_2026-09-06.md`，保留准确计数与未验证范围。

## 执行边界

ZCode 只改本任务业务与测试/示例/文档；保留当前所有未提交文件，不 reset、不提交/推送、不安装依赖或改全局环境。不读/改真实认证、缓存、记忆、断点，不执行付费模型或真实行情请求。离线测试使用临时目录并拦截 requests/yfinance/curl_cffi/socket 等外部路径。默认旧 865 项继续通过；Codex 最终负责统一全量、ruff（含审计脚本）、git diff --check 和真实 CLI 流程验收。
