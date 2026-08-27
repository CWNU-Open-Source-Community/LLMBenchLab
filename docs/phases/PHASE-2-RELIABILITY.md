# Phase 2：可靠性与任务执行

- 状态：`in_progress`
- 前置阶段：[Phase 1 — MVP](PHASE-1-MVP.md)（`completed`）
- 后续阶段：[Phase 3 — Benchmarks](PHASE-3-BENCHMARKS.md)
- 核心决定：[ADR-0005 — Durable task execution](../decisions/ADR-0005-durable-task-execution.md)
- 相关安全决定：[ADR-0007 — Web Provider credentials](../decisions/ADR-0007-web-provider-credentials.md)
- 相关传输决定：[ADR-0008 — OpenAI-compatible SSE transport](../decisions/ADR-0008-openai-compatible-sse-transport.md)

## 阶段目标

把单进程 SQLite 执行方式升级为以 PostgreSQL 为共享任务事实来源、Redis Streams 为可丢失/可重复的通知层、独立 Worker 为唯一执行入口的可靠任务系统。系统在受限并发下通过数据库租约、心跳、fencing、逐题幂等和对账恢复保护 `llmbenchlab-protocol-v1` 证据，但不承诺 Provider exactly-once、生产高可用、无限横向扩展或未经测量的容量。

## 功能范围

- PostgreSQL 共享部署数据库、双方言 Alembic migration 和 SQLite 单 Worker 本地兼容路径。
- Redis Streams at-least-once 通知；数据库保存 Run、取消、attempt、租约、Response、聚合、错误和 dead-letter 的全部权威事实。
- 独立 Worker、原子领取、数据库时间租约、心跳、单调 fencing token、有限重试/退避、取消、租约过期接管、重复投递 no-op 和 dead-letter。
- 显式、单向 SQLite→PostgreSQL 导入：只读且停止写入的源、空目标、单目标事务、并发互斥及六表 count/PK/content digest 对账；`model_credentials` 密文随数据库复制，部署 keyring 不随数据库复制。
- Run 内既有 1–4 题并发，以及 Phase 2 尚待完成的 Provider 限流、预算硬上限、完整背压和公平调度。
- 单个 Run 的输出/等待配置：Web 可按 Benchmark 建议选择 `max_tokens=1..131072`，或用 `null` 省略上游字段；`read_timeout_seconds=1..1800` 作为等待下一批 Provider 字节的空闲读取上限随 execution snapshot 固化，不是总生成时限。未显式提供 `max_tokens` 的通用 API/protocol-v1 默认仍为 256；这些每题参数不等同于 P2-05 全局预算治理。
- LLMBenchLab 应用 JSON 日志、请求/Run/Question correlation、存活/健康/就绪端点和数据库派生任务 gauges，以及尚待完成的历史 counters、延迟、完整审计与主循环 liveness。
- 真实 PostgreSQL/Redis、故障注入、竞争条件、进程恢复、迁移/导入演练，以及尚待完成的性能/容量基线、告警和完整 Runbook。

## 非目标

- 不新增标准大型 Benchmark、代码沙箱、Judge、Arena 或 Agent。
- 不实现 Kubernetes、多区域容灾、生产 HA 或无限水平扩展。
- 不改变 Phase 1 的题目、评分分母、聚合、排行榜或协议含义；不兼容变化必须升级协议或 API 版本。
- 不以本地 Response 幂等推断 Provider 请求/计费恰好一次，不在自动化测试中调用真实模型。

## 支持边界与不变量

