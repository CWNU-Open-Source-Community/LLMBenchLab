# ADR-0015：受控可观测性、Worker 进展与审计保留

- **Status**: Accepted
- **Date**: 2026-08-28
- **Deciders**: LLMBenchLab maintainers
- **Scope**: Phase 2 P2-06 metrics exporter、告警、Worker 主循环进展与 audit retention
- **Amends**: [ADR-0005](ADR-0005-durable-task-execution.md) 的任务可观测性、[ADR-0009](ADR-0009-database-governance-audit-fair-scheduling.md) 的 typed audit 保留流程，以及 [ADR-0010](ADR-0010-phase-2-governance-delivery-boundaries.md) 的恢复指标边界
- **Preserves**: PostgreSQL/数据库事实来源、Redis 非权威通知、`llmbenchlab-protocol-v1`、write-only Provider Key、Mock-only 自动化和可信 loopback 部署边界

## Context

Phase 2 已经交付数据库派生的 `GET /api/v1/tasks/metrics`、在同一数据库读快照内校验 retained audit 的 `GET /api/v1/tasks/history`、稳定分页的 Run audit、Provider/credential 非秘密事件和 Run latency。它们仍有三个正式闭环缺口：

1. 运维环境没有固定低基数、可被 Prometheus 抓取的 exposition，也没有仓库内可验证的告警规则与响应 Runbook。直接把 rolling-window audit count 标成 counter、把数据库字符串变成 label 或在高频 scrape 中无限扫描 retained event，都会制造错误语义或数据库放大。
2. `worker_probe.py` 是独立进程执行的数据库/head/Redis capability probe。它不能证明被探测 Worker 的主 event loop 正在 scan、claim、续租或持久化进展；读取任意 peer 的状态再给当前容器报健康也会掩盖卡死。
3. `audit_events` 已经冻结 `operational >= 90d`、`security >= 365d` 的行内 expiry，但没有可执行的 archive、离线 verify、精确 delete、restore 或 commit-outcome 对账。按 cutoff 宽泛删除、复用业务重放的宽松幂等比较，或把普通文件 hash 宣称为 WORM，都会破坏审计证据。

P2-06 只闭合以上应用级可观测性与 audit archive。PostgreSQL 与数据库外 keyring 的配对 backup/restore、PITR/RPO/RTO、Redis 灾难恢复和完整故障矩阵仍属于 P2-07；本决定不能被用来宣称生产 HA、不可篡改存储或 Provider exactly-once。

## Decision

### 1. 事实来源与公共边界

- 数据库 UTC、Run/lease/ledger、typed audit 和 Worker progress 行继续是唯一权威事实。Redis ping、Prometheus、告警状态、进程内 single-flight 与日志都不是第二状态机。
- 新增 `GET /api/v1/metrics/prometheus`。成功响应固定为 Prometheus text exposition `0.0.4`：`text/plain; version=0.0.4; charset=utf-8`、UTF-8、LF、唯一末尾换行和 `Cache-Control: no-store`。
- Endpoint 不接受查询参数、动态窗口或内容协商；不经 HTTP 递归调用现有 JSON endpoint。现有 `/tasks/metrics` 与 `/tasks/history` 保持兼容并与 exporter 复用同一 snapshot collector。
- API 与 exporter 仍无认证，只允许可信 loopback/受控内部网络；CORS 不是安全边界。仓库不部署 Prometheus、Alertmanager、OTel collector 或通知发送器。
- exporter 不输出 Run、Model、Provider、Worker、Question、reservation、policy、request、correlation ID、origin、URL、hash、错误文本或其他用户控制字符串。唯一 labels 是本文列出的固定 enum。
- 所有 series 都声明为 `gauge`。当前 DB 值和 rolling-window count 都可能下降，不得使用 `_total`、`counter`、`rate()` 或 `increase()` 伪造单调性。

### 2. Exporter 快照、窗口和失败语义

