# ADR-0016：PostgreSQL 与 keyring 配对恢复、Redis 重建及恢复演练

- **Status**: Accepted
- **Date**: 2026-08-28
- **Deciders**: LLMBenchLab maintainers
- **Scope**: Phase 2 P2-07 备份/恢复验证、Redis rebuild、告警响应与故障矩阵
- **Amends**: [ADR-0005](ADR-0005-durable-task-execution.md) 的恢复运维边界、[ADR-0007](ADR-0007-web-provider-credentials.md) 的数据库外 keyring 备份边界，以及 [ADR-0015](ADR-0015-observability-worker-progress-audit-retention.md) 的告警与 archive 后续演练
- **Amended by**: [ADR-0017](ADR-0017-schema-equivalent-governance-index-repair.md) 将尚未实施的 recovery-manifest-v1 exact head 显式更新为 schema-equivalent `20260829_0006`；[ADR-0018](ADR-0018-observational-token-estimates-are-not-hard-reservations.md) 再将 exact head 更新为 data-only `20260830_0007`；[ADR-0019](ADR-0019-explicit-provider-api-protocol-adapters.md) 将尚未实施的 exact head 更新为 Provider-column/check `20260830_0008`
- **Preserves**: PostgreSQL 唯一任务事实来源、Redis 非权威通知、write-only Provider Key、`llmbenchlab-protocol-v1`、Mock-only 自动化和可信本地运维边界

## Context

Phase 2 已交付 PostgreSQL 任务/治理事实、Redis Streams 通知、独立 Worker、租约/fencing、13 表 SQLite→PostgreSQL importer、数据库外 AES-GCM keyring、固定 exporter/八条规则和 audit archive。它们仍不能回答完整恢复问题：

1. SQLite importer 是停写源到空 PostgreSQL 的一次性迁移，不是 PostgreSQL backup/restore；audit archive 只保存 typed audit 行，也不是整库备份。
2. `model_credentials` 只保存 envelope。没有与备份点匹配的数据库外 keyring，即使 13 表完整恢复也不能恢复 stored Provider credential；只校验 key ID 或 active key 不能证明历史 envelope 可解密。
3. Redis AOF、Stream、consumer group、PEL 和 lag 都不是任务事实。空 Redis 能由 Worker 的数据库扫描恢复，但必须证明丢失 group/PEL/lag 后仍能收敛，并避免提供 `FLUSHALL`、删 group 或“数据库重放到 Redis”这类危险生产命令。
4. P2-06 的八条 Prometheus 规则已有静态合同和 Runbook，但尚未分别验证 `for` 时序、真实底层 symptom、处置动作与恢复证据。
5. 既有 acceptance/capacity/crash-seam 不能冒充 PostgreSQL+keyring 恢复认证，也不能证明 PITR、RPO/RTO、HA、Alertmanager 投递或 Provider exactly-once。

本决定只为可信操作者、本地/隔离 PostgreSQL 16 与 Redis 7 拓扑建立可重复验证的恢复合同。它不把仓库变成生产备份平台。

## Decision

### 1. 工具边界

- PostgreSQL 备份固定使用同 major 的 PostgreSQL 16 `pg_dump --format=custom`；恢复固定使用 `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges`。
- 仓库不实现生产级 dump/restore mutation CLI，不包办调度、异地复制、加密存储、PITR、WAL、保留策略或备份服务。`pg_restore --clean`、`--create`、覆盖既有数据库和自动 DROP/TRUNCATE 一律不属于受支持路径。
- 新增只读运维入口 `llmbenchlab-recovery-verify`，只允许三个动作：
  - `create-manifest`：读取已生成 dump、独立 keyring 和只读源数据库快照，安全 no-replace 写 canonical manifest；不修改数据库。
  - `artifacts`：完全离线核验 manifest、dump 与 keyring；不得导入数据库配置或构造 engine。
  - `restored-database`：只读核验已恢复数据库、manifest 与 keyring；不得创建、删除、迁移、清空或修正数据库。
- `llmbenchlab-recovery-verify` 不接受 Provider Key、credential plaintext、DSN 或密码作为 argv；数据库连接只从明确环境变量/libpq service 取得。成功和失败只输出固定状态、匿名计数和 digest，不输出路径、URL、连接信息、对象 ID 或异常原文。
- 新增独立 `make phase2-recovery` 隔离 Compose harness。它可以在随机且经过 label/identity guard 的项目内创建/删除测试数据库和 Redis volume；这些破坏性能力不进入生产 CLI 或 REST API。
- 不新增 REST API、数据库 migration 或长期运行 backup 服务，也不新增生产依赖。

