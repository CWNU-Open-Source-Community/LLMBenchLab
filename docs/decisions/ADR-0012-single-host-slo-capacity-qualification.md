# ADR-0012：单机控制面 SLO 与容量资格合同

- **Status**: Accepted
- **Date**: 2026-08-28
- **Deciders**: LLMBenchLab maintainers
- **Scope**: Phase 2 P2-01 支持拓扑、服务级目标、容量模型与可重复资格 evidence
- **Related requirements**: `docs/NEXT_TASK.md` P2-01
- **Clarifies**: [ADR-0005](ADR-0005-durable-task-execution.md) 的本地可靠执行支持边界、[ADR-0009](ADR-0009-database-governance-audit-fair-scheduling.md) 的容量外推边界
- **Preserves**: [ADR-0010](ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0011](ADR-0011-confirmed-pre-send-release-retry-generation.md)

## Context

精确实现 SHA `665244e095905083b606b8e98e946ed1a02dc0fc` 已通过一次 enhanced capacity、9/9 acceptance、真实 PostgreSQL/Redis integration 和远程 CI。该 capacity run 观测到单 Worker `7.306981 q/s`、双 Worker `13.396740 q/s`、bounded burst `8.585309 q/s`，并证明 typed backpressure、公平、故障恢复和最终对账；但它每个测量 cell 只有 4 个 Run、故障只有一次，且使用加速的 `lease/heartbeat/poll=6/2/0.15s`。它可以作为本 ADR 运行前阈值的预实验输入，但不计入正式资格样本，也不能单独证明 SLO。

P2-01 需要一个在运行前冻结的资格合同。合同必须区分平台控制面的可重复 Mock 负载与真实 Provider，不得把单主机、多进程恢复描述为 HA，也不得把 Hosted CI 的共享 runner 当作稳定性能实验室。

## Decision

### 1. 受支持资格拓扑

定义唯一正式配置 `P2-local-control-plane-v1`：

- 单一可信操作者、单主机/单故障域、loopback 或隔离网络。
- 1 个 API、1 个 PostgreSQL 16 primary、1 个 Redis 7、2 个独立 Worker；Redis 只作通知层。
- PostgreSQL 是共享事实来源；SQLite 只保留单 Worker 兼容，不进入本资格。
- Host 分配至少 8 logical CPU、8,000,000,000 bytes RAM；Docker engine 报告至少 8 logical CPU、4,000,000,000 bytes RAM。资格期间不得有操作者主动启动的竞争负载。
- PostgreSQL `max_connections >= 100`。API 与每个 Worker 显式使用 `pool_size=5`、`max_overflow=5`；两 Worker 时应用连接上界为 `(1 + 2) × (5 + 5) = 30`，并至少给迁移、探针和运维保留 20 个连接。
- 每个 Worker 同时只执行 1 个 Run；Run `concurrency=1`。负载固定为原创 Demo 15 题、Mock generation delay 80 ms、输入预留 256 tokens、输出上限 64 tokens。
- 每轮显式 apply/read-back 全有限治理 policy，固定 `backlog_limit=4`、`question_quantum=5`；bootstrap policy 的其他值不属于资格配置。
- 正式资格显式固定 `lease=30s`、`heartbeat=10s`、`poll=1s`、`max_attempts=3`、delivery retry backoff `base/cap=1/30s`、shutdown grace `30s`、Redis block/operation timeout `1000ms/1s`。`phase2-capacity` 的 `6/2/0.15s` 继续作为快速故障回归默认，但不能证明本 ADR 的恢复目标。
- Compose 显式注入 pool、retry 和 scan 参数；每轮从容器内 `Settings` 回读资格敏感值，并将稳定 image ID、资源和 PostgreSQL `max_connections` 纳入环境指纹。

以下不在支持范围：真实 Provider、公网或多租户、多主机/多区域、HA/RPO/RTO、Provider exactly-once/账单、超过 2 Worker、其他题量/并发/硬件的外推、可信 CLI 的 `legacy_unmanaged` 执行，以及 SQLite 多 Worker。

### 2. 预登记 SLO

每项都在同一个精确、干净 commit、同一 profile 和同一环境指纹下判定：

