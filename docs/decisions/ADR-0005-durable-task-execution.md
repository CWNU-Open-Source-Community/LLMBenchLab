# ADR-0005：PostgreSQL 事实来源、Redis Streams 通知与租约式独立 Worker

- **Status**: Accepted
- **Date**: 2026-08-25
- **Deciders**: LLMBenchLab maintainers
- **Scope**: Phase 2 可靠任务执行、数据库、队列、Worker 与恢复语义
- **Related requirements**: P2-01、P2-02、P2-03、P2-04、P2-06、P2-07
- **Supersedes**: [ADR-0002](ADR-0002-sqlite-first.md) 的“Phase 1 单进程执行”运行边界；保留其 SQLite 本地兼容与 Alembic 唯一 schema owner 决定
- **Superseded by**: 无

## Context

Phase 1 在 API 进程内创建异步任务，用内存字典去重；API 启动时把遗留 `running` Run 全部标为 `failed`。该模型不能承受 API/Worker 独立重启，不能安全地让多个执行者竞争同一 Run，也无法在队列故障或消息重复时恢复。

当前数据库已有两个可复用的不变量：Run 状态使用条件更新领取；`evaluation_responses` 对 `(run_id, question_id)` 有唯一约束。但唯一约束只阻止两条持久化 Response，不能阻止过期 Worker 在租约被接管后继续更新进度、终态或错误；现有 `completed_questions += 1` 也会在重复执行时漂移。

任务系统需要同时处理两个无法消除的提交裂缝：

1. API 提交 Run 后、向队列发通知前可能崩溃或遇到 Redis 故障；
2. Worker 获得上游响应后、提交本地 Response 前可能崩溃。

第一个裂缝可由数据库对账恢复。第二个裂缝意味着系统不能承诺 Provider 调用或外部计费 exactly-once；只能保证一个逐题持久化证据和一次协议计分。所有自动测试仍只使用 Mock，不产生真实调用或费用。

## Decision drivers

- 未完成 Run 在 API/Worker 重启、Redis 暂时不可用和消息重复后仍可恢复。
- 两个 Worker 竞争时同一 Run 只有一个当前有效执行者，过期执行者的所有写入都被 fencing。
- 保持 `/api/v1` 和 `llmbenchlab-protocol-v1` 的题目、评分分母、聚合与排行榜含义不变。
- PostgreSQL 提供共享部署数据库；SQLite 继续支持个人本地开发和快速测试。
- 队列故障不得覆盖或分裂数据库中的任务事实。
- 行为必须能以真实 PostgreSQL、Redis 和独立进程故障注入验证。

## Decision

### 1. 拓扑与事实来源

- PostgreSQL 是 Phase 2 部署的共享数据库和唯一任务事实来源。Run 状态、取消意图、attempt、租约、逐题 Response、聚合、错误和 dead-letter 证据只以数据库为准。
- SQLite 保留为本地单机兼容路径，使用同一 ORM 和 Alembic head；SQLite 只支持一个 Worker，不宣称多 Worker 并发安全或生产可用。
- API 只验证请求、保存 `pending` Run，并在数据库 commit 成功后发送队列通知。API 不加载 Adapter、不执行问题，也不因自身启动/停止改写 `running` Run。
- 独立 Worker 是唯一执行入口。Worker 每次只拥有有限数量的 Run，并继续遵守 Run 快照中的 1–4 题内并发。
- Redis Streams 是低延迟通知和工作分发层，不是状态数据库。消息只携带版本、`run_id` 和 correlation ID；不能携带权威状态、租约或结果。

### 2. Redis at-least-once 语义

