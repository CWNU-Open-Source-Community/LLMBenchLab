# ADR-0006：可信本地正式数据集与真实 Provider 评测入口

- **Date**: 2026-08-27
- **Deciders**: LLMBenchLab maintainers（用户明确要求继续到可实际评测的流程）
- **Scope**: 标准数据集供应链、真实 OpenAI-compatible 接入、评测编排与报告
- **Related requirements**: FR-MOD-05–10、FR-BEN-01–08、FR-REP-01–04、NFR-SEC-01–05
- **Supersedes**: 无；补充 [ADR-0004](ADR-0004-secret-management.md) 与 [ADR-0005](ADR-0005-durable-task-execution.md)
- **Partially superseded by**: [ADR-0007](ADR-0007-web-provider-credentials.md) 已取代本文关于“REST/前端不得接收 Key”和“数据库不得保存加密凭据”的限制；[ADR-0008](ADR-0008-openai-compatible-sse-transport.md) 已取代本文把 Chat 成功响应统一限制为 4 MiB 的决定；[ADR-0019](ADR-0019-explicit-provider-api-protocol-adapters.md) 将本文 Chat-only 的 discovery/canary 扩展为显式 Chat Completions / OpenAI Responses / Anthropic Messages Adapter；其余数据集供应链、可信本地 CLI、预检与报告决定继续有效

## Status

Accepted；Web/REST 凭据边界由 ADR-0007 部分取代，Chat transport/资源边界由 ADR-0008 部分取代，Chat-only discovery/canary 范围由 ADR-0019 扩展为三个显式协议。

## Context

Phase 1/2 已提供 Demo、自定义 ZIP、OpenAI-compatible Adapter、持久化 Run/Response 和可靠 Worker 基础，但还缺少从固定公开来源取得正式数据、只用兼容 API 完成真实调用、恢复运行以及导出全部证据的一条用户入口。现有下一任务原本要求先完成 Phase 2 的全局限流、预算、审计与性能基线，并把标准 Benchmark 留给 Phase 3；用户在 2026-08-27 明确要求继续形成可真实模型测试的完整流程，并授权在线查找、下载数据集。

这项要求不授权把密钥保存到数据库、在自动测试中调用付费模型、绕过来源许可或宣称完成全部 Phase 2/3。它需要一个边界清楚的可信本地垂直切片，避免因为阶段顺序而继续只有 Demo，也避免在全局配额尚未交付时把系统描述为生产级批量平台。

MMLU-Pro 当前固定版本含 12,032 道 test 题，超过 dataset-v1 原 10,000 题资源上限；GPQA-Diamond 的源 CSV 把正确答案与干扰项分列，必须在转换时确定性重排，否则固定选项位置会泄漏答案。IFEval 需要其官方 strict/loose 规则执行器，不能映射到现有 exact-match/multiple-choice/numeric 而保持可比性。

## Decision drivers

- 用户应能通过可信本地命令提供 Base URL、API Key（安全提示或环境变量）和可选模型名，完成下载、导入、运行与报告。
- 数据来源、revision、源文件 SHA-256、转换参数和最终 dataset hash 必须可复核。
- 真实密钥不得进入 argv、数据库、Run 快照、报告、日志、前端或 REST 请求。
- 普通单元测试、CI 和 Smoke 必须继续完全离线。
- 正式全量评测必须支持限题/分组、小额预检、恢复和完整证据导出，避免配置错误放大为数千次请求。
- 未完成的全局配额、持久化 attempt ledger、公平调度和生产秘密托管必须继续如实保留为 Phase 2 缺口。

## Decision

新增一个仅面向可信本地操作者的正式评测 CLI，并让它复用现有 Dataset Loader、数据库实体、Run 协议、Worker/lease、Adapter 和 Evaluator，而不是另建一套不可审计的评分脚本。

### 数据集供应链