| SLI | Objective |
| --- | ---: |
| 单 Worker Mock 吞吐 | one-sided 95% Student-t LCB `>= 5 q/s`；sample CV `<= 15%` |
| 双 Worker Mock 吞吐 | one-sided 95% Student-t LCB `>= 10 q/s`；sample CV `<= 15%` |
| bounded burst 吞吐 | one-sided 95% Student-t LCB `>= 6 q/s`；sample CV `<= 20%` |
| 双/单 Worker 配对吞吐比 | one-sided 95% Student-t LCB `>= 1.50`；sample CV `<= 15%` |
| 单 Worker queue / execution / E2E p95 | 每轮分别 `<= 3 / 8 / 10s` |
| 双 Worker queue / execution / E2E p95 | 每轮分别 `<= 2 / 5 / 7s` |
| bounded burst queue / execution / E2E p95 | 每轮分别 `<= 3 / 5 / 8s` |
| backlog admission | 每轮 6 路并发提交恰为 `4×202 + 2×typed 429 run_backlog_full` |
| backlog drain | Worker 恢复后 4 个已接纳 Run 全部完成且耗时 `<= 10s` |
| Worker loss | pause 后 DB 复核同 owner/token/send-started 的 kill fence 到新 claim `<= 38s`；旧 lease expiry 到新 claim `<= 6s` |
| Redis 通知丢失 | Run durable `created_at` 到数据库扫描 claim 的保守上界 `<= 3s`，且 Redis 仍停止时 Run 可完成 |
| 平台错误 | 每轮 unexpected HTTP 5xx、Run 基础设施失败、题错误和 dead-letter 均为 0 |
| 硬正确性 | 每轮 duplicate Response/operation/audit key、ledger→scope/minute projection drift、overdraw、active/reserved residue、最终 Redis PEL/lag 均为 0；固定故障画像为 `270 actual + 1 conservative` |
| 公平 | 每轮低流量 Model 在高流量 backlog 排空前取得 durable claim 和至少一个 slice |

Mock 答案正确率不是平台 SLI。`p99` 只作描述，不参与 pass/fail；小样本下它接近最大值。

### 3. 统计与实验纪律

- 一个资格 suite 先运行 1 个完整 warm-up trial；warm-up 必须通过所有硬不变量，但不进入性能样本。随后串行运行至少 5 个 measured trial，每轮使用新的 Compose project、空 PostgreSQL/Redis volume 和完整 cleanup。
- 每个 measured trial 的三个性能 cell 各有 4 Run/60 题；五轮合计每 cell 20 Run/300 题。测量顺序由固定 seed 预先生成并在单/双 Worker 顺序间平衡，不在看到结果后调整。
- 吞吐由 `completed_questions / wall_duration_seconds` 独立重算，以 trial 为统计单位。报告 `n/mean/median/min/max/sample standard deviation/CV/two-sided 95% mean CI`，判定使用 one-sided 95% Student-t lower bound；不得删除异常轮。双/单 Worker scaling 使用同一 trial 内的配对比值。
- queue=`started_at-created_at`、execution=`finished_at-started_at`、E2E=`finished_at-created_at`，全部来自数据库 UTC。每轮独立计算 p95 并逐轮过线，同时报告五轮合并的原始 Run 延迟分布。
- 0/300 题错误只能写成“观测错误为 0；one-sided 95% 零事件上界约 1%”。不得据此宣称真实错误率为 0、99.9% 可用性或生产 SLA。
- command failure、超时、脏工作树、commit/config/environment/script 漂移、非有限数、缺失/重复 cell、任何硬不变量或 cleanup 失败，都会使整个 suite 失败。失败 evidence 保留；不得仅重跑失败轮或选择成功样本覆盖。本地 artifact 能证明一次 invocation 内没有丢轮，不能独立证明操作者没有删除更早的整个 suite；正式记录必须披露所有资格尝试。
- 多轮绝对数值不进入 GitHub-hosted required job，因为其 CPU/内存与 Docker 调度不稳定。CI 继续验证统计、validator、失败路径和现有可靠性正确性；正式数值在干净候选 SHA 的受控本机执行。

### 4. 容量与参数模型

设双 Worker吞吐的 one-sided 95% LCB 为 `mu_lcb`，每 Run 题数 `Q=15`，每题由 ledger 观测的平均实际 Provider attempt 数为 `a`，安全系数 `h=0.70`，backlog `B=4`，Worker `W=2`，Redis wait bound 与 poll 合成的数据库扫描上界为 `Dscan=max(redis_block, redis_operation_timeout)+poll`，首次 delivery backoff 为 `b1=1s`，固定 Mock delay 乘 quantum 的服务预算为 `S_mock_slice=0.08×5=0.4s`：

资格合同固定数据库/调度抖动预算 `delta_db=1s`；aggregate 必须把该输入与下列公式结果一并保存，不允许各公式使用不同的隐藏余量。

