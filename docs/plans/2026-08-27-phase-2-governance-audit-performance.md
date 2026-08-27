# Phase 2 并发治理、审计与性能基线执行计划

- Owner: Codex
- Status: active
- Created: 2026-08-27
- Updated: 2026-08-27
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-27 工作日志](../worklogs/2026-08-27-phase-2-governance-audit-performance.md)
- ADRs: [ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0007](../decisions/ADR-0007-web-provider-credentials.md)、[ADR-0008](../decisions/ADR-0008-openai-compatible-sse-transport.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)

## Context

Phase 2 已有 PostgreSQL 事实来源、Redis 通知、独立 Worker、租约/fencing、幂等 Response、重试/取消/dead-letter 和真实故障证据。剩余缺口是 P2-05 的跨 Run 治理、P2-06 的历史指标/审计和 P2-07 的容量基线/Runbook。现有 Worker 一次执行整个 Run，OpenAI-compatible Adapter 在内部重试，故实现必须同时进入数据库、Adapter、Runner、Worker、API、安全与运维边界。

## Objective

在不改变 protocol-v1 评分且不调用真实 Provider 的前提下，让 LLMBenchLab 以数据库原子裁决四层并发、速率和预算，能从崩溃/重投中恢复额度，以有限 question quantum 公平调度，并用非秘密历史审计、真实 PostgreSQL/Redis 负载证据和 Runbook 完成 Phase 2 的剩余关键验收。

## Scope

- 0004 双方言 migration、新治理/审计模型、Run/Response 可观测字段和 importer。
- global/provider/model/run scope、reservation/settlement/release/unknown、固定分钟窗口和累计预算。
- Adapter 每 HTTP attempt hook、Runner backpressure、lease heartbeat/reconcile 与 cooperative slice yield。
- backlog admission、稳定 API 字段/错误、历史 metrics 与 Run audit 查询。
- credential 生命周期非秘密审计和 Provider 元数据持久化。
- 前端 backpressure/治理状态提示。
- 隔离双 Worker 容量脚本、基线、故障一致性与运维 Runbook。
- 全部相关文档、测试、stage commit/push/CI。

## Non-goals

- 真实 Provider 调用、账单核对、Provider exactly-once。
- 认证、多租户、KMS、Prometheus/OTel、公共部署、Kubernetes、生产 HA/SLA。
- 新 Benchmark、代码沙箱、Judge、Arena、Agent。
- protocol-v1 评分或排行榜语义变化。

## Assumptions

- PostgreSQL 提供多 Worker 行锁；SQLite 以 `BEGIN IMMEDIATE` 保持单 Worker兼容。
- 所有 policy 数值有显式边界，所有消费事实与 DB 时间持久化；Redis 不参与裁决。
- 硬 Token/费用治理需要有限 Token 上界和价格；缺失时调用前失败，不按零推断。
- question quantum 是调度让出，不是执行失败；claim count 和 failed-attempt count 必须分离。

## Requirements

- `docs/NEXT_TASK.md` 的 P2-05、P2-06、P2-07 全部范围与验收。
- ADR-0005 的 DB fact source、lease/fencing、Response idempotency 和 Provider non-exactly-once 不变量。
- ADR-0007 的 write-only Key、AES-GCM envelope、数据库外 keyring、origin/active-Run 门禁。
- ADR-0008 的真 SSE、严格 `[DONE]`、identity-only 和资源上限。
- `llmbenchlab-protocol-v1` 评分、分母和可比性保持不变。

## Implementation steps

1. [completed] 固化 ADR 与设计合同。
   - Files/modules: ADR-0009、本计划、工作日志。
   - Validation: 文档明确 scope lock order、reservation 状态机、未知 usage、slice/failure 分离、审计/回滚。
2. [in_progress] 建立数据库 schema 与治理 repository。
   - Files/modules: migration 0004、models、governance package、importer、migration tests。
   - Validation: SQLite + PostgreSQL 原子竞争、upgrade/downgrade/check、导入 digest。
3. [pending] 接入执行路径和公平调度。
   - Files/modules: adapters、Runner、run leases、Worker、run service/config。
   - Validation: 每 HTTP retry attempt 记账；四层竞争不越界；过期恢复；quantum 公平性。