### 2. 维护窗口与 PostgreSQL 恢复不变量

- 创建备份前停止 admission、API、Worker、CLI mutation 和所有 application/audit writer；确认 Worker 已 graceful stop 或进入可解释 stale 状态。数据库健康探针可以继续只读连接，但任何未知 writer 都使备份集不合格。
- `pg_dump` 与 source manifest snapshot 必须来自同一停写窗口。恢复后的 13 表摘要与 manifest 不一致即证明期间发生漂移或 artifact 不匹配，整组 fail closed。
- 目标必须由操作者预先创建且没有任何用户 schema/table/Alembic row。先运行只读 empty-target 检查；目标非空时拒绝，绝不由工具清空。
- 按 [ADR-0017](ADR-0017-schema-equivalent-governance-index-repair.md)、[ADR-0018](ADR-0018-observational-token-estimates-are-not-hard-reservations.md) 与 [ADR-0019](ADR-0019-explicit-provider-api-protocol-adapters.md)，恢复后数据库必须已经精确位于 Alembic `20260830_0008`。`0006` 与 `0005` 逻辑 schema 等价，只修复早期 `0004` 三索引缺口；`0007` 不改 schema 或 ledger/actual，只按“仅显式 input reservation 构成 hard bound”的规则重算 `governance_scopes.overdrawn`；`0008` 将 `models.provider_type` 从 `VARCHAR(17)` 扩为 `VARCHAR(18)`，并替换 Provider 类型 check 与远程配置 check。P2-07 尚未实施，因此 recovery-manifest-v1 在实现前直接固定当前 head。禁止先升级旧 dump 再把它报告为原备份恢复成功；若未来支持新 head，必须在新 ADR/manifest schema 中显式声明兼容。
- 13 张核心表固定为：

  ```text
  governance_policies
  models
  model_credentials
  benchmarks
  questions
  governance_scopes
  evaluation_runs
  evaluation_responses
  governance_minute_buckets
  question_executions
  provider_call_reservations
  audit_events
  worker_processes
  ```

- 每表必须逐项满足 `row_count`、主键集合 SHA-256 和 canonical 全行 SHA-256。恢复验证复用与 importer 相同的 typed canonical encoding，并重跑：policy hash/列、managed Run 冻结 snapshot/四 override、ledger→scope/minute 投影、active policy 唯一性、retained audit 完整事实、FK/约束和 Alembic head。
- `worker_processes` stopped/stale facts、managed Run、Response、ledger 和 audit 必须原样保留；验证器不得通过清空或重建事实来制造一致。
- P2-07 certification 源允许一个只使用 Mock 的 pending Run 来验证 Redis rebuild，但不得包含 running Run、active reservation 或 live Worker generation；这避免把未知 Provider 外部副作用混入恢复资格。例行备份也应优先排空或取消活动 Run。
- 标准 PostgreSQL dump 本身可以保留生产故障点的 running/active 事实，但这类备份超出本次认证 profile；恢复时仍必须隔离目标、让 lease/reconciler 按既有规则收敛，并独立核对备份点之后的 Provider 副作用。

### 3. Canonical recovery manifest

Manifest schema 固定为 `llmbenchlab-recovery-manifest-v1`，UTF-8、canonical JSON、未知/缺失字段、duplicate key、NaN/Infinity 和非规范时间均拒绝。最小字段为：

```json
{
  "schema": "llmbenchlab-recovery-manifest-v1",
  "backup_set_id": "00000000-0000-4000-8000-000000000000",
  "created_at_utc": "2026-08-30T00:00:00.000000Z",
  "source_alembic_head": "20260830_0008",
  "source_git_commit": null,
  "database_dump": {
    "format": "postgresql-custom",
    "postgres_server_major": 16,
    "pg_dump_major": 16,
    "size_bytes": 1,
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "credential_keyring": {
    "format": "llmbenchlab-credential-keyring-json-v1",
    "size_bytes": 1,
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "credential_binding": {
    "stored_model_count": 0,
    "envelope_count": 0,
    "active_stored_run_count": 0
  },
  "tables": []
}
```