- API 顺序固定为“数据库 commit，再 `XADD`”。数据库 commit 失败时绝不发送；`XADD` 失败时不回滚或复制 Run，而是保留可恢复的 `pending` Run、记录脱敏 `last_error`，仍返回原有 `202` 与 Run ID。
- Worker 使用固定 Stream 和 Consumer Group，通过 `XREADGROUP` 读取并在数据库处理结果落定后 `XACK`。长时间未确认消息可由 `XAUTOCLAIM` 重新交付。
- 消息可能重复、延迟、丢失或在 ACK 后再次出现。重复消息不得改变终态 Run，也不得让有效 attempt、Response、进度、分数或持久化费用重复。
- 每个 Worker 定期从数据库扫描“到期的 pending”与“租约过期的 running” Run；该 reconciliation 是 commit/XADD 裂缝、Redis 数据丢失与 Redis 暂时不可用时的恢复路径。Redis 恢复后可重新通知，但数据库也可直接驱动领取。
- Redis ACK 失败只会带来重复通知；Redis ACK 成功也不能让数据库中的未完成 Run 消失。

因此系统的任务交付语义是 at-least-once，数据库结果提交是幂等的；不得把它描述为端到端 exactly-once。

### 3. Run 租约、心跳与 fencing

Run 增加以下可靠性元数据：

| 字段 | 语义 |
| --- | --- |
| `attempt_count` | 已成功取得的 Run 执行租约次数，从 0 开始 |
| `max_attempts` | 创建时冻结的最大 Run 级 attempt，默认 3 |
| `lease_owner` | 当前 Worker ID；无有效执行者时为 `null` |
| `lease_token` | 单调递增的 fencing generation；释放租约后保留最后值 |
| `lease_expires_at` | 当前租约数据库时间截止点 |
| `heartbeat_at` | 当前 owner 最近一次成功续租的数据库时间 |
| `next_attempt_at` | `pending` Run 下次允许领取的数据库时间 |
| `last_enqueued_at` | 最近一次成功发出 Redis 通知的时间 |
| `last_error` | 最近一次脱敏的投递或可重试执行错误 |
| `dead_lettered_at` | 有限重试耗尽进入永久 `failed` 的时间 |

数据库时间是租约比较权威。PostgreSQL 领取使用单条条件更新（扫描可用 `FOR UPDATE SKIP LOCKED` 优化）；SQLite 使用同一 guard 的短写事务。应用单调时钟只用于本地轮询 deadline，不能裁决租约所有权。

一次原子领取必须同时满足：

- `cancellation_requested = false`；
- `attempt_count < max_attempts`；
- Run 为到期的 `pending`，即 `next_attempt_at` 为空或不晚于数据库当前时间；或 Run 为 `running` 且 `lease_expires_at` 已到期；
- Run 不是终态。

成功领取在同一语句/事务中把状态设为 `running`，写入 owner，令 `lease_token = lease_token + 1`，更新 heartbeat/expiry，令 `attempt_count = attempt_count + 1`，并保留首次 `started_at`。条件更新影响 0 行即领取失败，不得执行该 Run。

心跳只能在 `status=running`、owner/token 匹配且租约尚未过期时续期。过期租约不能被旧 owner 复活。任何 Response 提交、进度更新、重试释放、取消收敛、完成或失败操作，都必须在同一事务中锁定/校验当前 Run 的 owner、token 和未过期租约；校验失败的 Worker 立即停止该 Run，取消并等待尚未完成的题目协程，且不能再启动 Provider 调用或修改数据库。失租时已经在途且无法由客户端撤销的 Provider 请求仍可能产生一次外部副作用，这是 exactly-once 不可保证边界的一部分。

`lease_token` 在租约释放后不归零，使任何旧 token 永久失效。终态或重新排队时清除 owner、expiry 和 heartbeat。

### 4. Response 幂等与协议不变量

- `(run_id, question_id)` 继续作为逐题幂等键；恢复时只加载尚无 Response 的计划题，已有成功或错误 Response 都不再次评分。
- 逐题提交在一个短事务内先验证有效 lease，再判断是否已有 Response；真实新插入与进度同步提交。唯一约束继续作为最后一道竞态防线。
- `completed_questions` 必须等于该 Run 的持久化 Response 数，不能无条件递增；终态聚合从 Response 事实重新计算。
- 旧 Worker 即使已完成 Provider 请求，只要租约过期或 token 已变化，就不能保存 Response、费用、进度或终态。
- 题级 Adapter/Evaluator 错误继续按协议 v1 保存一条 0 分 Response，不触发整个 Run attempt。Run 级基础设施/不可恢复异常才进入任务重试。
- `score`、`completion_rate`、`answered_accuracy`、token/cost 完整性、Benchmark/Prompt/模型快照和排行榜过滤规则保持 ADR-0003 定义不变。