4. [pending] 交付历史指标、审计、API 与 UI。
   - Files/modules: audit repository、health/runs/models API、schemas、Run Detail、测试。
   - Validation: counter/latency 无 double-count；全链路关联；credential payload 无秘密。
5. [pending] 形成容量/故障基线和 Runbook。
   - Files/modules: capacity script、Makefile/Compose、Deployment/Testing/Security。
   - Validation: 真实 PostgreSQL/Redis + ≥2 Worker，输出脱敏 JSON evidence 与 p50/p95/p99。
6. [pending] 全量门禁与交付闭环。
   - Files/modules: README、CHANGELOG、PROJECT_STATUS、ROADMAP、Phase 2、NEXT_TASK、工作日志/计划。
   - Validation: 必跑命令零失败、diff/secret 审查、阶段 commit/push、精确 SHA 4/4 CI。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
| --- | --- | --- | --- |
| scope 锁死锁 | 中/高 | 规范化并固定锁序 | 回滚事务，保留 pending，审计 conflict |
| Provider attempt 与 DB commit 裂缝 | 高/中 | 过期 reservation 按完整预留保守结算 | 标记 unknown，不宣称账单精确 |
| 固定窗口边界突发 | 中/中 | 文档化 DB-time fixed window | 基线记录，不冒充平滑令牌桶 |
| 长 Run 公平改造破坏 retry | 中/高 | failed count 与 slice count 分离 | 定向状态机/故障测试后再启用 |
| 审计高基数/泄密 | 中/高 | 字段 allowlist、分页、retention class | fail closed 丢弃非法 payload，不记录正文 |
| importer/migration 数据损失 | 低/高 | 临时库、显式 downgrade guard、digest | 停止升级，按备份恢复；不碰用户默认库 |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
| --- | --- | --- | --- |
| 目标治理测试 | `cd backend && uv run pytest -q <governance targets>` | 全部通过 | 待执行 |
| 后端/前端全量 | `make test` | 零失败 | 待执行 |
| 静态门禁 | `make lint` | 零失败 | 待执行 |
| 离线协议链路 | `make smoke` | Mock smoke 通过 | 待执行 |
| 双方言 migration | Alembic upgrade/downgrade/upgrade/check | SQLite/PG 均通过 | 待执行 |
| 真实基础设施 | integration marker | PostgreSQL/Redis 零 skip | 待执行 |
| 可靠性故障 | `make phase2-acceptance` | 所有场景通过 | 待执行 |
| 容量基线 | `make phase2-capacity` | 双 Worker evidence 完整 | 待执行 |
| Compose | `docker compose config --quiet` | exit 0 | 待执行 |
| 远程门禁 | GitHub Actions 精确 SHA | 4/4 success | 待执行 |

## Rollback

停止 API/Worker 后先关闭新 admission，再等待或对账所有 active reservation；只有治理/审计新表为空且没有依赖 0004 字段的 active Run 时才允许 downgrade。代码回滚不得删除已结算 ledger/audit 证据；若旧代码不能识别 0004，则保持 schema 并回滚到兼容提交。Redis 不含权威额度，不能用于恢复。数据库与 keyring 继续独立备份，任何 migration/负载测试只针对隔离环境。

## Documentation updates

- [ ] README / 用户操作说明
- [ ] API / 数据格式 / Benchmark 协议
- [ ] Architecture / Security / ADR
- [ ] Testing / Deployment / Runbook
- [ ] CHANGELOG、PROJECT_STATUS、Roadmap、Phase 2、NEXT_TASK、工作日志

## Completion evidence

- Changed files: 待完成
- Commands run: 待完成
- Acceptance evidence: 待完成
- Not run: 当前无最终验证
- Known issues: 当前任务仍在设计/实施中

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
| --- | --- | --- | --- |
| 2026-08-27 | discovery | retry 位于 Adapter 内；Runner 外层治理会漏记。 | 采用 per-attempt hook。 |
| 2026-08-27 | discovery | Worker 整 Run 独占，FIFO claim 不能提供有限公平。 | 采用 question quantum + cooperative yield。 |
| 2026-08-27 | decision | unknown usage/commit outcome 不按零释放。 | 完整预留转 unknown consumed。 |
| 2026-08-27 | decision | audit 是应用 append-only、可审计但非不可篡改。 | 明确 DB 管理员仍可修改，禁止冒充 WORM。 |
