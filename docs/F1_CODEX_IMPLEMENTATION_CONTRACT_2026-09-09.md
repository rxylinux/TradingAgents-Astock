# F1 Codex 实施契约：离线复盘记录与时点检索

Codex维护。本文件优先于F/F1草案冲突条款。E独立验收通过后按本契约开工；当前仅准备，不授权绕过E失败。F1完成记录与离线检索最小闭环，F2生产记忆接入与F3效果评测分别交接，不把F1通过写成整个F已交付。

1. **范围与可演示入口**：新增离线模块，仅消费显式传入的JSONL文本/已解析列表；纯校验与检索函数无IO/网络/模型。提供独立CLI读取用户显式文件，传as-of、ticker、instrument-type、limit、可选query-window，输出JSON与Markdown（含选择理由/排除/未知/版本）。拒绝退出码2且不生成成功报告。生产TradingMemoryLog、用户历史/缓存/记忆不改；测试临时fixture。F1不计算新收益、不扩展_fetch_returns、不自动分类错误原因。
2. **稳定身份与不可变快照**：record_id独立于run_id，同run多期限/多版本可并存。唯一身份至少区分run、instrument/type、明确窗口、版本；来源引用自带版本/digest。规范JSON全记录摘要（排除自身摘要字段）用确定性算法复算；相同ID不同内容拒绝，不隐式覆盖，同内容重复可确定去重并报告。load与retrieve不修改输入或内部可共享子对象；返回独立对象或不可变结构。文件只读不等于对象不可变，测试嵌套修改与两次调用隔离。
3. **未知保持未知**：数值只接受有限非bool值；收益用ratio:fraction、费用用明确bps等闭合单位，禁止null变0/字符串数字强转/NaN/Infinity。缺费用/基准/MAE保留null与原因，不默认费率，不从rating推收益或正确性。error_type及correct_risk_flags只能来自显式人工/外部标注，带来源和标注时间；原文保留，不把C2可评估状态或数据缺口自动解释为真实推理错误。
4. **到期必须显式**：prediction_horizon自由文本只作描述，不将“3-6 months”自动映射为一个到期日。maturity与window.start/end由输入明确给定；缺失不可作为已成熟经验，窗口合法且start<=end，到期不得早于结果窗口结束。pending始终不进入已验证经验；区分not_yet_mature、overdue_unresolved、unknown_maturity。expired_unresolvable只能是显式带原因状态，不自动30交易日过期，不套周末日历猜交易日；同样不作为已解决事实。
5. **完整可知时间门槛**：决策形成、结果窗口结束/到期、observed_at、publication_time、记录/标注版本可用时间是不同概念。已成熟、已观测、已发布、当前版本当时可用必须同时成立（AND），不能用“maturity或publication任一满足”替代。publication早但观察/版本较晚，历史检索不得见后填结果；观察早但publication晚也不可见。任一必需时间未知给明确unverifiable原因，不从其他时间推导补齐。版本可用时间可以一个明确record_available_at锚点，但不能把作者自述publication当成严格历史快照证明；availability保留declared_only/verified_snapshot等来源事实，首批fixture声明不冒充point-in-time验证。
6. **时间严格解析**：YYYY-MM-DD与带Z/offset datetime接受且保留精度；拒绝无时区datetime、无效尾部、非法日期，不截前十位。as_of日期按上海当日结束，datetime按真实瞬时转换；date精度的可用时刻按当日结束保守比较，不伪造00:00时间。同日带offset跨日与as_of日中必须验证。决策原值和outcome/标签分别受各自时点过滤；不能用最终版result重新写回早期决策。
7. **检索定义可执行**：函数必有可信ticker与instrument_type、as_of、有限正非bool limit、已知ranking_version。先完整校验/过滤，再排序与limit。v1同标的优先、同类型次之、形成时间新到旧、record_id稳定平局；其余排序规则和事件有效期只消费显式字段，未知不伪造同行业/市场状态。给每个record可审计排除原因，可同时保留多个缺口，limit截断另报，不把截断计为时间违规。无合格记录也返回完整限制。
8. **重叠查询边界**：F1只有调用方显式给query-window时才做相同instrument/type的窗口重叠排除。查询窗口必须进函数签名和CLI参数，严格校验；使用闭区间，边界相同也算重叠。不从ticker/as_of猜query.maturity。未提供则明确overlap_filter=not_requested；未知记录窗口不给“不重叠”结论。不同历史记录之间不做贪心去重来伪称统计独立，F3训练/验证/留出间purge/embargo与配对另定。pair_id/arm可保留，不在F1评分，不认为两条不重叠就证明无泄漏。
9. **引用与摘要可信边界**：本记录自有digest可以复算；外部thesis/evidence/config digest若未提供原始快照，只能标external_unverified，不能声称内容自洽已验证。若提供明确快照则复算并拒绝失配；不得用event_count代替证据版本身份。CLI输出所用输入文件digest、规则版本、as_of与参数、选择/排除清单，不读取/修改生产配置或记忆路径。
10. **离线验收**：真实CLI正常/全排除/坏schema/损坏digest出口；两次加载与反序输入结果稳定；相同ID异内容拒绝；同run不同期限共存；publication/observed/version三种未来各排除；maturity未来但publication过去仍排除；日中as_of/date精度/offset跨日；pending到期未到期分别报告；未知费用/收益仍null；bool/NaN/非法窗口拒绝；明确闭区间重叠和未请求重叠分别呈现；嵌套输入不变/输出改动不污染下一调用；生产memory源码哈希不变。合成fixture只证明契约，报告明确不代表预测效果提高。

已决事项：无自动30日过期；错误与正确风险标注用显式外部输入；到期/窗口不解析模糊期限；时点条件AND且保留版本可用时间；F1重叠过滤须显式query-window；排序v1固定，不调生产权重；F1离线CLI与纯函数，后续生产接入/F3另审。E验收后可直接实施，无需再次等待用户确认。