- `backup_set_id` 为 canonical UUID v4；时间为六位微秒 UTC `Z`；Git SHA 为 40 位小写十六进制或 `null`。
- `tables` 必须按上节顺序恰好出现 13 次，每项只含 `name/row_count/pk_set_digest/canonical_row_digest`。
- Manifest 不包含路径、DSN、用户名/主机、key ID、Key、nonce、ciphertext、Provider URL、Run/Worker/Model ID、Prompt、Response 或日志。
- Manifest 绑定完整 dump bytes 和完整 keyring bytes；不能只绑定 active key。数据库 dump/manifest 与 keyring 必须独立备份、分开存储并使用不同访问控制，配对时才临时同时可见。
- Manifest 自身另有文件 SHA-256；operator 在验证和恢复时必须提交精确 digest。所有 SHA-256 只用于误改检测和精确配对，不是签名、来源认证、WORM 或恶意管理员防篡改证明。
- 历史备份必须保留其完整配对 keyring，直到该备份按策略销毁。当前只支持添加新 key/切换 active；在没有批量重加密与独立轮换合同前，不得删除仍被当前或历史备份 envelope 引用的旧 key。

### 4. Artifact 文件安全

- Manifest、dump 与 keyring 输入必须是当前用户拥有的普通文件且 `st_nlink == 1`；拒绝 symlink、FIFO、device、socket、hard link、identity 变化以及 group/other 可读写的文件。权限不得宽于 `0600`。
- 输入以 `O_NOFOLLOW` 单一 fd 打开，先后 `fstat` 校验 identity/size/mtime，采用固定 chunk 流式 hash；keyring 上限沿用 `64 KiB`，manifest 上限 `1 MiB`，certification dump 上限 `64 GiB`。任何读取期变化都 fail closed。
- 输出父目录必须存在、当前用户拥有、非 symlink 且不可 group/other 写，建议 `0700`。Manifest 使用同目录 `O_EXCL|O_NOFOLLOW` 的 `0600` 临时文件、fsync、no-replace 原子安装和 parent fsync。
- CLI parser、错误、日志和 evidence 不反射 argv path、异常、文件内容或工具 stdout/stderr。未知参数也只返回固定错误。
- 自动化 keyring 只使用每次随机生成的 32-byte 假密钥；所有 Provider credential 环境变量从 child env 删除。

### 5. Keyring 配对与本地解密验证

验证顺序固定。恢复流程不得调用会在 keyring 缺失时自动生成新文件的 `make setup/dev/docker-up` 或 `bootstrap_credential_keyring.sh`；必须使用只验证既有 artifact 的恢复入口，且在配对验证成功前不得启动 API/Worker：

1. `artifacts` 在任何数据库连接或 Adapter 构造前安全读取并严格解析 keyring，精确比较 manifest 的 size/SHA。即使数据库没有 stored credential，错误 keyring 也必须由 digest 拒绝。
2. `restored-database` 对每个 `credential_source=stored` Model 构造现有 `EncryptedCredential`，用当前 Model ID/origin 作为 AAD 调用 `CredentialKeyring.decrypt()`；只累计成功数，plaintext 不比较、不 hash、不保存、不输出。
3. 对每个 pending/running 且 frozen source 为 `stored` 的 Run，用 immutable Run snapshot 的 Model ID/origin 再验证一次；不得用当前可编辑 Model origin 代替。
4. missing/unreadable、格式错误、digest mismatch、未知/退役 key、同 key ID 不同材料、错误 Model/origin、tampered nonce/ciphertext 均在 Adapter/HTTP 构造前稳定 fail closed，并证明 Provider I/O 次数为零。

稳定错误族为 `recovery_keyring_unavailable`、`recovery_keyring_invalid`、`recovery_keyring_digest_mismatch`、`recovery_credential_binding_failed`；不得包含 key ID 或底层 crypto 文本。

### 6. `pg_restore` 结果分类

- `pg_restore` exit 0 仍不表示恢复成功；必须再运行 `restored-database`，只有 13 表与 keyring 全 exact 才成功。
- 工具非零或连接中断后，不盲目重跑、不执行 `--clean`：
  - 能确认目标仍无用户对象：`precommit_failed`，可由操作者创建另一个空目标重试。
  - 目标与 manifest 全 exact：`committed_but_tool_or_postverify_failed`；保留目标并调查，不能再 restore。
  - 无法连接判断：`commit_outcome_unknown`；隔离目标，恢复连接后只读 reconcile。
  - 目标为 partial/mixed/conflict：`integrity_conflict`；保留现场，不自动删除或覆盖。
- 只读 verifier 成功为 exit 0，安全拒绝为 exit 2。Compose harness 对上述 outcome 使用固定内部分类；exit 3/4 的既有语义分别保留给“已提交但后验证失败”和“提交结果未知”。

### 7. Redis replacement 与 Worker 恢复