- PostgreSQL 是多 Worker 验证过的部署目标和任务事实来源；SQLite 只支持个人本地单 Worker，不宣称多进程写协调。
- API 先提交 `pending` Run，再 best-effort `XADD`，自身不加载 Adapter 或执行题目；Redis 故障不能撤销数据库提交。
- Worker 通过数据库扫描覆盖 commit/XADD 裂缝、消息丢失和 Redis 暂停；重复/延迟通知只能触发数据库条件检查。
- 同一 Run 同时最多一个有效 owner/token。Response、进度、重试、取消和终态写入都必须验证未过期 lease 和 fencing token；大 Run 的同步快照加载移出事件循环，加载期间已领取租约仍持续心跳。
- `(run_id, question_id)` 最多一条 Response，`completed_questions` 来自持久化 Response 数，完成、取消、fail-attempt dead-letter 和 expired-lease dead-letter 都在进入终态前从 Response 事实重算。
- Web/API 的 `api_key` 是只写字段；一模型一行的 `model_credentials` 只保存 AES-256-GCM 认证密文，AAD 绑定 Model ID 与规范化 Provider origin。API 与 Worker 必须共享数据库之外的部署 keyring；旧 `api_key_env` 来源继续兼容，Run 快照只记录非秘密的 credential source。
- Provider origin 改变必须在同一更新中提交新 Key；存在 `pending`/`running` Run 时禁止改变 Provider endpoint、模型名或凭据。浏览器在提交开始、关闭、切换 Mock 与 unmount 时清空 Key，读取 API、队列、Run、报告和日志都不得包含明文或加密材料。
- 终态报告不信任可能陈旧的 Run 聚合列：`metrics` 从计划题目和持久化 Responses 派生，`groups.csv` 与 `responses.jsonl` 使用同一证据口径，`metrics_provenance` 记录与 Run 字段的漂移。
- Provider 调用不是 exactly-once：若 Worker 在上游响应后、本地提交前崩溃，接管者可能重复外部调用或计费。
- `max_tokens=null` 只表示让 Provider 采用其默认值，不表示无限输出。Provider 明确以 `finish_reason="length"` 截断且没有可用内容，或截断后无法解析最终答案时，Response 错误证据使用 `output_truncated`；这仍按协议严格计零。
- OpenAI-compatible Chat 显式请求 SSE 与流式 usage，看到 finish 后不提前返回；若有 usage-only 尾块则继续消费，并必须收到 `[DONE]` 才作成功。普通 JSON fallback 继续兼容，usage 缺失时保持未知。部分流不计分，transport 异常仍按 protocol-v1 快照有限重试，不声称 Provider exactly-once。
- 六服务 Compose 是本地可靠性/故障验收拓扑：`postgres`、`redis`、一次性 `migrate`、`api`、`worker`、`frontend`。它不是生产编排或 HA 证明。

## 任务状态

| ID | 状态 | 已交付与剩余范围 |
| --- | --- | --- |
| P2-01 一致性与容量设计 | `partial` | ADR-0005 已固定事实来源、交付、租约、恢复与回滚语义及默认容量边界；正式 SLO、容量模型/基线未完成 |
| P2-02 PostgreSQL 迁移 | `foundation_delivered` | `0002` 与 `0003` 双数据库方言往返、真实 PG check、SQLite 六表单向导入及 credential binary 对账/回滚/并发证据已交付；keyring 独立迁移且无自动反向回迁 |
| P2-03 Queue/Worker | `foundation_delivered` | Redis 通知、独立 Worker、数据库扫描、租约/心跳/fencing、幂等键与 ACK/no-op 语义已交付 |
| P2-04 生命周期可靠性 | `foundation_delivered` | 恢复、取消、有限重试/退避、租约超时、dead-letter 和终态聚合已交付；OpenAI-compatible 真 SSE、完整终止校验与 JSON fallback 已加入；两条 dead-letter 路径都会先聚合 Response 证据，Provider 外部副作用仍为 at-least-once 边界 |
| P2-05 并发治理 | `pending` | 新增的每题输出预算/读取超时只控制单次请求；Provider 限流、Token/费用硬预算、完整背压、公平调度和全局并发治理仍未完成 |
| P2-06 可观测性 | `partial` | 应用 JSON 日志/correlation、健康/就绪、DB gauges 和首次 canary Run 快照已交付；resume canary 未独立追加审计事件，每题 transport request ID/返回 model/system fingerprint 未持久化，历史 counters、延迟、完整审计、全 Uvicorn/第三方日志覆盖与 Worker 主循环 liveness 未完成 |
| P2-07 验证与运维 | `partial` | 真实故障、竞争、迁移/导入和 Compose 8/8 已交付；性能/容量基线、告警与完整 Runbook 未完成 |

表中的 `foundation_delivered` 只表示本轮最小可靠垂直切片已交付，不是 Roadmap 阶段状态；Phase 2 仍为 `in_progress`。

## 验收标准与当前证据