以上保证数据库只保留一个计分/费用证据，但不保证上游调用 exactly-once：若进程在 Provider 已响应、本地提交前崩溃，接管 Worker 可能再次调用 Provider。未来若需要外部计费去重，必须使用 Provider 支持的幂等键或单独 ADR，不能从当前数据库约束推断。

### 5. 重试、恢复、取消与 dead-letter

- Run 级失败采用有限指数退避。attempt 未耗尽时，当前 token owner 把 Run 重新置 `pending`、设置 `next_attempt_at` 和脱敏 `last_error`、清除当前租约；reconciliation 在到期后再次领取/通知。
- 在取消优先之后，若持久化 Response 数已等于 `total_questions`，Worker 或 reconciliation 必须直接从数据库事实重新聚合为 `completed`，不能为了补做终态提交再消耗 attempt 或调用 Provider。这覆盖“最后一题已提交、Worker 在终态 commit 前崩溃”的窗口。
- attempt 耗尽且 Response 集仍不完整时，Run 进入既有 `failed` 终态，同时写 `dead_lettered_at`、`error_message` 和 `last_error`。Redis dead-letter Stream 如存在也只是派生通知；`failed + dead_lettered_at` 是权威 dead-letter。
- Worker 崩溃不立即改写状态；当前租约到期后由另一个 Worker 接管并递增 token/attempt。已持久化 Response 被跳过。
- API 重启只重新检查 Alembic head 和依赖，不扫描或修改运行中任务。
- pending Run 取消时立即进入 `cancelled`。running Run 只持久化 `cancellation_requested`；当前 Worker 在心跳或题目边界观察后以有效 token 聚合已有证据并进入 `cancelled`。若 Worker 已死亡，reconciliation 在租约过期后收敛取消，不再领取执行。
- 取消、完成、失败竞争通过状态/token 条件更新裁决；终态操作幂等，终态消息不增加 attempt。
- Graceful shutdown 停止领取新任务并停止启动新题；当前安全边界可重新排队，无法完成时让租约自然到期。不得像 Phase 1 一样把所有 active Run 直接标为失败。

### 6. 健康、就绪与可观测性

- `/api/v1/health` 保持兼容并检查 API/数据库；新增 liveness 与 readiness 语义。liveness 只表示进程可响应，readiness 检查数据库 revision/连接和 Redis，并以组件化、脱敏状态解释降级。
- Redis 不可用时 readiness 可以返回 `503 queue_unavailable`，但已连接数据库的 `POST /runs` 仍可保存并返回 `202`；Run 的 `last_error` 表示等待 reconciliation 的可恢复状态。
- API 请求、队列消息、Worker、Run 与 Question 使用稳定 correlation ID；结构化日志至少包含事件名、run/question、worker、attempt、lease token 和结果，不记录 API Key、Provider 请求正文、原始模型输出或完整题目。
- 指标至少覆盖 pending/running/dead-letter 数、claim 成功/冲突、lease 过期/接管、heartbeat 失败、queue publish/read 错误、重试与恢复延迟。指标是派生观测，不是任务状态。

### 7. 容量和支持边界

- 本切片默认 lease 30 秒、heartbeat 10 秒、数据库 reconciliation 1 秒、最大 Run attempt 3；配置校验必须保证 heartbeat 明显短于 lease。
- 一个 Worker 同时执行一个 Run；Run 内并发继续限制为 1–4。PostgreSQL/Redis 连接池和 Worker 数量必须显式受限。
- Redis Stream 使用有界近似裁剪和 AOF 持久化，但任何保留策略都不能成为恢复正确性的前提。
- Provider 级速率、预算、公平调度和完整背压仍属于 Phase 2 后续工作。在这些门禁完成前，Phase 2 总状态保持 `in_progress`，即使本 ADR 的可靠执行基础已实现。