- 首个正式切片支持 MMLU-Pro test 和 GPQA-Diamond。插件只从固定 HTTPS URL 与固定 revision 下载，先校验已记录的源文件 SHA-256，再解析和转换。
- 原始数据、缓存和生成的 Benchmark ZIP 只写入 Git 忽略的 `artifacts/`，不提交第三方题目。
- MMLU-Pro 同时固定 test/validation Parquet；`official_cot` profile 用 validation 中按 category 的示例构造题目上下文，`direct` profile 明确作为较低成本、不可与官方 CoT 榜单直接比较的协议。
- GPQA-Diamond 只读取官方加密 archive 中的 Diamond CSV；以 `record_id + 固定 seed` 对四个选项逐题确定性重排，并在 metadata 保存 domain/subdomain 与转换 seed，不保存解释、作者或验证者个人字段。
- GPQA-Diamond 使用固定的 `zero-shot-cot-answer-line-v1` 提示 profile，要求末行输出 `Answer: X`；它是本项目固定协议，不冒充其他工具默认的重复采样结果。
- dataset-v1 的题数与文件资源上限提升到能容纳当前完整 MMLU-Pro，但 Schema、Hash 算法、三种客观评分含义和 `llmbenchlab-protocol-v1` 不改变。
- IFEval 延后到专用规则 evaluator；代码沙箱、Judge 和 Agent 不属于此切片。

### 真实 Provider 入口

- CLI 的 Base URL 是普通参数；API Key 只允许从指定环境变量读取或用 `getpass` 从终端安全输入。禁止 `--api-key` 明文参数。
- 远程 Provider 必须使用 HTTPS；明文 HTTP 只允许 loopback 本地推理服务。发现与三类远程生成都禁用 redirect、声明并只接受 identity encoding；本文接受时的发现正文上限为 2 MiB，Chat 成功/错误正文边界后来由 ADR-0008 部分取代，Responses/Messages 的同类边界由 ADR-0019 统一纳入。
- CLI 先调用同级 `GET /models` 验证认证并发现模型，鉴权跟随显式协议：Chat/Responses 使用 `Authorization: Bearer`，Messages 使用 `x-api-key` 与 `anthropic-version`，并以受累计 100 页、60 秒 wall-clock、2 MiB、10,000 项和重复 cursor 门禁保护的 `after_id` 跟进 `has_more/last_id`。若调用方明确给出模型名，可在 Provider 不支持模型列表时继续；未给模型且无法唯一发现时必须停止并给出候选，不猜测付费目标。发现结果中任何模型 ID 反射当前 Key 都会安全失败。
- 在创建正式 Run 前按显式协议执行一次最小 canary，分别调用 `/chat/completions`、`/responses` 或 `/messages`，并要求流式结果以 `[DONE]`、`response.completed` 或 `message_stop` 完整终止；它验证目标模型、请求形状和答案提取，成功体若明确返回不同模型则失败。预检结果只保存脱敏状态、返回模型名、request id、usage、finish reason 与延迟，不保存 headers 或 Key。
- 成功 content、raw usage 的字符串值/对象键、request ID、返回模型、system fingerprint 和 finish reason 若包含当前 Key，会在进入 Runner/快照/Response 边界前按精确值替换为 `[REDACTED]`。这是针对当前凭据的最后防线，不是通用 DLP。
- CLI 把 Key 临时注入由 `api_key_env` 指定、并冻结进 Run 的环境变量名；若该变量原来存在则退出上下文后恢复，否则删除。数据库 Model 与 Run 快照仍只保存变量名。ADR-0004 的 REST/前端禁收明文密钥规则不变。
- CLI 默认要求显式确认预计题数和 Provider 请求保守上界；上界同时计算 Adapter HTTP retries、题数、canary 和剩余 failed-attempt 预算 `max_attempts - failed_attempt_count`，不把 cooperative yield 算作失败。非交互自动化只能通过 `--yes` 继续。价格未知时不得显示虚假零成本。