- 安全题到达率：`lambda_q_safe = h × mu_lcb`。
- 安全 Run 到达率：`lambda_run_safe = h × mu_lcb / (Q × a)`。
- 无新流量时排空估算：`T_drain ~= B × Q × a / mu_lcb`。
- 首次 slice 估算：`T_first <= 2Dscan + ceil((B - 1) / W) × S_mock_slice + delta_db`。这里不使用 `Response.question_latency_ms`，因为 Mock 的该兼容字段固定为 1ms，并不代表配置的 80ms generation delay；调度与数据库余量由 `delta_db` 单独覆盖。
- 旧 lease expiry 到 claim：`T_expiry_claim <= 2Dscan + b1 + delta_db`；kill fence 到 claim 再加 `remaining_lease`。claim 后取得首次 slice 的排队项单独由 `T_first` 表达，不能混入 claim SLI。
- 连接安全条件：`(API + W) × (pool_size + max_overflow) <= max_connections - reserve`。

按最低合格 `mu_lcb=10 q/s` 和正常 Mock ledger `a=1`，`lambda_run_safe ~= 0.467 Run/s`，无新流量的四 Run backlog 排空估算约 6 秒；正式目标保留到 10 秒。只有 `send_started` 或结果不确定的 attempt 才计入 `a`，confirmed pre-send release 不增加实际 Provider attempt。若吞吐 SLO 未通过或 LCB 非正，容量输出必须标为 `not_qualified`，不得产生负值或除零后的容量数字。

计时保持 `lease >= 3 × heartbeat`；`poll` 只是数据库扫描发现延迟的一部分。真实 Provider 还受网络/SSE、retry、RPM/TPM、Token、价格和 Provider 排队限制，严禁用 `W / Mock latency` 或本模型外推 Provider 能力。

### 5. Evidence 合同

- 新增独立 `phase2-slo` schema 与 gitignored artifact 根；既有 capacity v1 历史 evidence 不改写。
- aggregate 只保存严格 allowlist：精确 commit、脚本/Compose SHA-256、稳定配置/环境指纹、每轮 evidence 相对路径与 SHA-256、脱敏 SLI 样本/统计/判定和 cleanup 摘要；不复制 child stdout、日志、DSN、URL、环境变量、题目、prompt/response、keyring/envelope 或 Provider 数据。子进程只继承运行本地 Git/Docker 所需的环境 allowlist，使用独立进程组，并给 scoped cleanup 420 秒完成窗口。
- 每轮 child evidence 必须为 Mock-only、finite policy、clean commit、完整三 cell/三 fault、公平、reconciliation 和 cleanup；不能只信 `status=passed`。
- aggregate 与 child raw evidence 都留在 `.pytest_cache/`，不提交、不自动上传。文档只引用相对本地路径、SHA-256、公开 commit 和允许的统计摘要。

## Consequences

### Positive

- P2-01 从一次性能快照变成运行前冻结、可重复且 fail-closed 的资格合同。
- 正确性不变量优先于平均吞吐，宿主/配置漂移不会被静默混入。
- 部署默认计时得到真实故障证据，快速 capacity 仍可用于日常回归。

### Negative

- 一次正式 suite 需要 1 warm-up + 5 measured full-stack trials；30 秒 lease 会使本机运行达到十余分钟。
- 单主机 Mock 小样本只能限定发布资格，不能建立生产 HA 或 Provider SLA。
- Hosted CI 不执行绝对性能门禁；复核依赖精确 SHA 的本地 evidence hash 和公开 CI 的正确性门禁共同成立。

## Validation

- 离线单元测试覆盖统计已知值、阈值方向/边界、零事件上界、JSON/path/指纹、失败 child、cleanup、dirty/mixed trial、secret canary 与 aggregate allowlist。
- `phase2-capacity` 默认 `6/2/0.15s` 行为与 v1 schema 回归通过；`phase2-slo` 固定 profile 使用 `30/10/1s`。
- 精确干净实现 SHA 上实际完成 warm-up + 5 measured trials，所有 SLO 与 hard invariant 通过并留下 SHA-256。
- 实现 commit 与只更新最终 evidence 的文档 commit 分开；两者普通 push，最终精确 SHA 的 GitHub Actions 必需 job 全部成功。

## Rollback

本决定不改变数据库、公共 API、评分协议或生产默认配置。回滚 wrapper/Make target 与 additive capacity evidence 字段不会破坏旧 capacity v1 reader；历史 aggregate 留在 gitignored artifact。若实现回滚到没有正式 timing/validator 的版本，就不得继续宣称 `P2-local-control-plane-v1` 资格通过。
