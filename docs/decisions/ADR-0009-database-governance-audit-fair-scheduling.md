# ADR-0009：数据库权威的执行治理、审计与公平调度

- **Status**: Accepted
- **Date**: 2026-08-27
- **Deciders**: LLMBenchLab maintainers
- **Scope**: Phase 2 P2-05/P2-06/P2-07；Provider attempt admission、额度账本、背压、公平调度、审计与容量验证
- **Related requirements**: `docs/NEXT_TASK.md` P2-05、P2-06、P2-07
- **Supersedes**: ADR-0005 中“`attempt_count < max_attempts` 同时限制所有成功领取次数”的局部语义；其数据库事实来源、租约/fencing、Response 幂等和 Provider non-exactly-once 决定继续有效
- **Amended by**: [ADR-0010](ADR-0010-phase-2-governance-delivery-boundaries.md) 对可信本地 CLI、Provider metadata redaction event、历史延迟来源和 credential origin payload 的交付边界修正；[ADR-0011](ADR-0011-confirmed-pre-send-release-retry-generation.md) 明确已确认 `released_pre_send` 以新本地 generation 保留 ledger、但不消耗实际 HTTP retry；其余决定继续有效

## Context

现有系统能可靠领取和恢复 Run，但每个 Worker 会持有一个 Run 直到全部缺失题完成；Run 内并发 1–4 不是跨 Worker 的全局/Provider/Model 限额。OpenAI-compatible Adapter 又在 `generate()` 内进行有限 HTTP retry，因此只在 Runner 外层计一次请求会漏记真实 attempt。usage 与价格可能未知，Worker 也可能在 Provider 已处理请求、本地 Response/ledger 提交前崩溃。

这些事实带来四个必须同时解决的问题：

1. 并发、RPM/TPM 和累计预算必须在多个 Worker 间原子裁决，Redis/进程内 semaphore 不能成为第二事实来源。
2. reservation、Provider attempt、settlement 之间不存在跨 Provider/数据库事务；未知结果不能按零释放，也不能宣称账单 exactly-once。
3. 长 Run 若不主动让出，即使 claim 顺序公平也会长期占用 Worker。
4. 当前日志与 gauges 没有持久、幂等、可关联的历史事件，无法证明 retry/recovery 没有 double-count。

## Decision drivers

- 不改变 protocol-v1 评分和逐题 Response 幂等语义。
- 在并发 API/Worker、重启、租约接管和 Redis 故障下不突破本地 admission policy，也不永久占用额度。
- 每个实际 HTTP attempt 都可追踪；未知 usage/价格或提交裂缝显式保守处理。
- 给有限 backlog 中每个可执行 Run 一个可说明的等待上界。
- 审计不包含秘密、题目或原始输出，并明确不是 WORM/不可篡改账本。
- SQLite 保持单 Worker兼容，真实多 Worker证明使用 PostgreSQL。

## Decision

### 1. 唯一事实来源、时钟和 scope identity

- PostgreSQL/SQLite 数据库继续是任务和治理唯一事实来源。Redis 只通知；内存 semaphore、日志和 metrics 均不能覆盖 reservation/settlement。
- 数据库当前时间裁决 fixed window、lease 和 reservation expiry；本地时钟只控制 sleep/poll。
- 每次 attempt 同时属于四个 scope，并固定按 `global → provider → model → run` 排序锁定，禁止调用方自定义锁序。
- global key 固定为 `global`；model/run key 使用内部 UUID；provider key 使用冻结 Provider type 与规范化 origin 的 SHA-256 opaque identifier。数据库、API、审计和日志不保存 provider credential、Authorization 或带 userinfo/query/fragment 的 URL。
- SQLite 在状态转换前使用 `BEGIN IMMEDIATE`；PostgreSQL 先安全 upsert 缺失 scope，再按固定顺序 `FOR UPDATE`。完整锁序固定为 `global/provider/model/run scope → UTC minute bucket → attempt reservation → EvaluationRun`；网络期间绝不持有数据库锁。global scope 会串行化所有新 attempt admission，这是当前正确性优先的明确吞吐成本，必须进入容量基线，不能提前绕过。
- attempt ledger 是 never-delete、单向可变状态账本，不冒充 append-only；真正 append-only 的只有 audit event。scope/minute counters 是与 ledger 状态转换同事务更新的物化聚合，可由 ledger 重算而不是第二事实来源；检测到漂移必须 fail closed 并写完整性事件，不能静默修补后继续发请求。