- Redis rebuild 不恢复 AOF/Stream/consumer state，也不从数据库生成历史消息。平台提供空 replacement Redis 后，先启动一个 Worker：每轮先 `reap_expired()`/`due_run_ids()`，因此无需 Redis 即可恢复 pending/expired Run；随后 Worker 以 `XGROUP CREATE ... 0-0 MKSTREAM` 建立唯一 group。
- 若 Stream 保留而 group 丢失，group 仍从 `0-0` 重建，让历史通知按 at-least-once 再投递；终态/重复通知必须由数据库 no-op 后 ACK。禁止用 `$` 跳过历史消息。
- Redis/NOGROUP/连接失败必须令 Worker 重置本地 group-initialized flag 和 `XAUTOCLAIM` cursor；下次尝试重新初始化。数据库 scan 在整个 queue outage 期间继续。
- 完全丢失 Redis 的演练至少保留：一个已完成基线、一个 orphan PEL、一个 lag pending，以及可选的部分 Response/过期 lease。重建后证明所有数据库事实收敛、新通知能完成 `XADD→XREADGROUP→XACK`、group 唯一、PEL/lag 为零、terminal duplicate 不改变 canonical snapshot、ledger projection 无漂移。
- 生产代码不新增 `FLUSHDB/FLUSHALL`、删 Stream/group/volume 或 replay 命令。Harness 只能删除自身随机 project 下经 exact label/identity 校验的 Redis container/`redis-data` volume；PostgreSQL volume identity 删除前后必须相同。
- 禁止 glob、未解析变量、`volume prune`、`image prune`、force、默认 project、共享 volume 和中途 `compose down -v`。最终 cleanup 才能对已验证随机 project 执行 scoped `down -v`。

### 8. Worker 扩缩与 expected minimum

- `worker_expected_processes` 是 API/exporter 的部署声明，不由 Worker 或历史 generation 数推断。Compose 把该环境变量只注入 API，不再放入公共 backend environment。
- 扩容 `1→2`：先增加 Worker 并确认两个 generation 已产生真实 DB scan，再以 expected=2 强制重建 API，最后验证 `expected/registered/live/stalled/shortfall=2/2/2/0/0`。
- 缩容 `2→1`：先以 expected=1 重建 API，再 graceful scale Worker；等待被停 generation 写 `stopped_at` 或 lease 自然恢复，最后验证 `1/1/1/0/0`、Response 唯一和 ledger 收敛。
- 当前资格只覆盖 1–2 Worker；第三个 Worker 或不同 pool/lease/profile 必须重新设计和测量。

### 9. 八规则时序与真实响应演练

P2-07 使用两层 AND 验收，不等待真实 15 分钟：

1. 新增 Prometheus `promtool test rules` fixture，evaluation interval 固定 30 秒；八条规则都必须在 `for` 到期前不 firing、到期后 firing、输入恢复后 clear，并匹配固定 label/annotation。`run_dead_lettered`、integrity 和 overdrawn 是持久/rolling symptom，fixture 的 clear 只验证规则输入，不允许在真实数据库中删除事实制造“恢复”。
2. 隔离 Compose 分别制造真实底层 symptom 并执行 Runbook 动作：API stop/restart、due backlog/drain、生产 repository failure budget→dead-letter、隔离库 projection drift→fail closed→从已验证备份恢复、Mock actual>reservation→overdraw、Redis outage/replacement、Worker stale/恢复、lease-owner kill/peer takeover。

真实演练证明 symptom、证据采集边界、处置和根因恢复；不声称仓库部署 Alertmanager sender、通知路由、silence API、ack 或生产值班系统。Dead-letter/integrity/overdrawn 恢复后只要求没有新增事件和新工作正常，不伪造旧事实消失。

### 10. 验证分层与 evidence