### 执行、恢复与报告

- CLI 创建标准 Benchmark、Model 和 Run 后，在同一可信本地进程直接复用 fenced EvaluationRunner。它不要求 Redis，也不要求浏览器或常驻 API；结果仍进入同一数据库，之后可由现有 API/UI 查看。
- CLI 执行期间必须停掉常规 API/Worker stack 并独占同一数据库。代码会在付费 canary 前拒绝已有 `running` Run，并在持久化前二次检查，但无法可靠探测一个尚未领取任务的空闲外部 Worker。
- 大数据集执行改为固定数量的消费者协程，不为全部题一次性创建 task；Run 内并发上限和每题隔离语义保持不变。
- CLI 可用 `resume <run_id>` 恢复既有 pending/running（租约过期后）Run，只处理缺失 Response；过期但证据不全的 running lease 由本地 Runner fenced reclaim，未过期 owner 不会被覆盖。completed/cancelled/failed Run 只允许导出，不重复调用 Provider。
- 每次导出包含 `summary.json`、`groups.csv` 和逐题 `responses.jsonl`。汇总同时报告严格总分、completion rate、answered accuracy、Token、已知成本、数据/协议/代码快照，以及按不重叠 metadata group 的样本数与三项指标。
- 报告不得包含真实 Key、Authorization、原始数据源中未参与评测的个人字段或未脱敏上游错误。

### 约束与不变量

- 自动化测试只使用 fixture、MockTransport 与 Mock Adapter，绝不下载在线数据或调用真实 Provider。
- CLI 的一次 canary 是用户显式启动真实评测时的付费/网络操作；帮助、准备数据、单元测试和报告导出都不得隐式调用 Provider。
- 不把当前切片描述为全局预算硬边界、Provider exactly-once、生产级 SSRF 防护、完整 Phase 2 或完整 Phase 3。
- 不同 source revision、转换 profile、分组/限题选择或转换器版本必须产生不同 Benchmark version/slug 或 dataset hash，不能无提示混排。

## Alternatives

### 由浏览器直接接收并保存 API Key

- 优点：表面操作步骤少。
- 缺点：Key 经过浏览器、REST、调试工具和后台任务边界，违反 ADR-0004；仍解决不了多进程 Worker 的安全传递。
- 未选择原因：扩大秘密泄漏面，且需要新的密钥托管系统。

### 独立写一个不进入数据库的评测脚本

- 优点：实现快，不受现有 Schema/Worker 约束。
- 缺点：重复 Adapter/评分逻辑，缺少历史 Run、逐题证据、恢复、排行榜隔离和已有故障保护。
- 未选择原因：不能称为 LLMBenchLab 的完整、可审计流程。

### 等全部 Phase 2 完成后再接标准数据

- 优点：阶段依赖最整齐，可先获得全局限流和预算。
- 缺点：继续没有可用于真实模型的正式数据闭环，与用户明确优先级冲突。
- 未选择原因：本 ADR 用可信本地、确认、preflight、限题和明确非生产边界降低风险，同时保留 Phase 2 未完成事实。

### 直接提交转换后的第三方数据

- 优点：用户无需下载，首次运行更快。
- 缺点：增加再分发、版本漂移、仓库体积和潜在样题泄漏风险。
- 未选择原因：运行时固定来源获取更符合许可审计和数据供应链要求。

## Consequences

### Positive

- 用户可从固定公开来源到真实模型结果完成一条命令式、可恢复、可导出的流程。
- 正式题目不进入 Git，数据 revision、源 Hash 与转换参数仍可复核。
- 密钥边界继续满足 ADR-0004；自动测试和 CI 仍不会产生 Provider 费用。
- MMLU-Pro/GPQA 的转换陷阱被显式处理，而不是把不完整或泄题数据标成正式结果。

### Negative