### 2. Policy 与组合顺序

- governance policy 是数据库中的版本化事实；只有一个 active policy，包含 global/provider/model 默认的并发、fixed-minute requests/tokens、backlog、question quantum 和累计预算。配置文件/环境变量只能通过显式 policy-apply 事务创建新版本，API/Worker 不得各自以内存环境值裁决；找不到 Run 冻结的 policy version 时 fail closed。
- `null` 表示该限制关闭，数值 `0` 表示紧急拒绝全部新 admission；正整数/Decimal 才是实际上限。这样既能关闭某维度，也能在不改代码时 stop-the-world。
- global 与可选 Run lifetime request/Token/cost budget 可配置。Run 级 override 与 active policy ID/hash 随 Run execution snapshot 冻结；进程重启、policy 更新或 Model 修改不能改变历史 Run 的边界。
- 四层条件全部满足才能取得 permit；有效上限是所有适用 scope 中最严格的一个，不允许某层成功掩盖另一层拒绝。
- admission 先检查有限 backlog，再持久化 Run。backlog 已满时 API 在事务提交前返回稳定 `429 run_backlog_full`；已成功提交的 Run 永远不因随后 Redis、rate 或 budget 背压而删除。
- Run 保持既有 `pending/running/completed/failed/cancelled` 生命周期，并增加非终态治理字段：`governance_status`、`governance_reason`、`governance_not_before`。瞬时并发/rate 饱和使 Run 回到或保持 `pending/delayed`；确定性 lifetime budget 耗尽使 Run 聚合现有证据后进入 `failed`，错误码为稳定治理代码。0004 以前创建且快照中没有治理 policy 的 Run 明确标记 `legacy_unmanaged` 并按旧边界收敛，不能在执行中途静默套用新预算；升级后的新 Run 必须是 managed。

### 3. 原子 reservation、settlement、release

每个逻辑 Provider HTTP attempt 使用唯一键 `(run_id, question_id, execution_generation, provider_attempt)`；`lease_token` 另作该 reservation 的 owner/fence 字段，不是 retry generation。每题另有持久化 execution row，保存 generation、下一 Provider attempt ordinal、首次 attempt 时间和 retry-not-before。cooperative yield 延续同一 generation/ordinal，只有真实 Run 级失败或异常租约恢复才开始新 generation；因此 defer/resume 不能重置 protocol-v1 的有限 HTTP retry 预算。

`reserve` 与 `send_started` 都必须验证当前 Run 的 owner/token/未过期 lease，防止失租 Worker 发起新请求；随后在一个数据库事务内：

1. 按固定锁序锁定四个 scope，先对账与这些 scope 相交、且 owner lease 已失效的 active reservation。
2. 锁定/创建当前数据库分钟 bucket。
3. 检查四层 active permit、RPM/TPM、lifetime request/Token/cost budget、该题剩余 retry ordinal 和 overdrawn 标志。
4. 同时写入一条 `reserved` attempt ledger，并增加四层 active/reserved 聚合。

Adapter 必须先在独立数据库事务把 ledger 从 `reserved` 转为 `send_started`，该事务成功后才进入 `client.stream`。因此，仍为 `reserved` 的条目或 mark-send 失败可确认没有开始外发并 `released_pre_send`，不消费 request/Token/cost；一旦 `send_started`，系统保守认为 Provider 可能已经处理。成功且 usage 完整时按实际 input/output Token 和冻结价格 `settled_actual`，释放未使用预留。以下情况一律转为 `settled_conservative`，把完整请求、Token 和费用预留结算为 consumed，不得按零释放：

- transport timeout/断连、retryable HTTP 错误或无法确认 Provider 是否处理；
- Provider 成功但 usage 缺失；
- Worker/进程在 `send_started` 后、本地结算前崩溃；
- lease/reservation 到期、数据库 commit acknowledgement 不确定。