一次成功 collection 使用一个数据库读快照和一个 DB `current_timestamp`：

- PostgreSQL：`REPEATABLE READ READ ONLY`；SQLite：首个读查询前显式 `BEGIN`。
- current Run/governance gauges、expired lease age、audit window、Run latency 与 Worker aggregate 全部使用同一个 DB `now`。
- audit 窗口固定为最近 `900s` 的半开区间 `[now-900s, now)`，按 `(occurred_at,id)` 最多读取 `50,001` 行；出现第 `50,001` 行时整次 scrape 以 `503 metrics_observation_limit_exceeded` 失败，绝不截断后返回错误 count。
- Run latency 窗口固定为最近 `3600s`。queue、execution、end-to-end 每类稳定读取最多 `10,001` 个样本，输出前 `10,000` 个的线性 p50/p95/p99 和 `truncated=1`。
- 每个 API 进程最多一个 collection in flight；重叠请求立即返回 `429 metrics_scrape_in_progress`，不排队。当前单 API 支持拓扑下这是数据库压力保护，不是多进程全局锁。
- 不提供 last-good cache。数据库或 audit 损坏后返回旧值会掩盖事故。
- 任一 retained event 的 event contract、payload hash、identity、retention interval 或数值边界损坏时，整次 scrape 返回 `500 audit_event_integrity_error`；数据库异常返回 `503 metrics_database_unavailable`。两者都不返回部分 exposition，也不反射损坏值、DSN 或异常文本。
- Redis ping 是快照外的非权威 component observation。Redis disabled/down 分别输出 configured/available，DB collection 仍成功；未知 queue 异常同样只把 availability 置零并写固定脱敏日志。
- 外层 asyncio timeout不能中止已经在线程中运行的同步 DB query；压力边界依赖固定窗口、hard cap、single-flight 和数据库 driver/pool timeout。推荐 scrape interval `>=30s`，本地默认文档使用 `60s`。

### 3. 固定 metric 集

当前 DB gauges 不带 label：

```text
llmbenchlab_runs_pending
llmbenchlab_runs_due_pending
llmbenchlab_runs_running
llmbenchlab_runs_expired_running
llmbenchlab_runs_cancellation_requested
llmbenchlab_runs_retry_scheduled
llmbenchlab_runs_dead_lettered
llmbenchlab_runs_queue_notification_error
llmbenchlab_runs_managed_backlog
llmbenchlab_runs_governance_delayed
llmbenchlab_runs_governance_exhausted
llmbenchlab_provider_attempts_active
llmbenchlab_governance_scopes_overdrawn
llmbenchlab_run_lease_acquisitions
llmbenchlab_run_failed_attempts
llmbenchlab_run_dispatches
llmbenchlab_run_expired_lease_oldest_age_seconds
```

expired lease age 是 `max(0, DB now - min(expired running lease_expires_at))`；空集合为零。它只表示当前 right-censored 逾期年龄，不是历史 lease-recovery duration 分布，也不改变 ADR-0010 的暂缓边界。

Typed audit 固定输出全部 17 个 event type，包括零值：

```text
llmbenchlab_audit_events_window{event_type="..."}
llmbenchlab_audit_event_window_seconds
llmbenchlab_metrics_audit_events_scanned
llmbenchlab_metrics_audit_event_limit
```

固定 event type 为 `governance_policy_bootstrapped`、`governance_policy_applied`、`run_admitted`、`run_claimed`、`run_cancel_requested`、`run_deferred`、`run_yielded`、`run_terminal`、`run_retry_scheduled`、`run_dead_lettered`、`run_lease_reconciled`、`provider_attempt_reserved`、`provider_attempt_send_started`、`provider_attempt_settled`、`question_evidence_persisted`、`queue_notification`、`governance_integrity_error`。

Run latency 固定为：