- MMLU-Pro Parquet 需要额外的 `pyarrow` 运行依赖；全量 12,032 题会消耗显著时间、Token 和费用。
- 一次最小 canary 仍可能产生少量费用；`GET /models` 并非所有兼容 Provider 都实现。
- CLI 进程若被入侵，内存/环境中的 Key 仍可被同权限进程读取。
- 本切片没有解决跨多个 Worker/Run 的全局 RPM/TPM/费用硬上限、持久化 attempt 审计或上游调用 exactly-once。

### Neutral / follow-up

- Phase 2 继续 `in_progress`；P2-05/P2-06/P2-07 仍按现有 Next Task 收敛。
- Phase 3 只标记 P3-01/P3-02/P3-03 的客观数据垂直切片，不标记代码沙箱、IFEval 或整个阶段完成。
- 后续若在 Web 接收 Key，必须另立 ADR 并实现 OS Keychain/Secret Service/KMS 的 credential reference，而不是放宽本 ADR。

## Validation

- 使用内存 fixture 验证固定源 SHA、MMLU-Pro 两种 profile、GPQA 选项确定性重排、筛选/限题和稳定 dataset hash。
- 使用 MockTransport 验证模型发现、认证失败、唯一/多模型选择、canary、重试与错误脱敏。
- 使用临时 SQLite、fixture、MockTransport 与 Mock Adapter 验证 CLI 编排、恢复、全量分页导出和分组指标；另以固定真实数据源手工验证完整转换与 ZIP round-trip。真实 Provider 留给用户在明确确认后手工验收。
- 继续通过 `make lint`、`make test`、`make smoke`、Phase 2 回归与前端 production build。

## Security and privacy impact

该入口新增两类出站请求：固定数据集下载与用户提供的 Provider URL。数据集插件只访问代码内固定 HTTPS host/revision 并校验文件 SHA；Provider URL 仍是可信本地输入且继承现有 SSRF/DNS 重绑定风险，不能向不受信任用户开放。Key 不进入 argv/DB/REST/报告，但会短暂存在于当前进程环境中。GPQA 转换只保留评测必需字段和非个人分组元数据；原始 archive 仅在本地忽略目录缓存。

## Rollback or migration

本决定不新增数据库列。回滚可删除 CLI、插件、报告模块和可选依赖，并把 Dataset Loader 上限恢复；已经导入的正式 Benchmark/Run 是用户评测证据，不自动删除。生成物位于 `artifacts/`，由用户按数据许可和保留需求显式清理。

## References

- [MMLU-Pro official dataset](https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro)（访问 2026-08-27；实现固定具体 revision 与文件 SHA）
- [MMLU-Pro official evaluation repository](https://github.com/TIGER-AI-Lab/MMLU-Pro)（访问 2026-08-27）
- [GPQA official repository](https://github.com/idavidrein/gpqa)（访问 2026-08-27；数据内 `license.txt` 为 CC BY 4.0）
- [ADR-0004 — 数据库仅保存密钥环境变量名](ADR-0004-secret-management.md)
- [ADR-0005 — Durable task execution](ADR-0005-durable-task-execution.md)
- [ADR-0019 — 显式 Provider API 协议](ADR-0019-explicit-provider-api-protocol-adapters.md)

## Change history

| 日期 | 变化 | 原因 |
|---|---|---|
| 2026-08-27 | Accepted | 用户明确要求优先形成可真实模型完整评测的可信本地流程 |
| 2026-08-27 | Hardened | 终审后固定 HTTPS/loopback、identity/响应上限、Key 反射与返回模型拒绝、成功元数据脱敏及过期租约恢复边界 |
| 2026-08-27 | Partially superseded | ADR-0008 以真 SSE 和独立 wire/event/content 上限取代统一 Chat 4 MiB 成功正文边界 |
| 2026-08-30 | Partially superseded | ADR-0019 将 Chat-only discovery/canary 扩展为三个显式协议，并保留本文的数据供应链与可信本地确认边界 |