唯一键和行状态转换使本地 replay 不重复结算同一 ledger；同一 execution generation 的新 lease 继续使用尚未消费的下一 ordinal，不会复制逻辑 attempt。`send_started` 后的终态结算只对 attempt row 做 reservation ID/state CAS，不再要求 Run lease 仍有效：旧 Worker 的实际外部消费与 takeover reconciler 的保守结算竞态中，先把该 row 置为终态者获胜，后者幂等 no-op；Response/进度写入仍严格受 Run fencing。数据库无法确认 settlement commit 时 Adapter 停止当前 retry，由唯一键和 reconciler 裁决，不能盲目发起下一 attempt。这个保证仍不是 Provider exactly-once 或账单真值。

fixed-minute window 以数据库 UTC epoch 向下取整，区间为 `[minute, minute + 60s)`；reservation 永远归入 `send_started` 所在窗口，长调用结算不得移动到当前分钟。`reserved` 计入临时预留，`send_started` 至少消费一个 request；明确 pre-send release 才可释放。usage 只有部分维度已知时，已知维度按 actual、未知维度按完整预留结算。fixed window 允许相邻边界出现接近两倍的瞬时突发，不冒充平滑 token bucket。rate defer 的 `governance_not_before` 精确设置为当前 window end，避免忙循环。

### 4. 有限 Token/费用上界与 fail-closed

- 普通观测可记录冻结渲染消息 UTF-8 长度及协议开销估计，但任意 OpenAI-compatible tokenizer/chat template 可能不同，该估计不是可证明的 Token 上界。
- 启用 hard Token budget/TPM 时必须在 Run policy 中冻结显式 `input_token_reservation`，并有显式 `max_tokens` 输出上界；任一缺失分别以 `governance_input_bound_unknown` 或 `governance_unbounded_output` 在调用 Provider 前 fail closed。nullable `max_tokens` 在未启用相应 hard Token/费用约束时继续保持 Provider-default 语义。
- 启用 hard cost budget 时必须同时有上述有限 Token reservation、明确货币（当前只支持 USD）和 input/output price；缺失价格以 `governance_pricing_unknown` fail closed。所有费用预留按数据库 `Numeric(20,8)` 的最小单位向上取整，不能向下舍入突破预算。
- usage 缺失按完整预留结算。Provider 若报告超过预留的实际 usage，ledger 保存实际值、scope 标记 `overdrawn` 并阻止后续 attempt；系统不能撤销已发生的外部消费，也不得裁剪证据来伪造未超额。

### 5. Adapter attempt hook

- Adapter 公共生成结果和 protocol-v1 retry 次数不变，但 OpenAI-compatible 的每次 `client.stream` 前必须调用可选三阶段 governance controller（reserve、mark-send-started、finish）；Mock/Stub 用同一接口模拟单 attempt。`generate` 以只读 per-question context 传入 execution generation/next ordinal，controller 不保存在共享 Adapter 的可变“当前请求”字段中。
- hook 接收的上下文只含 run/question/model/provider opaque scope、lease token、attempt ordinal 和数值预留；不得接触 API Key、headers、请求正文或原始响应。
- permit 暂不可用抛出专用 `governance_deferred`，Runner 不生成 0 分 Response，而是 fenced cooperative defer；永久 budget/pricing 拒绝抛出专用 exhausted 错误并终止 Run。这两类控制流不得继承 `AdapterError`。普通 Provider/解析错误继续按 protocol-v1 逐题计零。上一 HTTP attempt 的 release/actual/conservative 结算必须确认后，Adapter 才能 backoff 并进入下一 retry；取消后的短结算使用受限 shield，失败则由数据库对账。
- 可信本地 CLI 的 model discovery/canary 使用没有 question 的 synthetic operation key，但仍经过 global/provider/model scope；canary 创建 Run 前的累计消费记录在同一 policy ledger。任何明确排除的预检必须在费用确认中单列，不能把“所有 Provider 请求受治理”作为更宽声明。

### 6. 租约、崩溃与恢复

