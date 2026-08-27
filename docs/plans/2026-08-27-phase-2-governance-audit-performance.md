# Phase 2 并发治理、审计与性能基线执行计划

- Owner: Codex
- Status: completed (governance/audit candidate slice; Phase 2 remains in progress)
- Created: 2026-08-27
- Updated: 2026-08-28
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-27 工作日志](../worklogs/2026-08-27-phase-2-governance-audit-performance.md)
- ADRs: [ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0007](../decisions/ADR-0007-web-provider-credentials.md)、[ADR-0008](../decisions/ADR-0008-openai-compatible-sse-transport.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](../decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0011](../decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)

## Context

Phase 2 已有 PostgreSQL 任务事实来源、Redis at-least-once 通知、独立 Worker、租约/fencing、幂等 Response 和故障恢复。本计划新增的治理/审计切片已作为 SHA `665244e095905083b606b8e98e946ed1a02dc0fc` 交付：`0004`、数据库权威四层治理、逐 Provider HTTP attempt ledger、有限 question quantum、typed audit/history、Provider metadata、credential audit、前端治理状态和增强容量/真实 PostgreSQL 测试。最终全量门禁、精确 SHA 真实负载/故障证据、push 与远程 4/4 CI 均已完成；正式 Phase 2 闭环由 NEXT_TASK 继续，因此 Phase 2 本身仍保持进行中。

## Objective

在不改变 `llmbenchlab-protocol-v1` 评分且不调用真实 Provider 的前提下，交付数据库可恢复的 global/provider/model/run 并发、速率与预算治理，提供确定背压和有限公平调度，以固定非秘密 schema 串联历史审计；随后用精确候选 SHA 的真实 PostgreSQL/Redis 容量、故障和远程 CI 证据收敛本切片，同时把正式 SLO、Exporter/告警、审计归档、备份恢复和 Worker 进展探针留在 Phase 2 closure 中继续跟踪。

## Scope

- Alembic `20260827_0004`、六类新治理/审计表、Run/Response 证据字段及 12 表 importer。
- active policy 唯一性、canonical hash、managed Run 的 policy/override 冻结与 `legacy_unmanaged` 兼容边界。
- 四层 scope、固定 UTC 分钟 RPM/TPM、lifetime request/Token/USD budget、逐 attempt reservation/send-started/actual/conservative/pre-send release。
- ledger 重算校验；materialized scope/bucket counter 或冻结 policy/override 漂移时 fail closed，并写固定完整性事件。
- Adapter 每 HTTP attempt hook、Runner/lease reconciliation、有限 question quantum、due/backlog 排序和 `202/429` 背压。
- typed audit、Run audit、task history/latency、Provider metadata、credential 生命周期非秘密事件。
- Run Detail 的 managed/delayed/exhausted 状态、原因和 UTC not-before 提示。
- Mock-only 增强 capacity/acceptance 脚本、真实 PostgreSQL 竞争测试、运维/性能文档与最终交付门禁。

## Non-goals

- 真实 Provider 调用、账单核对或 Provider exactly-once。
- 认证、多租户、生产 KMS、公共部署、Kubernetes、生产 HA 或 SLA 声明。
- 本切片直接交付 Prometheus/OTel Exporter、正式告警、审计 retention archive、备份恢复认证或 Worker 主循环进展探针；这些留给下一 Phase 2 closure。
- 新 Benchmark、代码沙箱、Judge、Arena、Agent 或 protocol-v1 评分变化。

## Assumptions and invariants

- PostgreSQL 提供多 Worker 行锁；SQLite 只支持单 Worker，并以 `BEGIN IMMEDIATE` 保持相同事务不变量。
- 数据库及数据库时钟是额度、任务、窗口和审计时间的唯一权威；Redis 不参与治理裁决。
- 硬 Token/费用治理要求有限输入预留、输出上限和价格；缺失时 managed Run 在发送前 fail closed，不能按零推断。
- materialized counters 是 ledger 的加速投影，不是第二事实来源；任何高/低漂移都中止后续 admission/mutation/reconcile/import。
- cooperative yield 不增加失败数；只有已进入或无法确认是否进入 transport 的 attempt 消耗 Provider retry ordinal。
- ADR-0011 规定确认未发送的 release 以新 ledger generation 保留同一未发送 ordinal；never-delete ledger 不回写或删除。
- ADR-0010 规定可信本地 CLI 继续 `legacy_unmanaged`、不写 synthetic ledger；Provider 不安全 metadata 归一化为 `null`；Run 延迟来自数据库 Run 时间戳；credential audit 不保存 origin。

