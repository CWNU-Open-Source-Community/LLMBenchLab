# Phase 2 恢复与运维闭环执行计划

- Owner: Codex
- Status: in_progress
- Created: 2026-08-28
- Updated: 2026-08-28
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-28 工作日志](../worklogs/2026-08-28-phase-2-recovery-operations.md)
- ADRs: [ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0007](../decisions/ADR-0007-web-provider-credentials.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0015](../decisions/ADR-0015-observability-worker-progress-audit-retention.md)、[ADR-0016](../decisions/ADR-0016-postgresql-keyring-recovery-and-redis-rebuild.md)

## Context

P2-01 和 P2-06 已分别完成单机控制面资格与可观测性/retention 仓库级门禁。当前 PostgreSQL 是任务唯一事实来源，Redis 只是通知；13 表 importer、数据库外 keyring、audit archive、Worker DB-time progress、固定 exporter/八规则和 9/9 acceptance 均已实现，但仓库尚未认证 PostgreSQL+keyring 配对恢复、空 Redis rebuild、规则时序/响应和剩余真实故障矩阵。P2-07 是 Phase 2 的最后约定切片，必须使用新的计划/日志/ADR，不复用 P2-06 evidence 冒充恢复认证。

## Objective

在不调用真实 Provider、不改变 API/评分协议、不提供危险生产 mutation 命令的前提下，交付 P2-07：PostgreSQL 16 custom dump 与独立 keyring 的 canonical 配对验证、空目标 13 表精确恢复、Redis replacement 的数据库恢复路径、1↔2 Worker/八规则响应及真实 PostgreSQL/Redis 故障矩阵，并以独立 commit、clean-SHA evidence、push 和精确 SHA CI 完成仓库级闭环。

## Scope

- 抽取 importer 的 13 表 canonical snapshot 与完整性检查为公共只读恢复基础。
- 严格 recovery manifest、artifact/keyring 文件边界和只读 `llmbenchlab-recovery-verify`。
- 标准 PostgreSQL 16 `pg_dump`/`pg_restore` 运维合同与空目标/结果分类，不自制 restore mutation。
- Redis group/NOGROUP/replacement、数据库 scan、orphan PEL/lag/duplicate 的真实恢复验证。
- Compose expected minimum 与 1↔2 Worker 扩缩顺序修正。
- 八规则 promtool 时序 fixture 与真实底层 symptom/Runbook 演练。
- dead-letter、commit outcome unknown、governance integrity/overdraw、cancel/retry/lease/budget 等剩余真实 PostgreSQL/Redis 故障矩阵。
- 独立 `phase2-recovery` harness、Make/CI 接线、严格 evidence allowlist 与相关运维/安全/测试/状态文档。

## Non-goals

- 生产 backup scheduler、WAL/PITR、RPO/RTO、HA、多区域、Kubernetes、对象存储、KMS/HSM 或签名/WORM。
- PostgreSQL/Redis destructive REST API、通用 DROP/TRUNCATE、`pg_restore --clean/--create`、`FLUSHALL`、删 Stream/group 或 DB→Redis replay。
- Alertmanager sender、通知路由、silence/ack 系统、值班平台或生产告警 SLA。
- 第三个 Worker、不同硬件/profile、真实 Provider、费用/exactly-once 或公共多租户。
- 批量 credential 重加密、旧 key 自动删除、Benchmark/协议/评分/前端产品功能变化。

## Assumptions

- PostgreSQL 16 官方工具是 dump/restore 的事实实现；仓库只验证工件、数据库和 keyring，不重复实现数据库恢复。
- 恢复源在维护窗口停写；restore 目标为随机、隔离、预创建的空数据库。
- keyring 与数据库 dump 分开存储、使用不同访问控制；manifest 是敏感内部配对元数据，SHA 不是认证。
- 自动化只使用 Demo/Mock、假 stored credential 和隔离 PostgreSQL/Redis；不会构造真实 Provider Adapter/HTTP 请求。
- GitHub Actions 现有四个 required job 名保持不变；P2-07 harness 加入 full-stack job。

## Requirements

- [NEXT_TASK P2-07](../NEXT_TASK.md) 的 backup/keyring、Redis rebuild、alert response 与 fault matrix 范围。
- AGENTS §2 的 ADR/计划/日志先行、§3.2 的安全/部署/文档联动、§4 验证和 §5 exact-SHA 远程闭环。
- ADR-0005 的 PostgreSQL truth/Redis notification、ADR-0007 的数据库外 keyring、ADR-0009/0015 的 ledger/audit/retention 和 ADR-0016 的恢复合同。
- 现有 API、`llmbenchlab-protocol-v1`、P2-06 9/9 acceptance 与 P2-01 SLO 合同保持兼容。

## Implementation steps