```text
llmbenchlab_run_latency_quantile_seconds{phase="queue|execution|end_to_end",quantile="0.5|0.95|0.99"}
llmbenchlab_run_latency_samples{phase="queue|execution|end_to_end"}
llmbenchlab_run_latency_truncated{phase="queue|execution|end_to_end"}
llmbenchlab_run_latency_window_seconds
llmbenchlab_metrics_latency_sample_limit
```

无样本时输出 `samples=0`、`truncated=0`，不输出 quantile series；不得以零、NaN 或 Infinity 冒充观测。

Queue 与 Worker 固定为：

```text
llmbenchlab_queue_configured
llmbenchlab_queue_available
llmbenchlab_worker_processes{state="registered|live|stalled"}
llmbenchlab_worker_expected_minimum
llmbenchlab_worker_shortfall
llmbenchlab_worker_activity_observed{activity="scan|claim|lease_heartbeat|progress"}
llmbenchlab_worker_activity_oldest_age_seconds{activity="scan|claim|lease_heartbeat|progress"}
llmbenchlab_worker_stale_threshold_seconds
llmbenchlab_run_recovery_alert_threshold_seconds
llmbenchlab_metrics_snapshot_unixtime_seconds
```

没有某类 Worker event fact 时仍输出 observed=0，但省略对应 age series。Renderer 只接受有限非负数字并按固定顺序生成 HELP/TYPE/sample；任何负值或非有限值使 collection fail closed。

JSON `/tasks/metrics` 的五个时间字段按未停止 generation 取 `MAX`，表示最近一次聚合事实；Prometheus 的 `worker_activity_oldest_age_seconds` 则对四类 activity 先取未停止 generation 的 `MIN` 再以同一 DB now 计算年龄，表示最老 active-generation 事实。两者语义不得混用或用一个聚合值冒充另一个。

### 4. Worker process/progress 数据模型

新增 `worker_processes` 表：

| 字段 | 合同 |
| --- | --- |
| `generation_id VARCHAR(36) PK` | 每次长运行 Worker 启动生成的 canonical UUID，永不复用 |
| `worker_id VARCHAR(128) NOT NULL UNIQUE` | 现有 lease/audit owner，仅内部关联，不从 API/exporter 输出 |
| `started_at UTC NOT NULL` | 注册事务的 DB UTC |
| `last_seen_at UTC NOT NULL` | 最近成功刷新的任一真实 event |
| `last_scan_at UTC NULL` | reaper 与 due-run scan 均成功 |
| `last_claim_at UTC NULL` | lease claim commit/后置完整性处理成功 |
| `last_progress_at UTC NULL` | Response 新插入或 Run/reaper durable 状态实际推进 |
| `last_lease_heartbeat_at UTC NULL` | 已有 lease 成功续租；初始 claim 不计 |
| `stopped_at UTC NULL` | graceful stop 的 DB UTC |

约束：worker_id 长度 `1..128`；`last_seen_at >= started_at`；四个可空 event time 均在 `[started_at,last_seen_at]`；`stopped_at IS NULL OR stopped_at >= last_seen_at`。对 stopped generation 的任何迟到 flush 都由 `generation_id AND stopped_at IS NULL` CAS 拒绝。表不保存 hostname、PID、current Run、Provider、URL、错误文本、题目或 Response 内容。唯一辅助索引为 `(stopped_at,last_seen_at,generation_id)`。

生命周期只有 `registered(active) -> stopped`。`stale` 是同一 DB snapshot 中的派生分类，不写成可被迟到 event 复活的状态；异常退出保留 `stopped_at=NULL` 并自然变 stale。

配置冻结为：

```text
worker_progress_flush_seconds = 5
worker_progress_stale_seconds = 60
worker_expected_processes = 1
worker_recovery_alert_seconds = 60
```

