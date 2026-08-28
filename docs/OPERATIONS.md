# LLMBenchLab Phase 2 运维手册

## 1. 适用范围与安全边界

本手册覆盖可信本地 PostgreSQL/Redis/独立 Worker 部署中由 Web/API admission 创建的 managed Run，包含 Phase 2 治理、观测、故障恢复和安全回滚。它以数据库为唯一任务事实来源，Redis 只作可丢失、可重复的低延迟通知。可信本地 `llmbenchlab-evaluate` 直连 CLI 当前仍创建 `legacy_unmanaged` Run，不得把本手册的 global/provider/model/run policy 硬边界套用于该路径。

当前 Compose 没有身份认证、TLS、正式 secret manager、PITR、HA 或内置告警发送器，只允许受信任操作者在 loopback/隔离网络使用。治理 policy、审计 API 和运维 SQL 都不应暴露公网。Provider 调用仍不是 exactly-once：Worker 在远端处理后、本地 Response 提交前崩溃，可能造成重复调用或费用。

相关文档：

- 容量方法和当前 Mock 基线：[PERFORMANCE.md](PERFORMANCE.md)
- 部署、迁移和凭据：[DEPLOYMENT.md](DEPLOYMENT.md)
- 数据库治理语义：[ADR-0009](decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[交付边界 ADR-0010](decisions/ADR-0010-phase-2-governance-delivery-boundaries.md) 和 [pre-send retry generation ADR-0011](decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)
- API 字段与错误码：[API.md](API.md)
- 安全边界：[SECURITY.md](SECURITY.md)

所有诊断使用 UTC。不得把 API Key、Authorization、Cookie、DSN、keyring、密文、nonce、Prompt、完整题目或 raw Provider body 复制进工单、告警或共享日志。

## 2. 日常检查

### 2.1 启动前

1. 确认 PostgreSQL 备份和数据库外 keyring 的备份分别可读、访问控制不同；不要把两者放进同一未保护归档。
2. 确认唯一 migration owner 已完成 `alembic upgrade head && alembic check`，API/Worker 不自行迁移。
3. 检查数据库身份和 revision，防止 API/Worker 连到不同数据库。
4. 检查 `/api/v1/ready` 的 `database/schema/queue` 分量，而不是只看 HTTP 状态或容器颜色。
5. 保存当前完整治理 policy。policy 是版本化全量文档，不支持局部 patch。
6. 核对目标 Provider 的正式账单告警和额度；本地 ledger 是保守 admission 证据，不是 Provider 账单真值。

可信 loopback 示例：

```bash
curl -sS http://127.0.0.1:8000/api/v1/ready
curl -sS http://127.0.0.1:8000/api/v1/governance/policy
curl -sS http://127.0.0.1:8000/api/v1/tasks/metrics
curl -sS 'http://127.0.0.1:8000/api/v1/tasks/history?window_hours=24'
```

### 2.2 当前 gauges

`GET /api/v1/tasks/metrics` 从 PostgreSQL 当前事实派生，不能覆盖任务状态。至少观察：

| 指标 | 含义 | 需要排查的变化 |
| --- | --- | --- |
| `managed_backlog` / `due_pending` | 受治理的活动 backlog / 当前可执行 pending | 持续增长说明 Worker、Provider、rate 或数据库成为瓶颈 |
| `running` / `active_provider_attempts` | 当前 Run lease / ledger 中 active attempt | 超过 policy 或租约失效后不下降属于完整性问题 |
| `governance_delayed` | 并发或 UTC fixed-minute RPM/TPM 暂时背压 | 超过预期窗口仍不恢复时检查 policy、数据库时钟和 Worker |
| `governance_exhausted` | lifetime request/token/cost、未知上界/价格或 overdrawn 导致终止 | 先核对稳定 reason，再决定新建 Run 或修正未来 policy |
| `overdrawn_governance_scopes` | Provider actual usage 超过 reservation | 非零立即停止扩大负载并核对真实账单/Token 上界 |
| `expired_running` | lease 已按数据库时间过期 | 短暂出现可由 reaper 接管；持续非零说明恢复失败 |
| `retry_scheduled` / `total_failed_attempts` | Run 级失败等待 / 累计失败预算 | 观察增量，不能用 `attempt_count` 代替失败数 |
| `dead_lettered` | 失败预算耗尽的 Run | 任一新增都应检查 Run audit 和已有 Response |
| `runs_with_queue_notification_error` | 曾发生 Redis publish 失败的 Run | 可由 DB reconciliation 完成，但应检查 Redis 和延迟 |
| `total_dispatches` / `total_attempts` | cooperative slice / lease acquisition 累计 | 用历史增量判断调度活动，不是错误计数 |

### 2.3 历史 counters、延迟和单 Run audit

`GET /api/v1/tasks/history?window_hours=N` 使用数据库 UTC，`N=1..2160`。它在同一 PostgreSQL `REPEATABLE READ READ ONLY`/显式 SQLite 读快照中确定窗口，逐条验证 retained audit 的 contract/hash/identity/retention 后聚合 counter，再从同一快照的 Run 时间字段给出 queue、execution 和 end-to-end p50/p95/p99。任一损坏事件使整个响应 fail closed 为 `500 audit_event_integrity_error`，不反射损坏值。每类最多 10,000 个样本，`truncated=true` 时不能把分位数当完整窗口分布。

`GET /api/v1/runs/{run_id}/audit?offset=0&limit=100` 按 `(occurred_at,id)` 稳定分页。审计是应用 append-only，不是密码学不可篡改或 WORM；数据库管理员仍能修改它。operational 事件至少保留 90 天，security/credential 事件至少 365 天。清理不在请求链路自动执行：必须先归档、核验并生成所需 rollup，再由明确维护操作清理。

建议外部监控至少实现以下症状规则，具体数值要根据目标环境基线设定：

- 立即告警：数据库不可用、schema 非 head、`overdrawn_governance_scopes>0`、`governance_integrity_error` 新增、审计/ledger 唯一性或 reserved 对账漂移。
- 高优先级：dead-letter 新增、`expired_running` 持续、settlement unknown/保守结算异常增加、真实 Provider 账单超出本地 consumed 上界预期。
- 持续性告警：`managed_backlog`/`due_pending` 多个观察窗口单调增长，`governance_delayed` 跨越应有 fixed-minute 窗口，queue lag/PEL 不下降，p95/p99 明显偏离同硬件基线。
- 容量预警：数据库连接池等待、deadlock/conflict/temp file、Redis 内存/Stream 长度或 Worker CPU/内存逼近环境阈值。

仓库不包含告警发送器或 Prometheus exporter；必须由部署环境轮询 API/数据库并负责通知。不要把“一次 `/ready=200`”当作 Worker 主循环仍在推进的证明。

## 3. 限流和预算

### 3.1 Policy 规则

`GET/PUT /api/v1/governance/policy` 只适用于可信 loopback。一次 `PUT` 原子应用一个完整、内容寻址的 policy：新内容创建新 version，已存在的相同内容则重新激活原 ID/version。Policy 内容不可变；`is_active` 和 `activated_at` 是可变的激活元数据。

- `null` 关闭该维限制；`0` 拒绝新的对应 admission；正数为上限。
- 四层 scope 为 global/provider/model/run，全部满足才获得 permit。
- RPM/TPM 使用数据库 UTC fixed-minute `[minute, minute+60s)`，不是平滑 token bucket；相邻窗口可能出现接近两倍的瞬时突发。
- backlog 在 global admission lock 内检查；满时新 Run 在提交前返回 `429 run_backlog_full`，已提交 Run 不删除。
- `question_quantum` 控制每个 lease 最多新增的 Response 数；cooperative yield 不消耗 `failed_attempt_count`。
- Run 创建时冻结全部 20 个 policy 字段、ID/hash、provider opaque scope、quantum 和恰好四个 Run override：`input_token_reservation`、`lifetime_request_budget`、`lifetime_token_budget`、`lifetime_cost_budget_usd`。每次 attempt 会重算 policy hash 并比对冻结 override/Run 列，漂移则 fail closed。

因此，**已提交 Run 的新与 active Provider attempt 都使用该 Run 冻结的 policy ID/内容，不使用此刻全局 active policy**。紧急把新 policy 的 `backlog_limit` 设为 `0` 只能阻止新 Run；旧 Run 仍按冻结 policy 继续。若必须停止已有外发，应停止/排空 Worker或逐 Run 取消，不能假定 policy 更新会回写历史快照或撤销已发请求。

### 3.2 Hard Token/cost 前置条件

启用任何 hard TPM/token budget 或 cost budget 时：

- 每个 Run 必须有显式 `input_token_reservation`；估算值不能代替 hard bound。
- 必须有显式有限 `max_tokens`；`null` 的 Provider default 在 hard Token/cost 下会以 `governance_unbounded_output` fail closed。
- cost budget 还要求 USD input/output price；缺失以 `governance_pricing_unknown` fail closed。
- policy 与 Run override 的 USD 有效范围是 `0..10000000.00000000`、最多 8 位小数；API 响应以 JSON string 保留 Decimal。PostgreSQL 使用精确 `NUMERIC(20,8)`；该公开上限使 SQLite 浮点间距低于半个 `1e-8` 存储量化单位，不应绕过 API 直接写入更大金额。
- actual usage 超出 reservation 时保存 actual、scope 标记 overdrawn 并阻止后续 attempt；已发生的远端消费无法撤销。
- usage 缺失或远端是否处理不确定时按完整 reservation 保守结算，不按零释放。

`governance_scopes` 和 `provider_call_reservations` 是预算/attempt 事实，`audit_events` 只作历史观察。API 目前没有“剩余预算”端点；如需告警百分比，应由只读运维任务根据 active policy、Run override 及 scope 的 `reserved_* + consumed_*` 计算，并与 Provider 账单独立对账。不要直接更新物化 counter。

每次 reserve/send-start/settlement/lease renew/reconcile 都会在锁内从 never-delete ledger 重算 scope 和 minute bucket；任何高、低漂移都不会被自动“修正后继续”，而是 fail closed 并在 API/Worker 边界尽力写固定 `governance_integrity_error`。若观测到该事件，立即停止扩大 admission，保留数据库快照，对账 ledger 与物化值；不要直接 UPDATE counter。

### 3.3 Policy 变更流程

1. 导出当前 GET 响应，移除只读的 `id/version/policy_hash/is_active/activated_at/created_at`。
2. 估算最坏 backlog、每题 HTTP retry、Token reservation、费用和数据库连接压力。
3. 先在 Mock/隔离环境 PUT 完整文档并跑容量/故障基线。
4. 在低流量窗口应用，记录返回的 version/hash；监控 admission、delay、exhausted、overdrawn 和 Provider 账单。
5. 回退不是直接修改旧行；把已审核的旧限制作为完整文档再次 PUT。由于内容寻址，这会重新激活原 ID/version、更新 activation metadata，并追加 applied audit。Active version 因此可以回退；审计事件记录每次激活事实，不得用 version 大小推断当前状态。

不得直接修改 `governance_policies.is_active`、Run snapshot 或 scope counter。

## 4. Backlog、defer 和 dead-letter

### 4.1 Backlog 持续增长

依次检查：

1. `/ready` 的 database/schema；数据库不可用时先恢复数据库，Redis 不是替代事实源。
2. Worker 容器和 probe，再用 `total_dispatches`/history 判断主循环是否真的推进。
3. `running`、`active_provider_attempts`、`expired_running` 和数据库连接池。
4. `governance_delayed` 及 Run 的 `governance_reason/not_before`；RPM/TPM defer 应在对应 UTC 分钟结束后恢复。
5. Provider 超时/429/5xx、SSE 空闲、反向代理和真实账单限制。
6. Redis lag/PEL；Redis 故障只增加唤醒延迟，不能解释数据库扫描也完全停滞。

不要通过删除 Redis Stream、手工改 Run 为 running、清空 ledger 或提高 `max_attempts` 来“解堵”。先减少新 admission；已提交任务由数据库公平调度和 reconciliation 收敛。

### 4.2 Governance exhausted

`governance_exhausted` 是确定性终态，不是普通 Provider 题错误。检查稳定 reason：

- `*_request_budget_exhausted` / `*_token_budget_exhausted` / `*_cost_budget_exhausted`：核对冻结 policy/Run override 和 consumed/reserved。
- `governance_input_bound_unknown` / `governance_unbounded_output` / `governance_pricing_unknown`：修正未来 Run 的显式上界或价格；旧 Run 快照不应静默修改。
- `*_overdrawn`：停止扩大真实流量，核对 Provider actual usage 和账单。
- `governance_provider_retry_exhausted`：同一 question execution generation 的 HTTP ordinal 已耗尽，不得通过 yield/restart 重置。

已有 Response 会先聚合进 failed Run，仍可用于诊断，但不能作为 completed 正式比较。

### 4.3 Dead-letter

对新增 dead-letter：

1. 读取 Run、Responses 和完整 audit 分页，确认 `failed_attempt_count/max_attempts`、最后 error、lease token 和已保存证据。
2. 检查是否为基础设施持续故障、Provider 普通失败、租约过期或取消竞态。
3. 不删除旧 Run/Response/ledger/audit，也不手工重置 attempt；修复原因后创建新 Run。仓库没有通用“重新入队 dead-letter” API。
4. 将原 Run ID、稳定错误码、policy version/hash 和新 Run ID 关联记录；不复制 Prompt/raw body 到告警。

## 5. Settlement unknown 与保守结算

每个 Provider HTTP attempt 的状态只能：

```text
reserved -> send_started -> settled_actual | settled_conservative
reserved -> released_pre_send
```

Adapter 必须先提交 `send_started` 才进入 HTTP stream。`reserved` 可证明没有完成 send-start 事务，允许 pre-send release；`send_started` 后即使 timeout、断连、Worker 崩溃或 commit acknowledgement 丢失，也必须假定 Provider 可能已处理。

成功提交的明确 pre-send release 不消耗 HTTP retry：旧 `released_pre_send` row 保留在原 generation，QuestionExecution 单调进入新 ledger generation，`next_provider_attempt` 保留该次未发送 ordinal。因而 attempt 1 未发送时下次仍是 1；若 attempt 1 已发送、attempt 2 未发送，下次仍是 2，不得重置已发送 retry。租约失效 reconciler 不再二次推进 generation，因为 takeover 已依失败恢复规则推进。

出现 `governance_settlement_unknown`、异常增加的 `settled_conservative` 或 active reservation 长时间不收敛时：

1. 立即停止扩大该 Provider 的新负载；必要时停止对应 Worker，但不要杀数据库。
2. 不手工重试同一 operation key，不删除 reservation，不把 usage 改成 0。
3. 等待当前 lease/reconciler：失效 `reserved` 应变为 `released_pre_send`；失效 `send_started` 应只保守结算一次。
4. 核对 reservation 的 state/outcome、Run audit 的 `provider_attempt_settled`/`run_lease_reconciled` 和 `active_provider_attempts` 是否回到预期。
5. 与 Provider request ID、账单和 usage 独立对账；本地 conservative 是上界策略，不证明 Provider 确实收费。
6. 数据库仍无法确认 settlement commit 时保持停止，不盲目进入下一 HTTP retry；由唯一 operation key 和 CAS 决定最终状态。

旧 Worker 失去 Run lease 后仍可 CAS 结算已经开始的远端消费，但不能写 Response 或发起新请求。这是必要的账本收敛，不是 fencing 绕过。

Run 终态、defer 或 exhaust 转换会先在短事务中提交，再做 lease ledger 对账。如 post-commit reconcile 发现完整性漂移，已提交 Run 状态保留，调用向上失败并另写最小 integrity event；不伪装成整体回滚。过期 lease takeover 的边界更严：新 owner 提交后若旧 ledger 对账失败，会撤销新 lease、聚合已有 Response 并使 Run failed/exhausted（已有取消意图则 cancelled），阻止接管 Worker 外发。

## 6. Worker 扩缩与租约恢复

### 6.1 前置条件

- 多 Worker 只支持 PostgreSQL；SQLite 始终单 Worker。
- 先看同硬件容量基线、Provider policy、数据库 `max_connections`/pool 和 Redis 连接上限。
- 增加 Worker 不会自动提高治理 limit；也不能消除 Provider fixed-minute 或 lifetime budget。
- 当前实测只覆盖最多 2 个 Worker；更高数量必须重新测量。

Compose 扩到两个 Worker：

```bash
docker compose up -d --no-deps --scale worker=2 worker
```

缩到一个 Worker：

```bash
docker compose up -d --no-deps --scale worker=1 worker
```

缩容依赖 SIGTERM grace。先观察活动 Run，等待被停止 Worker 排空；若 grace 耗尽，未完成 lease 留到数据库自然过期，由 peer 以递增 token 接管。缩容后检查 Worker health、`running/expired_running/retry_scheduled`、active reservations 和 Response 唯一性。

### 6.2 Worker crash/lease expiry

- 不得手工覆盖 `lease_owner/token/expires_at`，也不得让 peer 在数据库 expiry 前抢占。
- 保留故障 Worker 日志中的非秘密 owner/token/correlation；根据数据库时间等待自然过期。
- 接管必须令 lease token 递增；旧 token 的 Response/进度写入应被拒绝。
- reconciler 对旧 token 的 `reserved` 做 pre-send release，对 `send_started` 做一次 conservative settlement。
- takeover 提交新 lease 后必须成功对账旧 token ledger 才能继续；完整性失败会撤销新 owner 并使 Run fail closed，不能用手工重试绕过。
- 若 `expired_running` 持续、`failed_attempt_count` 达上限或 active reservation 不释放，停止新 admission 并保留数据库现场。

强制 SIGKILL 只用于隔离故障演练，不是正常缩容手段。当前真实基线证明一个实际 lease owner 被杀后 peer 可恢复 15 条唯一 Mock Response；该结果不是恢复时间 SLA。

### 6.3 固定单机资格与扩缩容门禁

`P2-local-control-plane-v1` 只限定一台可信主机、一个 API、PostgreSQL 16、Redis 7 和两个 Worker 的 Mock 控制面。计划用该 profile 支持扩容或发布决策时，先确认精确 commit 工作树完全干净、主机至少 8 logical CPU/8,000,000,000 bytes RAM、Docker 至少 8 CPU/4,000,000,000 bytes memory、PostgreSQL `max_connections >= 100`，并停止其他会争用 CPU、内存、Docker 或数据库的负载。不得注入真实 Provider Key；该 qualification 不测网络模型。

Compose profile 固定每进程 `pool_size=5`、`max_overflow=5`，因此一个 API 加两个 Worker 的应用连接上界为 `(1 + 2) × (5 + 5) = 30`；在 PostgreSQL 100 连接门槛下至少另留 20 个连接给迁移、探针和运维。Worker 固定 `lease/heartbeat/poll=30/10/1s`、`max_attempts=3`、retry backoff `base/cap=1/30s`，不能把日常 `phase2-capacity` 的快速 `6/2/0.15s` 故障参数当作正式恢复时间证据。

执行并保留 aggregate 与每个 child 的 SHA-256：

```bash
make phase2-slo
```

该命令串行运行 1 次 warm-up 和 5 次 measured trial。只有所有轮都满足预登记吞吐/延迟/scale/recovery 门槛，且独立 ledger→scope/minute projection、唯一性、公平性、fault、Redis PEL/lag 和 cleanup 硬门禁全部为零漂移，容量模型才会输出 `qualified`。吞吐由 `completed_questions / wall_duration_seconds` 重算；双/单 Worker scale 必须使用同一 trial 的配对 ratio，不能从不同运行挑选最优值拼接。失败 suite 原样保留，不删除异常轮或仅重跑失败 cell。

合格容量模型使用双 Worker吞吐 one-sided 95% LCB `mu_lcb`、安全系数 `0.70`、15 题/Run 和 ledger 实际 Provider attempt/题估计安全 Run 到达率；如果任一 SLO 失败或 LCB 非正，输出为 `not_qualified`，不得人工套公式产生容量数字。模型仍只适用于相同 Mock 数据、硬件、容器资源和 commit。扩到第三个 Worker、改变 pool/retry/lease、切换真实 Provider 或把服务移到多主机都必须重新设计/测量，不能沿用该结论。

raw 与 aggregate evidence 都在 Git 忽略的 `.pytest_cache/artifacts/phase2-slo/`；不得提交或默认上传。aggregate 虽只保留 allowlist，仍包含 commit/hash、资源指纹、SLI 和运维结果，应按内部证据保护。child 超时/中断后有 420 秒 scoped cleanup 窗口；命令返回后仍应核对 artifact 中容器、volume、network 全部为 0。GitHub-hosted CI 只验证 validator/统计/失败路径，不是绝对性能复测；正式记录还必须关联同一精确 SHA 的 required CI。

## 7. Redis 故障与恢复

Redis 不可用时的正确状态是：

- `/ready` 返回 `503/degraded`、`queue=unavailable`，但数据库/head 正常时 `accepting_runs=true`。
- API 先提交 Run，通知失败记录 `queue_notification_unavailable`，不能回滚或删除 Run。
- Worker 通过 PostgreSQL 扫描继续领取；延迟可能增大。
- Redis 恢复后 consumer group 可重新初始化；重复/延迟消息只触发数据库 no-op/ACK。

处置顺序：

1. 确认 PostgreSQL 正常；不要因为 Redis 故障恢复数据库备份。
2. 检查 Redis 进程、磁盘/AOF、内存和网络；AOF 不是任务备份。
3. 保持或恢复 Worker 的数据库 reconciliation，观察 pending 是否下降。
4. 恢复 Redis 后检查 `/ready`、Stream `lag`/PEL 和新 Run 是否能通知。
5. 不为追求 PEL=0 删除 Stream 或消费组；数据库事实允许安全重复，但真实 Provider 调用仍非 exactly-once。

## 8. PostgreSQL 故障与恢复

PostgreSQL 不可用时没有可替代事实源。API 不应接受新 Run，Worker 不能安全领取、续租、结算或写 Response。

1. 停止 API/Worker 和用户入口，保留故障数据库、WAL 和日志现场。
2. 按部署平台恢复经过验证的 PostgreSQL 备份；仓库本身没有生产备份/PITR 工具。
3. 单独恢复匹配的 keyring；不要把 keyring 内容打印到验证日志。
4. 核对数据库身份、完整性、外键、Alembic revision 与 expected head；只有一个 migration owner 可以前进升级。
5. 在 API/Worker 停止时检查 active Run/lease/reservation。不要手工释放 `send_started`；让受支持的 reconciler 保守收敛。
6. 先启动一个 Worker，小流量验证 claim/fencing/settlement/Response；再按测量结果扩容。
7. 检查 `/ready`、task gauges/history、Run audit、queue lag 和 Provider 账单。

若恢复点早于某些 Provider 外发，数据库无法知道备份之后的远端消费；必须以 Provider 账单和 request ID 独立核对。Redis 可以重建通知，不能用来补造丢失的数据库事实。

## 9. 0004 安全回滚

revision `20260827_0004` 增加 policy、scope、minute bucket、question execution、Provider attempt ledger、typed audit、Run governance/fairness 字段及 Response Provider metadata。downgrade guard 在第一条 DDL 前检查数据损失风险。

任何以下事实存在时，`0004 -> 0003` 都必须拒绝：

- governance policy/scope/minute bucket/question execution 任意行；
- Provider call reservation 或 audit event 任意行；
- Run governance/failure/fairness/override 新字段含不可丢证据；
- Response Provider metadata 已保存。

应用首次治理 bootstrap 就会创建 policy/audit，正常使用还会创建 ledger。因此，已使用的数据库不能把 `alembic downgrade 20260827_0003` 当普通代码回滚。

安全流程：

1. 停止新 admission、API、全部 Worker 和 CLI，等待或对账 active reservation。
2. 创建并独立验证 PostgreSQL 备份，同时单独备份 keyring；记录 0004 revision 和 evidence 摘要。
3. 归档并核验 ledger/audit/Provider metadata。仓库没有自动“清空后回滚”工具；不要复制测试中的清理方式到生产。
4. 优先做向前修复，或恢复明确的 pre-0004 备份并接受备份点之后的 Run/ledger/audit 数据回退；保留 post-0004 只读归档用于账单/审计。
5. 只有经过批准的数据生命周期流程确认所有 0004-only 事实可删除时，才能另行设计并评审清理；guard 通过只说明表面为空，不替代归档核验。
6. 回滚后以唯一 migration owner 检查 revision，再启动 API/Worker。旧应用的 head gate 会拒绝它不认识的 revision，不能边运行边降级。

隔离空数据库允许 `0004 -> 0003 -> 0004` 往返；真实验收同时证明 populated 数据库 downgrade 被拒绝且 revision/core protocol hash 不变。详见 [PERFORMANCE.md](PERFORMANCE.md) 和 Phase 2 acceptance evidence。schema downgrade 不是 PostgreSQL→SQLite 回迁。

## 10. 事故收尾清单

- [ ] Run/Response 状态只由 PostgreSQL事实解释，没有手工覆盖。
- [ ] Redis PEL/lag 已观察，未通过删除权威数据“修复”。
- [ ] active reservation、scope/minute reserved 和 overdrawn 已对账。
- [ ] scope/minute 的 active/reserved/consumed/overdrawn 已从 never-delete ledger 重算，无高/低漂移；如有 integrity event 已保留现场而非手工改 counter。
- [ ] duplicate operation/audit key 为 0；同 key 不同 payload 按完整性事故处理。
- [ ] dead-letter、conservative settlement、failed attempt 和 Provider 账单已关联。
- [ ] policy version/hash、Run frozen snapshot 和协议/data hash 已记录。
- [ ] 日志、工单和 evidence 不含 Key、DSN、keyring、密文或原始 Provider body。
- [ ] 修复后重跑目标测试、真实 Compose acceptance 和同硬件容量基线。
- [ ] 清理只作用于明确的隔离测试 project；生产 evidence/backup 按保留策略归档。