1. [completed] 完成只读勘察并冻结 ADR-0016。
   - Files/modules: keyring/credentials、importer/snapshot、queue/Worker、Compose/CI、alert rules/Runbook、acceptance/capacity。
   - Result: 标准 PG16 工具 + 只读 verifier、分离 keyring、canonical manifest、空目标、Redis replacement、八规则双层演练、destructive/evidence guard 已冻结。
   - Validation: 核对 P2-06 clean 状态、现有 13 表/queue/Worker/alert 实现及文件/测试边界；无工作区重叠。
2. [pending] 实现共享恢复完整性、manifest 与只读 verifier。
   - Files/modules: `backend/app/db/recovery_integrity.py`、`backend/app/recovery/`、`backend/app/cli/recovery_verify.py`、`pyproject.toml`、`import_sqlite.py` 和目标测试。
   - Validation: canonical round-trip、13 表固定顺序、strict JSON、offline import、nofollow/owner/mode/size/race、dump/keyring swap、wrong/missing/tampered keyring、Model/active Run AAD、零 Provider I/O、结果/错误输出固定。
3. [pending] 实现 Redis rebuild、Worker expected 扩缩与真实 integration。
   - Files/modules: `compose.yaml`、Worker/queue、Redis integration/Worker tests。
   - Validation: NOGROUP reset、group `0-0` rebuild、orphan PEL/XAUTOCLAIM、ACK unknown/duplicate no-op、DB scan、1↔2 expected/live/stopped/ledger 收敛。
4. [pending] 实现八规则时序 fixture与 P2-07 recovery harness。
   - Files/modules: `deploy/observability/`、`scripts/phase2_recovery.py`、Makefile、CI、script tests。
   - Validation: promtool 八规则 before/fire/clear；PG custom dump→空目标 restore→13/13 exact；matching/wrong/missing keyring；Redis volume replacement；八 symptom/Runbook；dead-letter/integrity/overdraw/lease/cancel/retry/budget/commit-unknown；strict allowlist evidence/scoped cleanup。
5. [pending] 完成代码/安全/文档联动与完整本地门禁。
   - Files/modules: README、Architecture、Security、Deployment、Operations、Testing、CHANGELOG、PROJECT_STATUS、Roadmap、Phase 2、NEXT_TASK、本计划/日志。
   - Validation: target/full pytest、Ruff、frontend、smoke、双方言 migration、real PG/Redis、promtool、Compose config、P2-06 regression、P2-07 recovery、链接/secret/diff/staged B/H/M review。
6. [pending] 形成 clean implementation/evidence/status commit 并完成远程门禁。
   - Validation: 独立 implementation commit/push；在该 clean SHA 从零运行正式 P2-07 recovery evidence；证据文档 commit/push；每个精确 SHA 的四个必需 GitHub Actions job 全 success；Phase 2 在最后门禁前保持 `in_progress`。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| restore 覆盖用户数据库 | 低/极高 | verifier 只读、空目标、随机隔离名、禁 clean/create/drop/truncate | 停止流程，保留目标，人工确认；不自动清空 |
| dump 与 manifest 不是同一停写快照 | 中/高 | 维护窗口停 writer、恢复后 13 表 exact comparison | 整组 fail closed，重新建立新备份集 |
| DB/keyring 一起泄漏 | 中/极高 | 分开存储/ACL、短时配对、evidence 不含内容或指纹 | 停止传播，轮换 Provider credential 和 keyring；保留审计 |
| wrong keyring 在零 envelope 时空集通过 | 中/高 | 先比较完整 keyring bytes digest，再做 AES-GCM/AAD | 拒绝恢复，不构造 Adapter/HTTP |
| Redis 删除扩大到 PG/共享 volume | 低/极高 | exact project/container/volume label+identity，禁 glob/prune/down-v 中途 | 拒绝删除；只报告固定 cleanup failure |
| 新 Redis 后 Worker 永久认为 group 已初始化 | 中/高 | queue failure 重置 initialized/cursor，NOGROUP/real rebuild 回归 | DB scan 继续，修复初始化后再恢复通知 |
| 告警演练靠缩短/删除事实“碰绿” | 中/高 | promtool synthetic time + 真实 symptom 双层 AND；持久事实不清除 | 保留失败 evidence，修合同后从零重跑 |
| Compose/CI 时间或噪声过大 | 中/中 | 新独立 harness、固定有界场景、保持 required job 名 | 记录真实失败，优化确定性，不降低断言 |
| 备份点后 Provider 外部副作用丢失 | 中/高 | 明确非 exactly-once，保守 settlement 与账单/request ID 对账 | 停止新流量，人工外部对账，不能由 Redis 补造 |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| Manifest/verifier | 目标 pytest + CLI subprocess | strict/canonical/offline/secret-safe 全通过 | 待执行 |
| Recovery integrity | SQLite 单测 + PostgreSQL integration | 13 表与治理/audit/keyring 全 exact | 待执行 |
| Redis/Worker | unit + real Redis/PostgreSQL integration | NOGROUP/group/PEL/duplicate/scale 收敛 | 待执行 |
| Alert rules | parser tests + Prometheus `promtool test rules` | 八规则 before/fire/clear 全通过 | 待执行 |
| P2-07 Compose | `make phase2-recovery` | backup/restore/rebuild/drill/fault/cleanup 全通过 | 待执行 |
| P2-06 regressions | `make phase2-acceptance`、目标 capacity | 冻结 9/9 与既有 capacity 合同不回归 | 待执行 |
| Backend | target + full `pytest` | 全部通过，integration 环境项明确 | 待执行 |
| Lint/build | `make lint`、frontend test/build | 全部通过 | 待执行 |
| Smoke/deployment | `make smoke`、`docker compose config --quiet` | Mock-only、配置合法 | 待执行 |
| Security/docs | staged secret/destructive/evidence scan、Markdown links/diff | 0 未解决 Blocker/High/Medium | 待执行 |
| Remote exact SHA | 普通 push + GitHub Actions | 四个 required job 对每个精确 SHA 全绿 | 待执行 |