### 约束与不变量

- 数据库是唯一事实来源；Redis、日志、指标和 Worker 内存均不能覆盖数据库状态。
- API 永不执行 Run，API 重启永不终止 Run。
- 同一时刻每个 Run 最多一个有效 lease token；所有执行写入都必须 fenced。
- `completed_questions == COUNT(evaluation_responses WHERE run_id = ...)`。
- 每个 `(run_id, question_id)` 最多一条 Response。
- 终态 Run 不可重新领取，且不保留 owner/expiry/heartbeat。
- Redis 故障、消息重复或 ACK 丢失不能改变协议 v1 结果。
- 不得记录或传输真实密钥；测试不得调用真实 Provider。

## 状态转换

| 事件 | 前置条件 | 数据库结果 |
| --- | --- | --- |
| API 创建 | 模型/Benchmark 有效，事务提交成功 | `pending`、attempt 0、无租约；随后 best-effort 通知 |
| Worker 领取 | pending due 或 running lease expired，未取消，attempt 未耗尽 | `running`、attempt +1、token +1、owner/heartbeat/expiry 写入 |
| 心跳 | 当前 owner/token 且租约未过期 | heartbeat/expiry 前移 |
| 逐题提交 | 当前有效租约，题目尚无 Response | 插入一条 Response，按事实同步进度 |
| Run 级可重试失败 | 当前有效租约，attempt 未耗尽 | `pending`、设置 next attempt/last error、清租约 |
| 完整证据恢复 | 当前有效租约或 reconciliation 发现已耗尽的过期租约，且 Response 数等于计划题数 | `completed`、从 Response 聚合、清租约，不调用 Provider |
| 重试耗尽 | 当前有效租约或 reconciliation 发现已耗尽的过期租约，且 Response 集不完整 | `failed`、dead-letter 时间/错误、清租约 |
| pending 取消 | 非终态且尚未领取 | `cancelled`、finished time |
| running 取消 | 取消意图已持久化，当前 token owner 或过期 reconciliation 收敛 | `cancelled`、聚合已有证据、清租约 |
| 完成 | 当前有效租约，所有计划题已有 Response | `completed`、从 Response 聚合、清租约 |
| 重复/陈旧消息 | 终态、有效租约仍存在或条件不满足 | no-op；消息可 ACK，DB reconciliation 保留恢复能力 |

## Alternatives

### Celery/RQ 作为权威任务状态

- 优点：成熟 Worker 生命周期、重试和生态工具。
- 缺点：会引入一套与 Run 数据库并行的状态/重试/ACK 语义；仍需数据库 fencing 和 Response 幂等。
- 未选择原因：当前垂直切片需要清晰证明数据库唯一事实来源，框架状态会扩大 split-brain 和运维面。未来可在不改变本 ADR 不变量的前提下重新评估。

### Redis List 或 Pub/Sub

- 优点：实现简单。
- 缺点：Pub/Sub 无离线恢复；List 的消费确认、pending owner 和崩溃认领需要自行构造。
- 未选择原因：Streams Consumer Group 已提供 pending、显式 ACK 和故障认领，同时数据库对账能覆盖 Stream 自身丢失。

### PostgreSQL 表轮询，不使用 Redis

- 优点：只有一个基础设施和事实来源，正确性最直接。
- 缺点：低延迟依赖持续查询，通知扩展性和可观测的投递链较弱，也不满足本阶段 Redis 队列目标。
- 未选择原因：保留数据库轮询作为可靠兜底，Redis Streams 作为非权威加速层能同时满足正确性和阶段目标。

### 恰好一次任务/Provider 调用

- 优点：最易解释费用和副作用。
- 缺点：跨数据库与任意外部 Provider 无法仅靠本系统事务实现；网络超时无法判断请求是否已生效。
- 未选择原因：没有跨系统事务或 Provider 幂等协议，宣称 exactly-once 会是错误保证。

### 继续使用 SQLite 多进程共享文件