`stale_seconds` 必须至少为 `3 * max(flush_seconds, worker_poll_seconds, redis_block_milliseconds/1000, worker_heartbeat_seconds)`。Compose 默认一个 Worker，扩到两个时操作者必须同时把 expected minimum 设为 2；只有明确不需要执行能力的环境才可设零，不能从历史行数猜测 expected count。

### 5. Worker recorder 与 event 定义

长运行 `WorkerService.run()` 启动前必须完成 generation 注册；注册失败拒绝启动不可观测 Worker。`run_once()`、probe、测试/CLI 工具不注册长运行 generation。

每个 Worker 使用一个 event-loop owned、thread-safe note、单 in-flight 的 coalescing recorder：

- scan/claim/progress/lease-heartbeat 只设置固定 bit；后台最多每 5 秒通过 `asyncio.to_thread` 执行一笔短事务。
- 一次 flush 读取一个 DB `current_timestamp`，将全部 pending event 字段和 `last_seen_at` 写为同一值。timer 本身绝不能刷新 seen；无真实 bit 时零写入，避免主循环已死但 keepalive 继续制造幽灵健康。
- commit 成功后才清 bit；失败保留并重试，只记录固定错误。运行期 progress 失败不回滚 Run、Response 或 lease，旧时间会保守变 stale。
- graceful stop 将最后 pending bit 与 `stopped_at` 在同一短事务写入；失败保守留下 stale generation。
- observer 是可空、no-throw 的固定 enum 接口；既有 Runner/Worker 测试和调用方缺省使用 null object。

event 定义：

- `scan`：`_reap_expired()` 与 `_due_run_ids()` 都成功；任一步 DB 失败不记录。
- `claim`：Runner 得到非空 lease 且 claim commit 与 post-commit integrity 处理成功；no-op 不记录。
- `lease_heartbeat`：已有 lease 成功续租；rejected/exception/初始 claim 不记录。
- `progress`：Response `INSERTED`；terminal/defer/exhaust/yield/retry/dead-letter/cancel transition commit 确认；或 reaper report 任一 durable 计数非零。`ALREADY_PRESENT`、scan、heartbeat、Redis delivery/ACK 不算 progress。

Worker probe 保持 capability-only，不读取 peer aggregate、不写 progress，也不把任意 generation 冒充当前容器。响应固定补充：

```json
{"probe_scope":"dependencies_only","main_loop_progress":"not_checked"}
```

没有由主进程交付的 exact generation handoff 前，Docker healthcheck 不得声称当前 process main-loop liveness。

### 6. Worker read model 与 stale 语义

在 `/tasks/metrics` 与 exporter 的同一 DB `now` 中：

```text
cutoff     = now - worker_progress_stale_seconds
registered = stopped_at IS NULL
live       = registered AND last_seen_at >= cutoff
stalled    = registered AND last_seen_at < cutoff
shortfall  = max(worker_expected_processes - live, 0)
```

边界 `last_seen_at == cutoff` 为 live。JSON task metrics 增加 expected/registered/live/stalled/shortfall/stale-after，以及未停止 generation 中五个 event timestamp 的 `MAX`；空 fact 为 `null`。响应绝不包含 generation/worker ID。

历史 stopped 与 stale process 行属于低量运维事实，不是 audit/WORM。本切片不在 scrape/API 请求链路删除它们；importer 只拒绝仍 live 的 generation，并精确复制 stopped/stale 行。旧 crash 行即使存在，也不能让 `shortfall` 消失。需要 downgrade 时先停止 Worker、保存需要的运维事实并显式清空表；不得由 migration 静默丢弃。

### 7. 告警规则与 Runbook

新增 `deploy/observability/prometheus-alerts.json`；JSON 是 YAML 子集，可由 Prometheus rule loader 使用，也可由 Python 标准库严格测试，不新增 PyYAML。新增仅作示例的 scrape 配置，固定 `job_name: llmbenchlab`、path `/api/v1/metrics/prometheus`、interval `>=30s`；不把 Prometheus/Alertmanager 加入 Compose。