## Rollback

先停止 recovery harness 和 verifier 调用。只读 verifier/manifest 代码可回滚而不改变 schema/API；已经创建的 dump、manifest、keyring backup 和恢复目标是敏感运维资产，不能由代码回滚自动删除。Redis/Worker 修复可回退代码但不得覆盖 PostgreSQL 或删除 durable facts。任何 restore commit outcome unknown、partial target 或 cleanup failure 都保持隔离，禁止 `--clean`/DROP/force；由只读 verifier 与操作者决定后续。Compose expected 环境重定位若回滚，必须同时还原部署文档并披露 running API 不会自动更新 expected。

## Documentation updates

- [ ] README / 用户恢复入口和非 DR 边界
- [ ] Architecture / PostgreSQL truth、配对 manifest、Redis rebuild
- [ ] Security / dump+keyring 分离、文件/evidence/CLI 输出边界
- [ ] Deployment / PG16 标准工具、空目标、Worker scale 顺序
- [ ] Operations / 完整 backup/restore/Redis/八规则 Runbook 演练步骤
- [ ] Testing / test layers、Mock-only、real PG/Redis 与 evidence
- [ ] CHANGELOG、PROJECT_STATUS、Roadmap、Phase 2、NEXT_TASK、工作日志
- [ ] GitHub workflow / 保持 required job 名、只上传 recovery allowlist summary，并修正历史 acceptance 场景数口径
- [ ] API / 不适用：不新增或改变 REST 路径/schema/status；在计划/日志记录此判断
- [ ] Benchmark/Data format / 不适用：协议、数据 schema、评分不变

## Completion evidence

- Changed files: 待实施后记录。
- Commands run: 待实际执行后记录。
- Acceptance evidence: 待 clean implementation SHA 运行后记录；dirty/preflight 结果不得冒充正式证据。
- Remote evidence: 待 commit/push/exact-SHA CI 后记录。
- Not run: 当前尚未进入实施验证。
- Known issues: Phase 2 仍为 `in_progress`；P2-07 未完成，不能宣称生产 DR、PITR/RPO/RTO、HA、Alertmanager 或 Provider exactly-once。

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-28 | discovery | P2-06 implementation/evidence/status 三个 SHA 均已 push 且精确 SHA CI 4/4；工作树 clean。 | 可启动独立 P2-07，不复用 P2-06 dirty/clean artifact 冒充恢复证据。 |
| 2026-08-28 | discovery | Backend image 不包含 host `pg_dump/pg_restore`；Compose PostgreSQL 16 image包含标准工具。 | Harness 使用隔离 PG container；生产文档要求部署提供同 major 标准工具。 |
| 2026-08-28 | discovery | importer 已有 13 表 canonical summary 与 governance/audit preflight，但语义仍是 SQLite→PG mutation。 | 抽取公共只读 integrity 模块，避免 recovery verifier 依赖 importer 私有函数。 |
| 2026-08-28 | discovery | Worker queue failure 已重置 initialized/cursor；真实 NOGROUP/replacement 路径仍缺回归。 | 保持实现语义并补 unit/real Redis/Compose 证据。 |
| 2026-08-28 | discovery | running API 的 expected minimum 不会随 shell 中的 Worker scale 自动更新。 | Compose 只向 API 注入 expected，扩缩时按 ADR-0016 顺序重建 API。 |
| 2026-08-28 | decision | ADR-0016 拒绝生产 restore/Redis delete API，冻结标准 PG16 工具、只读 verifier、分离 keyring、空目标和 allowlist evidence。 | 实施分为 verifier、Redis/Worker、rules/harness 三条线。 |
| 2026-08-28 | scope | 用户要求避免过度工程化，本轮只落实最近的 P2-07 启动任务并停止。 | 本轮只提交 ADR、计划、工作日志和必要状态同步；步骤 2～6 保持 `pending`，不产生代码、脚本、Compose 或 CI 行为变更。 |