- 优点：无需新服务。
- 缺点：单写者与缺少行级 `SKIP LOCKED` 无法提供目标并发领取和生产故障恢复边界。
- 未选择原因：SQLite 仅保留单 Worker 本地兼容；真实并发验收必须使用 PostgreSQL。

## Consequences

### Positive

- Run 不依赖 API 内存任务，可跨 API/Worker 重启恢复。
- Redis 故障和 commit/XADD 裂缝不会丢失数据库任务。
- 单调 token 与逐写 fencing 能阻止过期 Worker 污染接管后的结果。
- 协议 v1 聚合仍以持久化逐题证据为基础，重复交付不会重复计分。
- PostgreSQL 和独立 Worker 支持受限水平扩展，并保留 SQLite 快速本地路径。

### Negative

- 实现和测试显著复杂，需要 PostgreSQL/Redis、租约时序、对账和故障注入。
- at-least-once 在外部调用提交裂缝中仍可能产生重复 Provider 请求/真实费用。
- SQLite 与 PostgreSQL 有不同并发保证，必须维护双方言迁移与明确支持矩阵。
- Redis readiness 降级与“POST 仍可持久化”需要运维理解，不能用单个绿色健康状态概括。

### Neutral / follow-up

- Phase 2 还需完成 Provider/Model/Run 级限流、预算、背压、公平调度、完整审计和性能基线。
- 若后续引入 Celery、Kafka 或云队列，必须继续满足数据库事实来源与 fencing 不变量，或新增 superseding ADR。

## Validation

- 在真实 PostgreSQL 上让两个 Worker/连接同时领取一个 Run，断言只有一次条件更新成功、只有一个有效 token。
- 对同一 `run_id` 重复发布 Redis 消息，断言终态和每题 Response 唯一、进度等于 Response 数。
- 在至少一题持久化后强制终止 Worker，令租约过期并由新 Worker 接管；断言旧证据保持、剩余题完成、分数语义不变。
- Worker 执行时重启 API，断言 Run 状态、租约和执行不中断。
- 暂停真实 Redis 后创建 Run，断言 `202`、可恢复 pending/错误状态；恢复 Redis 后由数据库 reconciliation 完成。
- 验证 pending/running 取消、lease 过期、旧 token heartbeat/Response/finalize 拒绝、有限重试和 dead-letter。
- PostgreSQL 与 SQLite 均运行新 revision `upgrade -> downgrade -> upgrade` 和 `alembic check`；PostgreSQL 还要验证约束与并发语义。
- SQLite→PostgreSQL 导入按表比较行数、主键集合和 canonical hash；源 SQLite hash 不变，目标冲突和提交前中途失败整体拒绝/回滚。另在真实 PostgreSQL 注入 COMMIT 确认丢失和已确认提交后的复核/输出失败，分别验证 `commit_outcome_unknown` 与 `committed_but_verification_failed` 不会被误报为普通回滚失败。
- 完整执行 Phase 1 API/协议回归、15 题离线 Mock Smoke、前后端 lint/test/build 和完整 Compose `up --build --wait`。

若任何真实 PostgreSQL/Redis/进程重启关键场景未运行或失败，不得把可靠执行基础标为完成；若限流、预算、背压等后续项未完成，Phase 2 总状态仍为 `in_progress`。

## Security and privacy impact

- PostgreSQL 和 Redis 默认只位于 Compose 内部网络，不暴露宿主端口；示例密码仅适用于本地隔离环境，不能称为生产秘密管理。
- Redis 消息仅包含内部 ID 和版本，不包含 API Key、Provider URL 凭据、Prompt、题目或模型输出。
- `last_error`、日志、ready 响应只保存稳定错误码和脱敏摘要，不包含 DSN 密码、Redis URL、请求正文或 Provider 响应正文。
- Worker 与 API 使用相同的最小数据库权限是本地切片的限制；生产部署应拆分角色、启用 TLS、认证、网络策略和正式秘密管理。
- SQLite→PostgreSQL 导入可能复制原始回答和题目，必须在受信环境运行并保护源/目标备份。