## Implementation steps

1. [completed] 固化 ADR 与设计合同。
   - ADR-0009 定义治理/审计主合同；ADR-0010 收窄 CLI、metadata、延迟和 credential 审计边界；ADR-0011 修复确认 pre-send release 的 retry/generation 语义。
2. [completed] 建立数据库 schema、治理 repository 与完整性边界。
   - 已实现 `0004`、六类表、active policy 唯一约束、Run/Response 字段、12 表 importer、ledger 重算和 counter/policy/override 漂移 fail-closed 测试。
3. [completed] 接入执行路径、背压和公平调度。
   - Adapter 内每 HTTP retry attempt 进入 reserve/mark/finish；Runner 与 lease/reconciler 处理延迟、保守结算和失租；question quantum、dispatch/failure 分离及 due/backlog 顺序已接入。
4. [completed] 交付历史指标、审计、API 与 UI 切片。
   - 已实现 policy GET/PUT、Run governance/read fields、Run audit、task history/latency、credential audit、Provider evidence 和 Run Detail 治理状态；payload 使用固定 allowlist。
5. [completed] 形成可执行容量/故障工具和 Runbook 切片。
   - 增强 capacity 脚本已覆盖有限 policy、并发 backlog `202/429`、小于 15 题的 quantum、跨 Model 公平、Worker/Redis 故障与 ledger/audit 对账；真实 PostgreSQL 测试覆盖 RPM/TPM/lifetime budget、backlog、settlement/reconcile race 和 audit replay。最终精确候选 SHA 的实际运行属于步骤 6。
6. [completed] 全量门禁与本切片交付闭环。
   - 冻结候选树后重跑 lint/test/smoke、双方言迁移、真实 PostgreSQL integration、增强 capacity、acceptance、Compose、diff/secret 检查。
   - 三个确定性 DB seam injection（reserved、send_started、response_committed）及断言已加入，脚本单测和冻结候选的 9/9 完整 Compose acceptance 均通过。
   - 独立实现 commit 已 push PR 分支；精确 SHA `665244e…` 的 GitHub Actions run `33099260233` 四个必需 job 全部成功。
   - Phase 2 仍保持 `in_progress`，并把正式 SLO/容量模型、Exporter/告警、retention archive、备份恢复和 Worker progress/liveness 作为下一 closure 合同。

## Risks

| 风险 | 控制 | 剩余边界 |
| --- | --- | --- |
| 四层锁死锁或限额突破 | canonical scope、global→provider→model→run 锁序、真实 PG 竞争测试 | 精确候选 integration/capacity 已通过；仍需生产规模参数校准 |
| materialized counter 被破坏 | 每次关键 mutation 前从 ledger 聚合并 fail closed | 管理员仍可直接篡改数据库；不是 WORM |
| Provider attempt/commit 裂缝 | `reserved` 可释放，`send_started` 保守结算，终态唯一键 | Provider 响应后本地提交前仍可能重复外部调用/费用 |
| fixed-window 边界突发 | 数据库 UTC 窗口、typed not-before、容量证据 | 不等同平滑 token bucket 或 Provider SLA |
| 公平改造破坏 retry | dispatch 与 failed count 分离、ADR-0011 generation/ordinal | 精确候选双 Worker与 deterministic seam evidence 已通过；仍非 Provider SLA |
| 审计高基数或泄密 | event/payload allowlist、无正文/URL/Key、分页 | retention archive/exporter 尚未交付 |
| importer/migration 数据损失 | 停写只读源、空目标、单事务、12 表 fingerprint、downgrade guard | 备份恢复演练仍属 Phase 2 closure |

## Validation status