规则组 `llmbenchlab.phase2`、interval `30s`，固定八条：

| Alert | expression | for | severity | Runbook anchor |
| --- | --- | ---: | --- | --- |
| `LLMBenchLabMetricsUnavailable` | `up{job="llmbenchlab"} == 0` | `2m` | critical | `alert-exporter-unavailable` |
| `LLMBenchLabBacklogPersistent` | `llmbenchlab_runs_due_pending > 0` | `15m` | warning | `alert-backlog-persistent` |
| `LLMBenchLabDeadLettered` | `llmbenchlab_audit_events_window{event_type="run_dead_lettered"} > 0` | `0s` | critical | `alert-dead-letter` |
| `LLMBenchLabGovernanceIntegrityError` | `llmbenchlab_audit_events_window{event_type="governance_integrity_error"} > 0` | `0s` | critical | `alert-governance-integrity` |
| `LLMBenchLabGovernanceOverdrawn` | `llmbenchlab_governance_scopes_overdrawn > 0` | `0s` | critical | `alert-governance-overdraw` |
| `LLMBenchLabQueueDegraded` | `llmbenchlab_queue_configured == 1 and llmbenchlab_queue_available == 0` | `2m` | warning | `alert-queue-degraded` |
| `LLMBenchLabWorkerStalled` | `llmbenchlab_worker_shortfall > 0` | `2m` | critical | `alert-worker-stalled` |
| `LLMBenchLabLeaseRecoverySlow` | `llmbenchlab_run_expired_lease_oldest_age_seconds > llmbenchlab_run_recovery_alert_threshold_seconds` | `1m` | warning | `alert-lease-recovery-slow` |

每条规则只有 `severity` 与固定 `component=llmbenchlab-control-plane` label，并具有 summary、description、绝对 runbook URL 和 silence_policy annotation。Runbook 使用显式英文 HTML anchor，逐条规定响应、证据、恢复和以下 silence 边界：只允许有 owner/ticket/到期时间的窄 matcher；governance integrity/overdraw 不作常规静默；维护静默最长 15 分钟至 4 小时（按事件表固定），禁止全局 silence。

Dead-letter 与 integrity 是 15 分钟 rolling symptom；Prometheus 整个窗口不可用时可能错过一次性事件。Run audit 仍是事实来源，规则不等于 durable acknowledgement。

### 8. Audit 完整行验证

把 `runs.py` 与 `health.py` 重复的 retained-row read validation 收敛到 `app.governance.audit` 的公共函数。API、history、exporter、archive 和 restore 都必须复用同一 event contract、payload hash、identity、retention interval、timestamp 和 numeric validation。Read validation 不只比较 normalize 后的值：数据库原始 JSON 必须与 normalize 后的 canonical JSON（类型和编码语义）完全一致；例如数值 `1` 不得冒充规范化的 USD 字符串 `"1"`，否则 archive 会改写而不是保存原始存储事实。

业务 `append_audit_event()` 的 `_event_matches()` 有意忽略首次观察的 `id/occurred_at/expires_at`，适合 commit-ack 重放；archive restore 的精确比较必须包含所有存储字段，绝不能复用该宽松函数。

### 9. Audit archive 与文件合同

`llmbenchlab-audit-retention archive --output PATH` 在一个数据库读快照中取得 DB UTC `cutoff_at`，只选择 `expires_at < cutoff_at`：

- PostgreSQL 使用 `REPEATABLE READ READ ONLY`；SQLite 首读前显式 `BEGIN`。
- 按 `(expires_at,id)` 排序，单批固定上限 `10,000`，额外一行只决定 `has_more_eligible`。
- cutoff 不由用户任意传入；空集合也生成有效 archive。
- archive 默认绝不删除；运维顺序固定为 archive -> offline verify -> maintenance-window delete。