- [x] API/Worker 任一进程重启后，未完成 Run 可安全恢复。Compose 在部分 Response 已落库时重启 API，并对真实 lease owner 执行 `SIGKILL`；peer 在租约自然过期后接管，已有 Response ID 保持、最终 15 题唯一。
- [x] 重复投递不会生成重复本地 Response、进度、分数或费用记录。显式重复 `XADD` 最终 ACK/PEL=0 且终态快照不变；这不保证 Provider 调用或外部计费 exactly-once。
- [x] 同一 Run 只有一个有效执行者；SQLite barrier、真实 PostgreSQL 双连接和双 Worker Compose 均证明并发领取只有一个 owner/token，过期接管令 token 递增并 fencing 旧 owner。
- [ ] **部分通过**：pending/running 取消已做真实 Compose 端到端验证；有限重试、超时、dead-letter 和取消/完成竞态有自动化仓储/Runner 测试，但尚未把每一种失败组合都纳入完整生产式故障演练。
- [ ] **部分通过**：既有 SQLite 数据可校验迁移到 PostgreSQL，真实 PostgreSQL 16 已验证六表导入（含 encrypted credential binary）、提交前整体回滚、双源竞争、提交确认丢失和提交后验证失败；Compose 的 `head -> 0001 -> head` 也保留一个 15 题 baseline Run 的协议证据。keyring 必须独立迁移；该 hash 不是全库快照，也不提供 PG→SQLite 自动回迁，因此完整运维范围仍为部分通过。
- [ ] **未通过（P2-05）**：Provider 速率、预算、完整背压、公平调度和全局并发治理尚不可配置，不得扩展为成本可控或过载安全声明。
- [ ] **部分通过（P2-06）**：应用日志可关联请求、Run、Question、Worker、attempt/token；`/tasks/metrics` 提供数据库当前 gauges，首次 run canary 的 discovery/request/model/fingerprint/usage/latency 证据会固化进 Run 快照。`resume` 的 canary 不会独立追加事件，每题 transport request ID、Provider 返回 model 与 system fingerprint 也未持久化；仍无历史 counters、延迟、完整任务审计和全日志源覆盖，不能追踪完整单题生命周期。
- [x] Phase 1 API/协议兼容测试与离线 Smoke 继续通过；故障 Run 保持 `llmbenchlab-protocol-v1`、15 个唯一 Response、严格总分/完成率/已回答准确率语义，所有自动化模型调用均为 Mock。
- [ ] **部分通过（P2-07）**：真实并发、API/Worker restart、Redis stop/start、两类取消、重复消息、租约过期和迁移/导入演练有可复核证据；性能/容量基线、完整告警与操作 Runbook 尚未完成。

## 已验证证据

| 验证 | 结果 |
| --- | --- |
| Web 凭据后端全量 | `make test`：`427 passed, 6 skipped`；skip 仅为未注入 DSN 的 infrastructure marker；keyring bootstrap 定向 `24 passed` |
| Web 凭据真实 PostgreSQL/Redis | PostgreSQL 16/Redis 7：`6 passed, 0 skipped`；精确测试容器已清理 |
| Web 凭据前端、Smoke 与静态门禁 | Vitest `21 passed`；Smoke `1 passed, 5 deselected`；Ruff/format/ESLint/typecheck/Vite build/lock/config 全部通过 |
| Web 凭据 Compose 与迁移/导入 | 更新后的隔离 Compose `8/8 passed`，project `llmbenchlab-p2-60f3ccdac113` 清理后容器/卷/网络均为空；PostgreSQL Alembic upgrade/check 与六表真实导入通过 |
| OpenAI-compatible 真 SSE | Adapter `50 passed`；最终 `make test` 后端 `453 passed, 6 skipped`、前端 `36 passed`；lint/typecheck/build、Smoke、Alembic、lock/Compose/diff/秘密扫描通过；全部为 Mock/stub，修复后真实 Provider 未运行 |

Compose 八场景覆盖拓扑/健康、protocol-v1 基线、执行中 API restart、实际租约 owner `SIGKILL` 后自然接管、Redis stop/start 与数据库对账、pending cancel、running cancel 加重复投递，以及 PostgreSQL `head -> 0001 -> head` 往返。详细命令、Run ID、哈希与清理记录见 [Phase 2 工作日志](../worklogs/2026-08-25-phase-2-reliable-execution-foundation.md)。没有调用真实 Provider。

2026-08-27 当前工作树已加入大数据有界消费者、快照加载期间心跳、Provider 安全边界、报告漂移和
dead-letter 部分证据回归；完整套件、真实集成和 Compose 最终门禁均已通过。
详细证据记录于 [正式评测工作日志](../worklogs/2026-08-27-complete-evaluation-workflow.md)。

## 可观测性边界