- active attempt reservation 绑定 run/owner/lease token。reconciler 必须联结当前 Run lease：只在对应 token 已不再是当前未过期 running lease（自然到期、接管、让出或终态）时回收，不能用独立固定 30 秒 attempt timeout 误杀仍在持续 heartbeat 的长 SSE。旧 token 不能 reserve 或开始新请求，但可按上一节 CAS 结算既有 `send_started` ledger。
- acquisition、Worker reconciliation 和过期 Run reaper 都可幂等对账已失效 token 的 reservation：`reserved` 证明尚未完成 send-start 事务，按 pre-send release；`send_started` 按完整预留保守结算；两者都释放本地 admission permit，并写唯一 audit event。Worker 崩溃后远端请求可能继续运行，释放本地 permit 不能证明 Provider 侧幽灵请求已经停止；若永久占用则又违反可恢复性，因此本 ADR 的并发硬限额只约束数据库已准入且仍受有效本地 lease 管理的 attempt，不承诺崩溃后的真实 Provider 在途并发上界。
- cancel、dead-letter、retry、cooperative yield 和终态前均执行相同对账。Redis stop/start 不改变 ledger。
- `attempt_count` 改为“成功取得 lease 的总次数”，保持单调但不再受 `max_attempts` 上限约束；新增 `failed_attempt_count` 只统计 Run 级失败/租约异常，并由 `max_attempts` 限制。cooperative yield 不增加失败计数。0004 以前不存在 cooperative yield：旧 `pending` Run 以 `min(attempt_count, max_attempts)` 初始化；旧 `running` Run 的当前 lease 尚未失败，以 `min(max(attempt_count - 1, 0), max_attempts)` 初始化，并仅在该 lease 真失败/过期时原子加一；历史 `attempt_count` 保留原值，不为旧 active Run 静默增加 retry 预算。

### 7. 公平调度与背压

- Worker 每个 lease 最多新增 `question_quantum` 条 Response；quantum 按真实新插入 Response 数而不是 claim/HTTP attempts 计算。一 slice 的 Provider attempt 理论上限是 `question_quantum × (max_retries + 1)`，并考虑 slice 开始时至多 Run concurrency 个已启动题。仍有缺失题时 fenced 地让出为 `pending`，记录 `last_scheduled_at`/`dispatch_count`，不视为失败。
- 可执行 Run 按内部优先级排序：取消/完整证据收敛与过期 reservation 对账最高；随后按 `coalesce(last_scheduled_at, created_at)` 最老优先，再以 created/id 稳定破平。新 Run 不能反复越过更早等待的已让出 Run。
- backlog 检查在 global admission scope 锁内原子执行，并明确统计所有 pending/running managed Run。由于 backlog 有显式有限上限，在没有持续基础设施失败的前提下，一个可执行 Run 最迟在当时 backlog 中其他可执行 Run 各获得一个 slice 后获得服务；这是服务顺序上界，不是墙钟上界，单题 SSE 和 Provider/rate/budget 阻塞时间不计入。
- 某题遇到 governance defer 后停止启动新题，但必须 drain 已启动的题再让出；已取出但未获 permit 的题不生成 Response，下一 slice 从缺失 Response 重新加载。defer 不增加失败计数，永久 budget/pricing exhausted 聚合现有证据后直接进入治理失败，不伪装成 retry dead-letter。
- Redis delivery 仍可优先唤醒 Worker，但不能绕过数据库最老可执行 Run；重复消息只触发 no-op/ACK。

### 8. 历史 metrics 与 append-only audit