单文件 canonical JSONL schema 为 `llmbenchlab-audit-archive-v1`：一行 header、零至一万行 `audit_event`、唯一末行 manifest。V1 的 event/payload/retention contract 与 compatible Alembic head allowlist 独立冻结；当前唯一 compatible head 是 `20260828_0005`。`source_alembic_head` 不是装饰性字符串：write/verify/restore/delete/reconcile 都拒绝未列入 allowlist 的旧版、分支或未来 head；未来 schema 只有在证明 V1 全字段语义仍兼容后才能显式扩充 allowlist，否则必须提升 archive schema 并保留 V1 reader。Event 保存完整恢复事实：

```text
id, event_key, event_type, payload_hash, payload, retention_class,
occurred_at, expires_at, correlation_id, run_id, model_id, question_id,
worker_id, reservation_id, attempt, provider_attempt, lease_token,
duration_ms_hex
```

时间编码为六位微秒 UTC `Z`；finite nonnegative duration 用规范化 `float.hex()`，正负零统一。Manifest 至少含 event/type/class counts、occurred/expiry min/max、`has_more_eligible`、source Alembic head 和 `content_sha256`。Content hash 对 header+event canonical bytes 使用域分隔和长度前缀；manifest 不进入自引用 hash。工具另计算整个文件 `archive_sha256`，delete/restore 必须提交该精确 digest。

这两个 hash 只提供误改检测与精确文件绑定，不是签名、认证或 WORM。

Canonical 与文件边界：

- UTF-8、LF、最终换行；JSON `sort_keys=True`、紧凑分隔符、`ensure_ascii=True`、`allow_nan=False`；每行必须与重新编码 bytes 完全相同。
- 文件最大 128 MiB、单行最大 64 KiB、事件最多 10,000。拒绝 duplicate JSON key、空行、尾随记录、未知/缺失字段、NaN/Infinity、非法 UTF-8、重复 id/event_key、乱序和 hash/rollup 漂移。
- 输入必须是当前用户拥有、权限不宽于 `0600` 的普通非 symlink 文件。输出父目录必须存在；目标不得存在。
- 同目录用 `O_EXCL|O_NOFOLLOW` 创建 `0600` 临时文件，write+fsync 后 no-replace 原子安装，再 fsync 父目录。失败不留下可被误认作成功的目标。
- verify/delete/restore 一次打开并解析同一个 file descriptor，避免验证后换文件。

Archive 含内部 Run/Model/Question/Worker 等运维 ID，按敏感运维文件保护，不经 API 提供下载。它不得包含 Key、Authorization、Cookie、credentialed DSN、credential ciphertext/nonce、keyring、Provider URL、题目正文、Prompt、Response 或 raw Provider body。

### 10. Verify、reconcile、restore 与 delete

CLI 固定为：

```text
llmbenchlab-audit-retention archive --output PATH
llmbenchlab-audit-retention verify --archive PATH [--expected-sha256 HASH]
llmbenchlab-audit-retention reconcile --archive PATH --confirm-sha256 HASH
llmbenchlab-audit-retention restore --archive PATH --confirm-sha256 HASH
llmbenchlab-audit-retention delete --archive PATH --confirm-sha256 HASH
```

数据库连接只从项目固定 settings/environment 取得；命令不接受 DSN 字符串参数。`verify` 完全不导入数据库 session/engine、不读取或校验数据库配置、不创建 SQLite 父目录，也不连接数据库；即使数据库 URL 无效或不可用，合法 archive 仍可在 fresh process 中验证。它只输出固定 status、count 和 digest，不输出 path、ID、payload 或异常原文。参数解析错误同样使用固定 exit `2`/code；未知参数和值不得由 argparse usage/error 反射。

Restore/delete/reconcile 先在同一 file descriptor 完成 strict verify 与 digest confirmation，再打开当前 Alembic head 数据库。PostgreSQL mutation 使用固定 transaction advisory lock，SQLite 使用 `BEGIN IMMEDIATE`，以串行化本工具的 mutation；API/Worker audit writers仍必须在 delete/restore 维护窗口停止。