- 单元测试覆盖 manifest canonical/path/权限/line/size/duplicate/future schema、keyring mismatch/crypto binding、只读 CLI/离线 import、result classifier、Redis NOGROUP reset、project/volume guard、evidence allowlist 和规则 fixture 合同。
- 真实 PostgreSQL/Redis integration 覆盖 custom dump→空目标 restore→13 表 exact、credential local decrypt、nonempty target refusal、commit-unknown classifier、orphan PEL/XAUTOCLAIM、group destroy/recreate from `0-0`、ACK unknown/duplicate no-op 及故障矩阵的 repository/ledger 状态转换。
- 独立 Compose recovery harness 覆盖完整 DB+keyring 配对、空 Redis volume replacement、Worker 1↔2、八项底层 symptom/响应与 scoped cleanup；保留现有 P2-06 9/9 acceptance 和 P2-01 SLO 合同不变。
- `phase2-recovery` 使用严格 allowlist evidence。内部/CI summary 只保存 schema、suite UTC 起止、commit/dirty、脚本/Compose/rules hash、PG/Redis major、scenario 名称/状态/时长、固定 revision、`table_count=13`、整体 count/PK/content/governance/audit/keyring equality、稳定 outcome、匿名聚合计数、Worker/Redis/ledger/cleanup count 和布尔门禁；不保存逐表 count/hash、命令、stdout/stderr、日志、traceback、路径、DSN/URL、port、对象/container/image/volume/database ID、环境名/值、key ID、keyring hash/material、envelope、题目、Prompt 或 Response。
- 公开文档只引用外层 evidence 相对路径、整文件 SHA-256、commit/dirty、匿名汇总和边界；raw evidence 不提交、不上传。CI artifact 若保留，只允许相同 allowlist schema。
- P2-07 实现进入现有 `Real Compose reliability acceptance` 必需 job，但 job 名保持不变；该 job 先运行冻结的 P2-06 acceptance，再运行 P2-07 recovery harness。P2-07 只上传通过 schema validator 的 allowlist summary，dump/manifest/keyring/raw evidence 永不上传。GitHub-hosted runner 的结果证明功能门禁，不是恢复 SLA。

## Alternatives considered

### 自制 PostgreSQL backup/restore mutation CLI

拒绝。它会复制 PostgreSQL 工具、扩大权限/版本/commit outcome 面，并诱导操作者把仓库脚本当生产 DR 平台。

### 把 keyring 嵌入 dump 或与 dump 存在同一 bundle

拒绝。数据库与 keyring 同时泄漏即可恢复 Provider Key；独立存储和访问控制是现有凭据安全边界的一部分。

### 只比较 key ID 或只尝试解密现有 envelope

拒绝。零 stored credential 时会让错误 keyring 空集通过；只比较 ID 也不能发现同 ID 不同 key material。必须先比较完整 artifact digest，再执行 AES-GCM/AAD 验证。

### 备份/恢复 Redis 或把数据库状态 replay 为 Stream

拒绝。Redis 不是任务事实来源；恢复历史 queue state 只会增加重复和错误权威性。空 Redis + DB reconciliation 是更小且可证明的恢复路径。

### 真实等待八条告警的 `for` 时长

拒绝。15 分钟 wall-clock 等待会让 CI 变慢且不增加表达式可信度；使用 promtool synthetic time 验证规则时序、Compose 验证真实 symptom/Runbook。

## Consequences

### Positive

- 备份与恢复职责保持在成熟 PostgreSQL 工具，仓库验证器只读、可复用现有 13 表/治理/audit 合同。
- 数据库与 keyring 既能精确配对，又保持分离存储和不同访问控制。
- Redis 全丢失、group 丢失和重复通知的恢复路径与数据库权威设计一致，不引入危险生产删除命令。
- 八条规则同时获得确定性时序测试和真实底层响应演练。
- Evidence 默认比既有 acceptance 更小，降低 dump、凭据和基础设施信息泄漏面。

### Negative

- 操作者仍需自行部署、调度、加密和异地保存 PostgreSQL dump/keyring；仓库不提供一键生产恢复。
- 配对验证需要在受控环境临时同时访问 dump、manifest 和 keyring。
- 完整 Compose recovery 会增加本地/CI 时间，并需要严格清理随机数据库、volume 和临时文件。
- 备份点之后已发生的 Provider 请求无法由数据库恢复；本地恢复仍不是 exactly-once。

## Rollback

- 只读 verifier 和 recovery harness 可从调用面停用，不影响数据库 schema/API/评分协议；manifest/dump/keyring 保持敏感资产，不能因代码回滚自动删除。
- Compose expected 环境重定位若回滚，必须同步恢复部署文档并承认运行中 API 的 expected 不会随 Worker scale 自动变化。
- Redis queue 修复只能回退代码，不能删除数据库事实或用旧 AOF 覆盖 PostgreSQL。
- 已恢复目标出现不确定结果时保持隔离；禁止通过 DROP/clean 伪装回滚。删除测试目标只限 harness 的 exact project/database guard 与最终 cleanup。

## Follow-up

- 若未来需要生产 PITR、RPO/RTO、签名 manifest、KMS/HSM、异地对象锁、自动轮换/重加密或多主机 HA，必须另立 ADR 与部署/安全模型。
- P2-07 完成只允许评估 Phase 2 的约定范围；不得外推真实 Provider、第三个 Worker、公共多租户、Alertmanager 或灾难恢复 SLA。