- `/live` 不访问外部依赖；`/health` 只检查数据库；`/ready` 检查数据库、Alembic head 和 Redis。Redis 不可用时返回 `503/degraded`，但数据库可用时仍接受 Run，Worker 可继续数据库对账。
- `/tasks/metrics` 只从数据库事实计算 pending/due/running/expired/cancel/retry/dead-letter/queue-error/attempt gauges。它不是 Prometheus 历史 counter、延迟分布、审计事件或容量报告。
- JSON/allowlist/correlation 只覆盖 LLMBenchLab 应用 logger，不覆盖全部 Uvicorn access、SQLAlchemy、Redis client 或其他第三方日志；秘密仍不得进入 URL。
- 首次正式 Run 的 model discovery 与 canary 证据会固化进 Run 快照；resume canary 目前只作为恢复前门禁，不追加独立审计事件。逐题 Response 也未保存 transport request ID、Provider 返回 model 或 system fingerprint，不能完成 Provider 侧逐请求追溯。
- Worker probe 只证明数据库/head/Redis 依赖能力。Redis 故障时它以 degraded 成功退出以保留 DB reconciliation；它不证明 Worker 主循环仍在领取、心跳或推进任务。
- readiness 的同步数据库检查通过 `asyncio.to_thread` 执行；HTTP 等待超时不会取消已进入线程的驱动调用，实际资源上界仍由驱动、连接和池 timeout 决定。

## SQLite→PostgreSQL 导入边界

- 导入器复制六张核心表，包括题目、参考答案、Prompt/模型快照、原始回答、错误、legacy `api_key_env` 名称及 `model_credentials` 的算法/key ID/nonce/ciphertext；必须在受信环境保护源、目标和备份。
- keyring 不属于数据库导入范围。含 stored credential 的目标必须安全取得匹配 keyring；丢失 keyring 会令密文不可恢复，同时泄漏数据库与 keyring 会令 Provider Key 可被解密。
- 含凭据目标 DSN 通过 `--target-env`（默认 `LLMBENCHLAB_DATABASE_URL`）读取；`--target` 拒绝含密码 URL，CLI 不输出 URL 或行内容。
- exit `2`：提交前失败，目标事务已回滚。exit `4`：PostgreSQL 未确认 COMMIT，目标只能是空或完整但客户端未知。exit `3`：COMMIT 已确认，提交后验证/报告失败，数据已经提交。exit 3/4 都禁止盲目重试，必须先只读核验目标与对账证据。

## 风险

| 风险 | 已有控制 | 剩余工作/限制 |
| --- | --- | --- |
| at-least-once 重复写 | 幂等键、数据库唯一约束、条件状态转换、fencing | Provider 外部调用/费用仍可能重复 |
| Redis 与数据库分裂 | 数据库唯一事实来源、commit 后通知、周期对账 | Redis 故障增加延迟；没有生产 Redis HA/ACL/TLS |
| 租约不当造成停滞/接管 | 数据库时间、可配置 lease/heartbeat、自然过期故障证据 | 需用性能/容量测试校准生产参数 |
| 并发导致限流/费用失控 | Run 内 1–4、Worker 单 Run、有限 attempts | P2-05 限流、预算、完整背压、公平调度未完成 |
| 长推理被截断或读超时 | Benchmark 建议值、1..131072/Provider 默认、1..1800 秒空闲读取快照、真 SSE、`output_truncated` 证据 | Provider 能力/默认值各异；代理缓冲、空闲或绝对总时长仍可失败；没有全局 Token/费用硬预算 |
| 数据迁移丢失/泄漏 | 只读源、空目标、事务、锁、三阶段摘要、明确退出语义 | 完整数据仍为敏感内容；无自动反向迁移 |
| stored credential/keyring 泄漏或丢失 | AES-256-GCM、随机 nonce、Model/origin AAD、共享只读 keyring、fail-closed 解密 | 无生产 KMS/身份隔离；数据库与 keyring 同时泄漏可恢复 Key，keyring 丢失则凭据不可恢复 |

## 交付物

已交付可靠基础：