Restore 对每条 record 同时按 id 与 event_key 比较全部存储事实：

- 二者均不存在：插入精确原行；
- 已存在且全部字段 exact：幂等 no-op；
- 同 key 不同 id、同 id 不同 key、任一字段不同或关联 reservation 缺失：整批 rollback；
- exact 与 absent 可以在同一事务安全收敛；提交后独立只读快照必须全部 exact。

Delete 永不执行宽泛 `DELETE WHERE expires_at < cutoff`：

- DB now 不得早于 archive cutoff，且每条 archive record 必须已经过期；
- 事务内 exact id/key 锁定并比较；全部 exact 时只删除 archive 列出的行；全部 absent 返回 `already_absent`；mixed missing/conflict 整批 rollback；
- 事务内删除数必须等于 archive count，提交后独立快照必须全部 absent；不删除 ledger、Run、Response 或其他表。

`reconcile` 是只读恢复入口，只返回 `all_exact`、`all_absent`、`mixed_exact_absent`、`conflict` 或 `empty_archive`，不打印 event 正文。

提交结果沿用 importer 风格：exit `0` 为成功且后验验证完成；`2` 为 pre-commit/file/preflight 失败且无已知写入；flush 成功后 commit 抛错为 `4 commit_outcome_unknown`，禁止盲重试；commit 返回后后验验证失败为 `3 committed_but_verification_failed`。所有错误只输出固定 code、operation、count/digest 和异常类型，不使用可能含 path/DSN/payload 的 `str(exc)`。

### 11. Schema、migration 与 importer

新 Alembic head `20260828_0005`，同时是 archive-v1 当前唯一 compatible source/target head：

- 创建 `worker_processes` 及本文约束/索引；
- 为 audit archive 扫描增加 `(expires_at,id)` 索引；
- 为 exporter 的有界 audit 窗口扫描增加 `(occurred_at,id)` 索引；
- 不创建 retention 状态表，不回填旧 Worker 进展，也不修改历史 audit expiry。

`prepare_migrations.py` 将 `20260827_0004` 纳入 historical head/fingerprint/备份/adoption，严格验证新表、约束和索引。`0005 -> 0004` 在第一条 DDL 前要求 `worker_processes` 为空；有行即拒绝且不得静默丢失事实。空隔离库继续支持 downgrade/re-upgrade。

SQLite -> PostgreSQL importer 的 core table list 从 12 增至 13，精确复制 stopped 和已经 stale 的 Worker facts并纳入 count/PK/content digest、目标空表与锁序；source preflight 使用 DB now/stale threshold拒绝仍 live 的 generation，防止把正在运行的 Worker 数据库迁走。Stale crash row不会被当成 live，也不会让 expected shortfall 消失。

Audit archive 文件不是 importer 的第十四张表，也不由 importer 自动读取。P2-07 后续整库恢复必须独立证明 13 表与 keyring 配对；本决定不提前完成该工作。

### 12. 日志与秘密边界

- 新 exporter、Worker recorder、archive 和 CLI 只记录固定 event/error code、operation、计数和 allowlisted enum；不得记录 DSN、path、event payload、record ID、worker ID、hash 以外的文件内容或原始异常文本。
- 维护 CLI 的 digest 可以输出；archive path、内部 ID 和 child/raw 诊断不进入公开状态文档。
- 所有生产 logging source 必须在本切片中做静态和动态 marker 审查；只有显式登记的应用 logger 可输出 literal message，structured extra 还须逐字段经过固定 enum、canonical UUID/Redis stream ID、HTTP method/route 与有限数值规范化。非法 ID 省略，未知字符串只输出固定 `unsupported`；Redis notification 的 Run/correlation identity 在进入 Worker 前也必须是 canonical UUID。上游异常、OS error、SQLAlchemy error 和 subprocess output 不直接进入 message/extra。
- 自动化只使用 Mock/Stub，不创建真实 Provider Model，不继承或调用真实 Provider credential。

