# D2 Codex 实施契约

本文件由 Codex 维护，优先于 ZCode 设计中的冲突条款。D1 已独立验收，可立即开始 D2，无需用户再次确认。分 D2a 准备/提示词/恢复、D2b CLI/Web 入口定向交接；等待独立审核时继续不冲突的下一步设计，不空转。

1. 默认无显式 manifest 对 stock/index 一律保持无卡；禁止虚构 dummy 财报来生成指数卡。显式指数 manifest 经现有 D1 校验产生 not_applicable。
2. 模型只接合法投影：ok 计算数值及其实际依赖的合法出处；情景仅 ok 才显示数值，假设明确标用户给定。D1 inputs 内含 future/unknown 原始声明，仅供审计，不能将全卡/全表 Markdown 截取后送模型。被排除的原始值/标题/正文不能进入五个真实决策工厂的提示词。零个 ok 指标仍需保留确定性的 unknown/partial/not_applicable 限制，不得空串。用唯一 future sentinel 验证，不能仅测纯渲染 helper。
3. 必须接实际 prepare_graph_run/_safe_resume_checkpoint/propagator；fresh 在图执行和模型调用前读取/验证/计算/注入，失效输入 fail-fast。不新增网络、取数、记忆写入或模型调用，不改 D1 数学语义。
4. 计算 digest 与 run_id 区分：相同输入计算值相同；新 fresh run 的 run_id 必须新生成，不能要求整卡跨 fresh 逐字节相同。卡绑定可信 instrument/type/trade_date/run_id 和 manifest_digest；元数据存可复验摘要，路径不能替代 digest。机器路径和上传文件字节不进提示词。
5. 必须先判 resume 再处理 fresh 输入。恢复只读 checkpoint 中原卡，不打开当前 manifest；删除/修改路径、恢复时新增路径都不能改变原卡或给旧无卡断点补卡。真实 MemorySaver/SQLite interrupt→resume 验证原节点不重跑、卡不变、文件未读取；不能仅 mock 返回值。
6. 卡的 run_id/digest/instrument/type/date 与运行元数据不符、未知版本等损坏，在模型调用前明确拒绝恢复，保留原 checkpoint，不清空/覆盖，不把损坏变成“未记录”。真正旧断点缺卡才是未记录。提示词 helper 直接被调用遇到无效归属也不能暴露数字，可返回受控 invalid-panel 限制；正式流程由准备层阻断。
7. 文件大小/条目数/UTF-8/JSON 对象类型设置明确上限。Web 用显式上传或清楚标注的本机路径最小入口；不得依 offline_ref 任意读文件/联网。上传使用独立临时存储或显式内存参数，不改用户历史/缓存；恢复不依赖上传临时文件仍存在。
8. 投影最多 15 数值行并限制每字符串/总字符数，所有截断明示；限制独立保留不能只展示 ok。数值/单位/口径/期间与 D1 代码表一致。提示词不能保证模型文案永不写错，原始代码表继续保存供复核。指数 RM 共用工厂、指数 Trader/PM 与个股三工厂都覆盖，无额外 LLM 调用。
9. 真实验收：fresh有卡/无卡/指数/非法→两次fresh不串卡→断点恢复删除文件不重读→旧断点不补卡→错身份或digest拒绝→未来原始声明不入模型→三出口与调用次数一致。仅离线 fixture，禁止真实模型/行情/交易/提交推送。每小批完成写当前 hash/真实测试/限制，接着推进下一已定义事项，稳定批次全量由 Codex 做。

原 D2 文档在并行设计时被 ZCode 重写，Codex 修正因此独立保存在本文件，避免覆盖丢失；请阅读后再实施，勿重写本文件。