| 验收项 | 已实际发生的结果 | 最终门禁状态 |
| --- | --- | --- |
| 最新本地静态门禁 | `make lint` 通过 | 本地冻结树与精确实现 SHA 远程门禁通过 |
| 最新本地全量测试 | `make test`：后端 `604 passed, 29 skipped`；前端 `38 passed` | 收尾文档前重跑通过 |
| 最新真实基础设施 | PostgreSQL/Redis integration `29/29 passed` | 本地通过；同一实现 SHA 的远程 integration 通过 |
| acceptance seam 脚本 | 三条 deterministic DB seam 场景；定向脚本测试 `19 passed`，完整 Compose 9/9 | 精确 `665244e…` 通过，evidence `ab311665…ddec` |
| 最新本地 Smoke | `make smoke`：`1 passed, 7 deselected`，仅 Mock | 本地冻结树通过；未调用真实 Provider |
| 定向治理/API/Worker | 目标套件曾通过；另一次审计为 `218 passed` | 已由最终全量、真实 integration 与候选 evidence 补充 |
| SQLite/PostgreSQL migration | 隔离 SQLite 与临时 PostgreSQL 16 的 prepare/upgrade/downgrade/upgrade/check 通过 | 本地通过；精确实现 SHA 的远程 integration 亦通过 |
| Compose config | `docker compose config --quiet` 通过 | 最新本地冻结树通过 |
| enhanced capacity | 1W `7.306981`、2W `13.396740`、burst `8.585309` q/s；4×202+2×429，公平/故障/对账通过 | 精确 `665244e…`，evidence `40deadeb…0588`，Mock-only 非 SLA |
| 远程门禁 | SHA `665244e095905083b606b8e98e946ed1a02dc0fc` 的 run `33099260233` | 4/4 成功；PR #1 未合并 |

## Rollback

停止 API/Worker 后关闭新 admission，并对账所有 active reservation。只有六类治理/审计表为空、没有依赖 `0004` 字段的 active managed Run 时才允许 downgrade；不得删除已结算 ledger/audit 证据。旧代码不能识别 `0004` 时保留 schema 并回滚到兼容提交。Redis 不含权威额度，不能用于恢复。数据库与 keyring 继续独立备份，迁移和负载测试只使用隔离环境。

## Documentation updates

- [x] README / 用户操作说明
- [x] API / Architecture / Security
- [x] Testing / Deployment / Operations / Performance
- [x] ADR-0010 / ADR-0011
- [x] CHANGELOG、PROJECT_STATUS、Roadmap、Phase 2、NEXT_TASK、工作日志
- [x] 精确候选 SHA、最终 evidence hash、CI 链接与完整命令结果

## Completion evidence

- Changed files: 实现 commit `f587691…`，admission rollback 修复 `ecf93f7…`，canonical lock-order 修复 `665244e…`；均已普通 push 到 PR #1 分支。最终 staged/security 独立审查无 blocker/high。
- Commands run: 见上表和工作日志；全量、真实 PostgreSQL/Redis integration、双方言 migration/check、enhanced capacity、9/9 acceptance、secret/diff/cleanup 与远程 CI 均实际运行。
- Acceptance evidence: capacity `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-51cfadee04f5/evidence.json`，SHA-256 `40deadeb…0588`；acceptance `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-afe52c2d54cb/evidence.json`，SHA-256 `ab311665…ddec`。两者记录 `dirty=false` 与精确 SHA `665244e…`。
- Known issues: Phase 2 正式 SLO/容量模型、Exporter/告警、审计归档、备份恢复、Worker progress/liveness 与剩余生产式故障矩阵仍未闭环。

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
| --- | --- | --- | --- |
| 2026-08-27 | discovery | retry 位于 Adapter 内；Runner 外层治理会漏记。 | 采用 per-attempt hook。 |
| 2026-08-27 | discovery | Worker 整 Run 独占，FIFO claim 不能提供有限公平。 | 采用 question quantum、cooperative yield 与 due ordering。 |
| 2026-08-27 | decision | unknown usage/commit outcome 不按零释放。 | 完整预留转 conservative consumed。 |
| 2026-08-27 | decision | audit 是应用 append-only，不是防 DB 管理员篡改的 WORM。 | read/write allowlist 与完整性 fail-closed。 |
| 2026-08-28 | amendment | CLI、Provider metadata、Run latency 和 credential origin 按 ADR-0010 收窄。 | 避免未实现或高基数数据面被误报为交付。 |
| 2026-08-28 | bug fix | 确认 pre-send release 曾消耗零 HTTP 的 retry ordinal。 | ADR-0011 以新 generation 保留未发送 ordinal。 |
| 2026-08-28 | review | materialized counter 可被低报后绕过限额。 | admission/mutation/reconcile/import 改为 ledger 重算 fail closed。 |