- PostgreSQL/SQLite revision `0002` 的既有往返证据，以及 revision `0003`、六表 SQLite→PostgreSQL credential binary 导入/核验的完整本地门禁。
- Redis Streams 通知、独立 Worker、租约/心跳/fencing、幂等恢复、取消、有限重试与 dead-letter。
- PostgreSQL/Redis/API/Worker/frontend/一次性 migrate 的六服务 Compose、真实集成和八场景故障验收。
- ADR-0005、应用 JSON 日志/correlation、健康/就绪端点、DB gauges，以及更新后的架构/API/测试/部署/安全文档。
- ADR-0007、Web/API write-only Key、AES-GCM `model_credentials`、API/Worker 共享 keyring 与 legacy environment 兼容；这是一项可信本地安全切片，不是 P2-05/P2-06/P2-07 完成证明。
- ADR-0008、OpenAI-compatible 真 SSE、空闲 read timeout、严格 `[DONE]`、JSON fallback、三层 SSE 资源上限和跨 delta Key 脱敏；这是 Provider transport 可靠性切片，不是修复后真实链路成功或 P2-05 完成证明。
- Web Run UX 切片：Benchmark 输出/超时建议、全状态评测记录与活动轮询、100 条逐题证据分页，以及移动端导航/详情/表单修复。功能提交 `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 已 push、完整本地门禁通过；精确 SHA 无 Actions run（分支无 PR，workflow 仅监听 PR/main），不作为远程绿色或 P2-05 完成证据。

阶段剩余交付物：

- P2-05 Provider 限流、预算硬上限、完整背压、公平调度与全局并发治理。
- P2-06 历史 counters/延迟、完整审计、全日志源治理、Worker 主循环 liveness 和告警。
- P2-07 性能/容量基线、完整 Runbook 及更完整的备份/恢复演练。

## 状态

`in_progress`。可靠任务执行基础和指定真实故障证据已经交付，但 P2-05 未完成，P2-06/P2-07 仅部分完成；未满足的复选项禁止把 Phase 2 标记为 `completed`，也禁止宣称生产 HA、无限横向扩展、完整可观测性或 Provider exactly-once。

2026-08-27 的 [ADR-0006](../decisions/ADR-0006-local-real-provider-evaluation.md) 按用户优先级提前交付
可信本地正式数据/真实 Provider 垂直切片：远程 Provider 强制 HTTPS、HTTP 仅 loopback，模型发现只接受
identity 且限制为 2 MiB；Chat JSON 成功体、SSE wire/单事件/聚合 content、错误体分别限制为 4 MiB、64 MiB/1 MiB/4 MiB、64 KiB；发现阶段拒绝反射当前 Key 的模型 ID，
canary 拒绝返回不同模型，成功 content/raw usage/request ID/model/fingerprint/finish reason 在持久化前精确移除当前 Key。
该切片并增加有界题目消费者、快照加载期间心跳与连接池回收回归；它不属于
P2-05 的全局限流/预算/公平治理证据，也不改变本阶段 `in_progress` 结论。

同日的 [ADR-0007](../decisions/ADR-0007-web-provider-credentials.md) 增加 Web 只写 Key、AES-256-GCM
`model_credentials`、API/Worker 共享 keyring、legacy environment 兼容与 origin/active-Run 门禁。
自动化只使用 marker Key、固定测试 keyring、MockTransport/stub fetch 和 Mock Adapter，没有调用真实 Provider。
该切片的最终全工作树、双方言/真实导入与 Compose 本地门禁已通过；精确 SHA CI 仍按阶段 push 单独核验。
无论该远程门禁结果如何，本切片都没有交付 P2-05/P2-06/P2-07 剩余范围，因此不能改变 Phase 2 的 `in_progress` 结论。

同日功能提交 `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 还实现了 Web 长推理配置与 Run 可达性切片：数字 `max_tokens` 上限为 131,072，`null`
省略上游字段但不是无限；Benchmark 建议同步设置 1–1,800 秒读取超时并写入快照，长度截断可诊断为
`output_truncated`。主导航新增全状态评测记录，详情证据每页 100 条，并修复第五导航与关键内容的移动端
裁剪/错位。该切片没有调用真实 Provider，完整本地门禁已通过且提交已 push，但分支无 PR、精确 SHA
未触发 workflow，不能宣称远程绿色。

同日 [ADR-0008](../decisions/ADR-0008-openai-compatible-sse-transport.md) 进一步把原来只流式下载最终 JSON 的 Chat 路径改为显式 `stream:true`：request-local parser 持续消费 token/comment、可选 usage 尾块直到 `[DONE]`，并保留普通 JSON fallback。`read_timeout_seconds` 明确为下一批字节的空闲窗口，SSE wire/单事件/聚合 content 限制为 64 MiB/1 MiB/4 MiB。完整本地 Mock 门禁通过；修复前约 126 秒 524/499 与 Cloudflare 当前默认 125 秒边界高度吻合，但修复后的用户真实 Provider/代理链路尚未运行，提交和精确 SHA 远程门禁仍按工作日志收尾。