## Consequences

### Positive

- JSON API、Prometheus scrape、告警与 Worker liveness 从同一 DB-time facts 派生，且低基数/压力/损坏边界可测试。
- capability probe 与 main-loop progress 不再混淆；长 SSE 可用 lease heartbeat 保持 live，而无事件 timer不能制造幽灵健康。
- audit 保留从“只有 expiry 字段”变成 archive、离线验证、精确删除、恢复和不确定提交对账的可执行流程。
- 无新增 Prometheus/YAML/OTel 生产依赖，数据库仍是唯一事实来源。

### Negative

- 每个长运行 Worker 最多每 5 秒增加一笔短事务；高频 scrape 仍会读取有界 audit/Run 窗口，需要按文档限制抓取频率。
- Stale crash generation 会保留为诊断事实；数据库迁移与 downgrade 需要显式处置，而不是静默删除。
- JSONL hash 不能抵抗有权同时改文件与 hash 的管理员；Alert rules 没有 sender/ack 状态，Prometheus 长时间不可用时可能错过 rolling event。
- Audit archive restore 只恢复 audit 行，不证明数据库、credential keyring、Redis 或 Provider 侧状态完整。

## Validation

- Worker：注册/停止、DB UTC、bit coalescing、无 event 零写、flush failure重试、stopped CAS、scan/claim/heartbeat/progress精确边界、stale/expected/shortfall、probe capability-only，SQLite/真实 PostgreSQL并发一致。
- Exporter：固定 content type/顺序/HELP/TYPE、空与 populated 快照、17 event series、窗口边界、50,001 hard cap、latency cap/分位数、queue disabled/down、Worker stale、长 SSE、single-flight、audit/DB/renderer fail closed、marker/ID/secret absence。
- Rules：标准库 strict JSON、精确 8 rules、只引用已交付 metrics/`up`、固定 labels/annotations/runbook anchors、event rules禁用 rate/increase；若本机有 promtool，额外运行 `promtool check rules` 并如实记录。
- Retention：canonical/hash/rollup/permissions/symlink/atomic output/limits、冻结 V1 fixture/contract 与 unsupported future head 拒绝、fresh-process invalid DB URL 下离线 verify/无 engine 或目录副作用、argparse marker 不反射；SQLite archive->verify->delete->reconcile->restore；same-key/different-fact、same-id/different-key、missing reservation、mixed batch、commit unknown；真实 PostgreSQL snapshot/advisory/row-lock/完整往返。
- Migration/importer：0004 adoption/backup到0005、双方言空库 round-trip、populated downgrade拒绝、13表 exact digest、live Worker preflight拒绝。
- 完成目标/全量 pytest、Ruff、Mock smoke、Compose config、文档链接、staged diff/secret检查后，形成独立 commit push，并等待该精确 SHA 的四个必需 CI job全绿。P2-07未完成时 Phase 2保持 `in_progress`。

## Rollback

先停止 API/Worker/exporter scrape 和 retention maintenance。应用可关闭 exporter 和 Worker progress recorder，但保留 `0005` schema、已有 progress rows和 archive；不允许为代码回滚删除审计或未保存的 Worker facts。只有 `worker_processes` 为空时才能 downgrade 到 `0004`。

Audit delete默认从不自动运行。若 delete/restore出现 exit 3/4，禁止盲重试，先用同一 archive+digest执行只读 reconcile。已经生成的 archive按敏感 `0600` 文件保存并独立验证；它不能替代 PostgreSQL+keyring备份。若 exporter压力或损坏导致非2xx，修复数据库/降低抓取频率，而不是返回缓存或放宽完整性检查。