- 新增应用 append-only `audit_events`。`event_key` 唯一并由状态转换的稳定 ID 生成；同一事务重放不会 double-count。冲突时必须比较既有 event type 与 payload hash，同 key 不同事实是完整性错误，不能 `ON CONFLICT DO NOTHING` 静默吞掉。
- 事件只使用固定 `event_type`、UTC DB time、correlation/run/model/question/worker/attempt/lease 等非秘密列，以及受 allowlist 约束的短枚举/数值 payload。禁止任意异常文本、Prompt、raw response、Provider body、URL、Key、密文、nonce 或 keyring 内容。
- 预算 attempt/token/cost counters 以 never-delete 单向状态 ledger 为事实，audit 只是历史观察；queue/claim/retry/cancel/dead-letter counters 与 queue、slice execution、Provider attempt、end-to-end latency 从 retained typed events 聚合。过期 reconciliation 的 Provider 结束时间标为 censored/unknown，不混入正常 Provider latency。现有 `/tasks/metrics` gauges 保留并明确是当前事实。
- 单 Run audit 分页按 `(occurred_at,id)` 稳定排序；历史 metrics 使用并声明不超过 retention 的有限查询窗口。事件带 `retention_class` 与 `expires_at`：普通操作事件至少 90 天，credential/security 事件至少 365 天。自动删除不在请求链路执行；Runbook 只允许先归档/核验并生成保留期 rollup 后再由显式维护操作清理，否则历史查询会按定义缩短。成功 heartbeat 不逐次写高基数事件，只审计 claim、续租失败、失租、接管和汇总。
- append-only 是应用行为约束，不是密码学不可篡改、WORM 或对数据库管理员的防护。数据库管理员仍能修改数据；文档和 API 不得宣称完整性证明。
- credential create/replace/source switch、origin rejection、active-Run conflict、key ID 与 decrypt failure 只记录 model ID、credential source、opaque origin/key ID 和稳定错误码。key ID 是非秘密标识，但 key material 绝不进入事件。被业务事务回滚的 rejection 使用按服务器 request ID 去重的独立短审计事务；进程在拒绝响应前崩溃仍可能留下缺失事件，文档不得声称跨进程 exactly-once 审计。

### 9. Provider 响应元数据

- EvaluationResponse 可保存已脱敏的 Provider request ID、returned model、system fingerprint、finish reason 与 HTTP attempt count；不保存任意 raw usage 对象。
- 若任一字段等于当前 Key 或不满足长度/字符安全边界，保存 `null` 并写非秘密 redaction 事件。报告可导出这些稳定字段，但不得导出 governance scope 内部密钥材料（其本身也不含凭据）。

### 10. 容量验证与支持边界

- 真实基线必须使用隔离 PostgreSQL 16、Redis 7、至少两个独立 Worker 和纯 Mock Adapter；记录 commit、OS/CPU/内存、容器配额、Run/题数、并发、吞吐、p50/p95/p99、错误/重试和 DB/queue 压力。
- 基线脚本必须覆盖 overload/backpressure、Worker scale、lease expiry、Redis stop/start、duplicate delivery 与 audit/ledger 对账，并输出内容脱敏的机器可读 evidence。
- 结果只描述该硬件/commit/配置，不是生产 SLO/SLA、无限扩展或真实 Provider容量。

## State invariants

- 四层 scope 以固定顺序在一个事务内裁决；成功 reservation 必须在四层同时可见，失败不得留下部分计数。
- 每个 ledger 状态只能 `reserved → send_started → settled_actual|settled_conservative`，或在明确未发送时 `reserved → released_pre_send`；终态不可再次改变。
- scope/minute active/reserved/consumed 物化聚合等于 ledger 可重算事实；过期 `reserved` 释放，过期 `send_started` 保守结算，两者都不永久占用本地 admission 并发。
- hard limit 检查使用 `consumed + reserved + new reservation`，不能只看已结算值；这是显式 reservation 的本地 admission 边界，不是 Provider tokenizer、账单或崩溃后幽灵请求的全局证明。
- audit event key 唯一；状态重放不增加第二个 counter 事件，同 key 不同 payload hash 必须失败。
- cooperative yield 不增加 `failed_attempt_count`；真实 Run 级失败/租约异常才增加。
- 已提交 Run 不因 Redis 或瞬时 governance 背压丢失；终态仍从 Response 事实聚合。
- 任何治理/审计/metrics/queue/snapshot/report 路径都不得包含 credential 明文或加密材料。

## Alternatives considered

### Redis semaphore/token bucket 作为权威

延迟低，但与 Run/lease/Response 数据库形成双重事实，Redis 丢失或恢复会重置预算，无法满足持久恢复，因此拒绝。Redis 后续只能缓存可由 DB 重建的提示。

### 只在 `adapter.generate()` 外层计一次

实现简单，但漏掉 Adapter 内 HTTP retry，无法给 RPM/请求预算真实上界，因此拒绝。