## Rollback or migration

### 升级

1. 先固定/备份现有数据库并停止旧 API 写入。
2. Alembic 新 revision 只新增可靠性字段、约束和索引；既有终态 Run/Response 不变。升级边界上的旧 `running` Run 仍受其冻结的 Phase 1 `restart_recovery=mark_failed_without_resume` 语义约束：普通中断 Run 聚合已有证据后收敛为 `failed`，已请求取消的 Run 收敛为 `cancelled`，二者都保留逐题 Response 和协议快照。只有由新代码创建、快照明确为 `database_lease_resume_missing_responses` 的 Run 才进入租约恢复路径。
3. 启动 PostgreSQL、Redis 和一次性 migration 服务，再启动 API 与 Worker；不得让多个服务各自并发执行 Alembic。
4. SQLite→PostgreSQL 为单向、显式导入：源需在当前 Alembic head 且停止写入，目标需为空且不得同时服务应用；在一个目标事务内保持 ID/JSON/Decimal/UTC/协议快照，并在提交前输出行数、主键集合和 canonical hash 对账。源文件保持只读，带凭据的目标 DSN 从环境变量读取而不进入 argv。
5. 导入提交边界必须区分三种非成功结果：提交前异常回滚并返回 exit 2；PostgreSQL 未确认 `COMMIT` 时返回 `commit_outcome_unknown`/exit 4，因事务原子性目标可能为空或已完整；已确认提交后的稳定只读快照或摘要输出失败时返回 `committed_but_verification_failed`/exit 3，目标已经提交。后两者都禁止盲目重试，必须先核验目标；PostgreSQL→SQLite 不提供自动反向同步。

### schema downgrade

- 回退新 revision 前必须停止 API/Worker，并确认不存在 `pending` 或 `running` Run；migration 在发现 active Run 时拒绝 downgrade。
- downgrade 只删除租约、attempt、队列和 dead-letter 元数据，保留五类核心实体、Response 和协议分数。该可靠性元数据删除不可逆，必须先备份。
- Redis 可直接停用/清空，因为它不保存权威事实；清空会造成重复或延迟通知，不会删除数据库任务。
- 回退到 Phase 1 进程内 Runner 只允许在所有 active Run 已排空/取消且明确接受失去恢复能力后进行。
- PostgreSQL 生产数据不会自动“反向同步”回 SQLite。回滚依赖迁移前 SQLite 源/一致性备份或单独验证的导出流程，不能把 schema downgrade 描述为数据库平台回迁。
- 历史 `0001 -> 0000` 会删除题目 `position`，带数据的全历史 downgrade 不保证原题序；本阶段的数据保持验收只覆盖新 head 与 `0001` 的往返，`downgrade base` 仅用于空隔离库或明确接受数据损失的恢复演练。

## References

- [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- [Phase 2 next task](../NEXT_TASK.md)
- [ADR-0002 — SQLite first](ADR-0002-sqlite-first.md)
- [ADR-0003 — Evaluation protocol](ADR-0003-evaluation-protocol.md)
- [Redis XREADGROUP documentation](https://redis.io/docs/latest/commands/xreadgroup/)（访问：2026-08-25）
- [Redis Streams documentation](https://redis.io/docs/latest/develop/data-types/streams/)（访问：2026-08-25）
- [SQLAlchemy `with_for_update(skip_locked=True)` documentation](https://docs.sqlalchemy.org/en/20/core/selectable.html)（访问：2026-08-25）

## Change history

| 日期 | 变化 | 原因 |
| --- | --- | --- |
| 2026-08-25 | Accepted | Phase 2 可靠执行基础在实现前固定一致性、失败与回滚语义 |
| 2026-08-25 | Clarified upgrade/finalization | 保持旧 Phase 1 冻结恢复语义，并覆盖完整 Response 后、终态提交前崩溃的恢复窗口 |
| 2026-08-25 | Clarified importer commit boundary | 区分提交前回滚、COMMIT 结果未知和已确认提交后的复核失败，避免运维盲目重试 |