### 未知 usage 按零或平均值结算

吞吐更高，但会在最危险的超时/崩溃窗口释放可能已经消费的额度，违反硬边界，因此拒绝。

### 继续让 Worker 完整执行一个 Run

减少租约切换，但大型 Run 可让低流量来源长期等待，无法证明公平上界，因此拒绝。

### 把 audit 等同不可篡改账本

可营销但不真实；同一数据库管理员能够更改任务和事件。选择明确的应用 append-only 与完整性边界。

## Consequences

### Positive

- 多 Worker 的并发/rate/budget 决策可原子恢复，Redis 故障不重置额度。
- HTTP retry、未知 usage 和 crash 裂缝有保守、可追踪的本地账本语义。
- 长 Run 不能无限独占 Worker，有限 backlog 下有可说明的调度上界。
- 任务与 credential 生命周期有幂等历史事件和延迟分布，能验证 double-count。

### Negative

- 每个 Provider attempt 增加数据库事务与四层 scope 锁，吞吐会下降，必须用容量基线校准。
- fixed-minute window 在边界处允许相邻窗口突发，不等同平滑 token bucket。
- unknown 保守结算可能低估剩余额度；这是安全取舍，需要人工账单对账。
- 新 migration、importer、审计保留和公平 slice 显著增加测试/运维复杂度。

## Validation

- PostgreSQL 多连接 barrier 同时申请相同/不同四层 scope，断言限额从不超出且无部分 reservation。
- 模拟 429/5xx/transport retry，断言每个 HTTP attempt 唯一记账；成功 actual、`send_started` 未知 full reserve、pre-send release 均正确，且结算完成后才发起下一 retry。
- kill Worker 于 reservation、attempt、Response commit 各裂缝，租约过期后断言 active permit 释放、unknown 只结算一次、接管可继续。
- 并发提交达到 backlog 上限返回稳定 429；已提交 pending 在 Redis 中断和 rate delay 后恢复。
- 持续添加同 Provider 高流量 Run，同时保留另一 Provider/Model Run，断言后者在 backlog×quantum 文档边界内获得 slice。
- cancel/retry/dead-letter/duplicate delivery/commit-uncertain 重放不 double-count ledger/audit。
- credential 事件与所有 API/report/queue/log/snapshot 做 marker Key/envelope/keyring 泄漏回归。
- SQLite/PostgreSQL 0004 upgrade/downgrade/upgrade/check、SQLite→PostgreSQL 新表 digest 和真实基础设施全量回归。
- 双 Worker Mock 负载输出可复核 p50/p95/p99 与资源环境；不调用真实 Provider。

## Security and privacy impact

- provider scope 只保存 opaque hash；audit payload 不接受任意文本或秘密字段。
- request ID/model/fingerprint 仍可能是 Provider 控制字符串，必须先经过现有 exact-Key redaction和新长度/字符限制。
- ledger 暴露请求/Token/费用元数据，数据库备份和 audit API 仍应视为敏感运维数据；当前无鉴权，因此只允许可信 loopback。
- keyring 保持数据库外；数据库与 keyring 同时泄漏的风险不变。

## Rollback and migration

1. 升级前停止旧 API/Worker并备份数据库与独立 keyring；仅 Alembic 拥有 schema。
2. 0004 增加治理/审计表、Run/Response 字段和约束；旧终态 Response/评分不改写。
3. 旧 Run 不补造历史 reservation/audit；缺 governance snapshot 的 pending/running Run 标为 `legacy_unmanaged` 并按旧行为收敛。不得调用真实 Provider进行迁移验证。
4. 回滚前停止 admission，等待或对账所有 active reservation，并确认没有依赖新字段的 pending/running Run。
5. downgrade 若发现任何未显式归档/清理的 attempt ledger、audit event、active reservation 或新治理状态的 active Run，必须在 DDL 前拒绝；不得静默删除费用/审计证据。隔离 migration roundtrip 测试可在核验 digest 后显式清理测试数据，但生产 Runbook 不自动删除。
6. 关闭治理只能停止新的 reservation；已有 ledger/audit 保留。Redis 清空不能作为回滚。
