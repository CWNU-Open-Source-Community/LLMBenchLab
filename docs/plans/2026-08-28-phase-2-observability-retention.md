# Phase 2 可观测性与审计保留执行计划

- Owner: Codex
- Status: active (`pending_on_docs_ci`)
- Created: 2026-08-28
- Updated: 2026-08-28
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-28 工作日志](../worklogs/2026-08-28-phase-2-observability-retention.md)
- ADRs: [ADR-0005](../decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](../decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0015](../decisions/ADR-0015-observability-worker-progress-audit-retention.md)

## Context

Phase 2 已交付 durable Worker、租约/fencing、数据库治理、typed audit/history 和单机 Mock 控制面资格。本计划开始时，数据库 API 能返回任务 gauges、typed event counters 和 Run latency，但没有稳定低基数 exporter/规则、Worker 主循环进展或可执行 audit retention。P2-06 实现已进入 clean commit `9a20676dcf545040782f04c166205d0043345753`，clean-SHA capacity/9/9 acceptance 与该实现 SHA 的远程 CI 4/4 均通过；计划仍保持 active，因为本次证据文档提交及其自身精确 SHA CI 尚未完成。

## Objective

在不调用真实 Provider、不改变评分协议且不混入整库灾难恢复的前提下，交付可抓取、可告警、可验证的 P2-06：固定低基数 exporter、Worker DB-time progress/liveness、审计 retention archive/verify/restore/delete 维护工具与完整 Runbook，并以独立 commit、push 和精确 SHA 绿色 CI 闭环。

## Scope

- 固定 Prometheus 文本 exposition endpoint，复用现有 DB current/history facts，并增加 Worker progress/stalled facts。
- Alert rules 与 Runbook，覆盖 backlog、dead-letter、governance integrity、overdraw、queue degraded、Worker stalled 和恢复持续时间。
- Worker process/progress 持久模型、Alembic、repository、Worker 主循环接线和只读 API/schema。
- Audit retention archive、verify、restore、delete CLI，内容 hash/rollup、原子输出、严格路径/权限/失败语义。
- SQLite 与 PostgreSQL 一致性、importer/schema fingerprint、Compose/config、API/安全/架构/部署/测试文档。

## Non-goals

- Prometheus/Alertmanager/OTel 服务部署、通知发送器、登录/RBAC 或公网支持。
- PostgreSQL+keyring 完整 backup/restore、Redis disaster recovery、PITR/RPO/RTO；这些属于 P2-07。
- 真实 Provider 性能、费用、exactly-once、HA 或生产 SLA。
- 高基数标签、任意日志转指标、raw audit 导出 API、自动请求链路删除或 WORM 声明。
- Benchmark/协议/评分/前端产品功能变化。

## Assumptions

- 固定 exposition renderer 足以支持当前有限指标集，避免新增生产依赖；实现前由 ADR-0015 冻结。
- Worker progress 需要新表，旧 Run/Worker 不回填虚构事实；多 Worker 仍只在 PostgreSQL 正式支持。
- Archive 是显式受控 CLI 产物，存放在仓库外/ignored 路径并作为敏感运维数据保护。
- DB current timestamp 是 progress、stale、cutoff 与 retention 的唯一时间源。

## Requirements

- `docs/NEXT_TASK.md` P2-06 全部条目。
- AGENTS §3.2 的数据库/API/安全/文档联动与 §4 验证要求。
- ADR-0005 的数据库权威任务状态与 metrics 非权威原则。
- ADR-0009 的 typed audit、90/365 天 minimum retention、先 archive/verify/rollup 后 cleanup 及非 WORM 边界。
- ADR-0010 的 Run latency 数据来源与不扩大 Provider/credential audit 数据面。

## Implementation steps

1. [completed] 勘察并冻结 ADR-0015 合同。
   - Files/modules: `backend/app/api/v1/health.py`、`schemas/system.py`、`workers/service.py`、`worker_probe.py`、`models/governance.py`、`governance/audit.py`、migration/importer、相关文档。
   - Validation: 记录现状/竞态/安全边界；ADR 明确指标名/标签、DB 压力、Worker stale、archive format/cutoff/commit outcome 和 rollback。
2. [completed] 实现 Worker progress schema、持久写入与读模型。
   - Files/modules: models、Alembic `0005`、repository/service/probe、schemas/API、migration/importer。
   - Result: generation 注册/停止、DB UTC 合并刷新、四类真实 event、inclusive live cutoff、JSON 聚合、`0005` guard 与 13 表 importer 已实现；importer committed-target postverify 使用同一 canonical integrity contract；probe 固定为 capability-only。
   - Validation: DB UTC、节流、启动/scan/claim/progress/heartbeat/stop/stale、并发/错误路径目标回归与全量回归已通过；完整计数见 Validation。
3. [completed] 实现受控 exporter 与告警规则。
   - Files/modules: health/observability API、Prometheus renderer、规则文件、规则验证测试、Compose/config。
   - Result: 固定 text `0.0.4` gauge、单 DB-time snapshot、audit/latency hard cap、single-flight、固定八规则/抓取示例与 Runbook 已实现；取消中的 collection 在同步 DB 工作结束前继续持有 gate。
   - Validation: 固定 content type/name/labels、audit/DB/renderer fail closed、取消竞态、无对象 ID/secret label和规则合同目标/全量回归已通过；Compose 告警链路仍属步骤 6。
4. [completed] 实现 audit retention 维护工具。
   - Files/modules: audit validator、maintenance CLI、schemas/manifest、双方言测试。
   - Result: canonical archive v1、原子 no-replace `0600` 输出、真正离线 verify、精确 reconcile/restore/delete、默认不删除和 exit 2/3/4 已实现；`app.db`/`app.governance` 的 runtime export 改为 lazy，离线 verify 不构造 engine。
   - Validation: strict JSON/size/path/permission/duplicate-key/hash、冻结 fixture、SQLite/PG round-trip、same-key/different-fact 与 commit outcome 回归已通过。
5. [completed] 完成文档、迁移/importer、运行与安全联动。
   - Files/modules: README、API、Architecture、Security、Testing、Deployment、Operations、Compose/env、prepare/importer。
   - Result: 相关代码/Compose/env 及主要协议、API、安全、架构、部署、运维和状态文档已进入当前 diff；状态、测试、计划和工作日志已同步。
   - Validation: 本轮九份状态文档相对链接检查与 `git diff --check` 通过；最终全仓规则/秘密审计属于步骤 6。
   - Logging: 生产 logger 消息必须为无格式参数的字面量；第三方动态消息统一固定化，Uvicorn raw access log 禁用。
6. [in_progress] 执行完整验证、终审和远程门禁。
   - Files/modules: 全部本切片文件与状态文档。
   - Validation: 定向+全量+lint+smoke+双方言 migration/PG integration+Compose/规则/secret/diff、clean-SHA capacity/acceptance 与实现 SHA CI 4/4 已通过；剩余门禁仅为证据文档提交、push 和该文档 SHA 自身 CI 4/4。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| 高频 scrape 放大数据库压力 | 中/高 | 固定有界窗口、样本上限、查询快照、抓取间隔文档 | fail whole scrape，降低频率并调查查询计划 |
| label/archive 泄密或高基数 | 中/高 | exact allowlist、无对象 ID label、固定错误、严格 archive schema | 停止发布，保留本地证据并修复后重建 archive |
| Worker progress 写放大/幽灵健康 | 中/高 | DB UTC、节流写、process generation、stale/stop 语义 | fail closed 标 stale，不让 dependency probe 覆盖 progress |
| archive 删除竞态/提交结果未知 | 低/高 | snapshot+cutoff+manifest hash 绑定、默认 archive-only、独立 verify/delete | 禁止盲重试，按 count/hash 只读对账 |
| schema/importer 历史兼容漂移 | 中/高 | 0005 migration fingerprint、双方言与 12→13 表 importer 测试 | 保持服务停止，向前修复或用已验证备份 |
| P2-06/P2-07 范围混合 | 中/中 | audit restore 只验证 archive 自身；整库/keyring 留待下一任务 | 更新计划并拆分，不以局部恢复宣称 DR |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| Worker progress/API/exporter | 目标 pytest | DB UTC、stale、低基数、fail-closed 全通过 | 合并定向套件全绿；随后包含于后端 `916 passed, 33 skipped` 全量结果 |
| Audit retention | 目标 pytest + CLI self-check | archive/verify/restore/delete 与失败路径全通过 | 合并定向套件全绿；包括 no-op postverify、FIFO/line-cap 和 PG advisory/row-lock，随后包含于后端全量和 `33 passed, 0 skipped` 真实 integration |
| Migration/importer | SQLite + PostgreSQL integration | 0005/schema fingerprint/import 对账通过 | 临时 SQLite upgrade head/check 与真实 PG 往返/check 全绿；默认用户 SQLite 尚未到 head，直接 `alembic check` 按预期失败且未被擅自迁移；真实 importer 路径包含于 33 个 integration 用例 |
| 真实 PostgreSQL/Redis | `pytest -m integration` | 新增 retention 与既有 lease/governance/importer 全部收集且零 skip | 临时 PG16/Redis7 migration/check 后 `33 passed, 0 skipped`；首次 cleanup 被安全策略拒绝且未启动容器，改用明确目标后通过 |
| Lint/typecheck | `make lint` | Ruff、format check、ESLint、TypeScript 全通过 | 全绿；Ruff 检查 152 files，ESLint 与 TypeScript typecheck 通过 |
| Full test | `make test` | 后端/前端全量通过且只用 Mock/Stub | 后端 `916 passed, 33 skipped`；前端 `38 passed` |
| Offline smoke | `make smoke` | 只用 Mock 的最小垂直链路通过 | `1 passed, 7 deselected` |
| Frontend build | `cd frontend && npm run build` | production build 成功 | 成功，2192 modules；662.39 kB 主 chunk warning 为非阻断既有告警。首次在仓库根误运行 `npm run build` 因无 `package.json` 失败，随后使用正确目录通过 |
| Deployment | `docker compose config --quiet` | exit 0 | exit 0 |
| Alert rules | unit test + `promtool check rules` | 八条规则可被 Prometheus 解析且只引用已交付指标 | 临时 `prom/prometheus:v3.5.0` 验证八条规则全部成功 |
| Dirty Compose acceptance | `make phase2-acceptance` | 9/9、Worker `2/2/2/0/0`、两级 populated refusal/两层空库往返、cleanup C/V/N empty | 通过；artifact `llmbenchlab-p2-11554c25ec2d/evidence.json`，SHA-256 `d5f058457dbc29875cbac4bc38345b810b5ed556ea538862d309116ceb629fde`，`dirty=true` |
| Dirty Compose capacity | `make phase2-capacity` | 最新脚本全通过且 cleanup empty | artifact `llmbenchlab-p2-c6de062ab77e/evidence.json`，SHA-256 `4aeb8271dd81e8671fc287942839f8d06862140ea9a6bf1d7ee5660265aa8453`；18/270/270/271/1229，0 error/drift/duplicate/PEL/lag，Worker expected 2，cleanup C/V/N/image=0；offline Mock、非 SLO |
| Clean-SHA Compose acceptance | `make phase2-acceptance` | exact clean implementation SHA、9/9 与 cleanup C/V/N empty | `9a20676…` 上通过；artifact `llmbenchlab-p2-92e173eeee28/evidence.json`，SHA-256 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`，`dirty=false`；Worker `2/2/2/0/0`、两级 populated refusal、两层空库往返、cleanup C/V/N empty |
| Clean-SHA Compose capacity | `make phase2-capacity` | exact clean implementation SHA、完整对账与 cleanup empty | `9a20676…` 上通过；artifact `llmbenchlab-p2-ca5673061b0f/evidence.json`，SHA-256 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`；1W/2W/burst `7.267474/12.962228/9.333604 q/s`，18/270/270/271/1230，0 question error/drift/duplicate/PEL/lag，cleanup C/V/N/image=0；offline Mock、非 SLO |
| Scripts Ruff | 目标 correctness/import 规则 | P2-06 脚本无 E/F/I 问题 | 过宽默认命令报告 93 条既有 modernization 告警；`--select E,F,I` 通过 |
| Implementation staged 技术/安全终审 | Blocker/High/Medium 终审 + 受影响回归 | 无未解决 B/H/M | structured-extra High 与 Worker `__main__` logger Medium 已修复；76-file implementation index 为 0 Blocker/High/Medium；hydration/import integrity 目标集 `67 passed` |
| Docs/security review | Markdown 链接、secret/diff scan | 无坏链接、秘密或虚假状态 | 状态文档相对链接、whitespace diff、staged secret/path/blob 扫描均通过 |
| Implementation remote gate | push 后 GitHub Actions exact SHA | 四个必需 job 全 success | PR [#3](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/3)；[run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) 对 `9a20676dcf545040782f04c166205d0043345753` 4/4 success |
| Evidence-doc remote gate | 本次文档 commit 的 GitHub Actions exact SHA | 四个必需 job 全 success | 待本次文档提交、push 后执行；完成前 P2-06 保持 `pending_on_docs_ci` |

## Rollback

先停止 API/Worker/exporter scrape 与 retention maintenance。代码可关闭新增 exporter/Worker progress 写入，但保留新 schema 与已有 progress/archive 事实；只有新表为空且无依赖时才允许 0005 downgrade。Audit delete 默认关闭且永不自动运行；已经生成的 archive 先验证/hash 后按敏感文件策略保留。若 delete commit outcome unknown，禁止重跑，先在只读事务对账 event key/count/hash。整库恢复与 keyring 不在本切片回滚声明内。

## Documentation updates

- [x] README / 用户操作说明已同步当前实现与 pending-gate 状态
- [x] API / Schema 已进入当前 P2-06 diff
- [x] Architecture / Security / ADR-0015 已进入当前 P2-06 diff
- [x] Testing / Deployment / Operations / alert Runbook 已进入当前 P2-06 diff
- [x] CHANGELOG、PROJECT_STATUS、Roadmap、Phase 2、NEXT_TASK、工作日志已同步；最终门禁结果仍须在步骤 6 回填

## Completion evidence

- Changed files: `0005`/models/importer/prepare、Worker service/runner/progress/probe、JSON/Prometheus API 与 collector/renderer、audit validator/archive/retention CLI、logging/config/Compose、规则与相关测试/文档。
- Commands run: `make lint` 全绿（Ruff 152 files、ESLint、TypeScript typecheck）；`make test` 后端 `916 passed, 33 skipped`、前端 `38 passed`；`make smoke` 为 `1 passed, 7 deselected`；临时 PostgreSQL 16/Redis 7 migration/check 与 integration `33 passed, 0 skipped`；临时 SQLite head→0001→head/check、正确目录 frontend build、`docker compose config --quiet`、Prometheus `v3.5.0` 八规则校验、clean-SHA capacity/9/9 acceptance 成功。
- Acceptance evidence: implementation commit `9a20676dcf545040782f04c166205d0043345753` 的 clean acceptance artifact `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-92e173eeee28/evidence.json`，SHA-256 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`；clean capacity artifact `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-ca5673061b0f/evidence.json`，SHA-256 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`。两者 `dirty=false`、status passed；acceptance cleanup C/V/N empty，capacity cleanup C/V/N/image empty。Capacity 是 offline Mock 非 SLO。
- Remote evidence: implementation commit 已 push 到 `origin/codex/complete-evaluation-workflow`；PR [#3](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/3) 的 [run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) 对该精确 SHA 4/4 success。
- Not run: 本次证据文档提交尚未形成，因此尚无该文档精确 SHA 的 CI；默认用户 SQLite 未迁移到 head，按保护原则保留原状。
- Known issues: P2-07 仍为下一独立切片；Phase 2 保持 `in_progress`。

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-28 | discovery | 现有 JSON metrics/history 已有 DB snapshot 与 audit fail-closed，可作为 exporter 数据源；仓库没有 exporter/alert sender。 | 复用现有事实，不建立第二套状态。 |
| 2026-08-28 | discovery | Worker probe 只验证 DB/head/queue capability，不能证明主循环 scan/claim/progress。 | 新增 DB-time process progress，并保持 probe 语义分离。 |
| 2026-08-28 | discovery | `audit_events` 有 minimum expiry 与索引，但没有可执行 archive/verify/delete/restore。 | ADR-0015 必须冻结显式维护协议。 |
| 2026-08-28 | decision | ADR-0015 冻结固定 15 分钟/50,000 行 exporter、DB-time coalesced Worker progress、八条规则及 canonical JSONL retention 流程。 | 三条实现线可在不改变事实来源和 P2-07 边界的前提下并行。 |
| 2026-08-28 | implementation | `0005` 增加 Worker progress 与两个 audit 扫描索引；importer 从 12 表扩展到 13 表并拒绝 live generation。 | 当前 Alembic/archive compatible head 为 `20260828_0005`；历史 `0004` 证据保留不改写。 |
| 2026-08-28 | implementation | exporter 采用由 collection owner 持有的进程内 gate；request 重复取消不会在线程 DB query 仍运行时释放 single-flight。 | 避免取消竞态导致第二次 scrape 与未结束 query 重叠。 |
| 2026-08-28 | implementation | `app.db` 与 `app.governance` runtime exports 改为 lazy，CLI `verify` fresh process 不再读取 DB 配置或创建 engine/目录。 | 保持 archive verification 真正离线，同时保留既有 public imports。 |
| 2026-08-28 | review | importer committed-target integrity、retention no-op postverify、外部 logger extra、FIFO/line cap 与 PostgreSQL advisory/row-lock 门禁经终审补强并进入回归。 | 相关目标测试、全量与真实 integration 已通过。 |
| 2026-08-28 | gate | Prometheus v3.5.0 八规则、dirty capacity 与 9/9 acceptance 通过；structured-extra High 与 Worker `__main__` logger Medium 修复后，最新 staged 技术/安全终审为 0 Blocker/High/Medium。 | acceptance `11554c25ec2d` / `d5f058…9fde`；capacity `c6de062ab77e` / `4aeb827…8453`；clean-SHA 仍须重跑。 |
| 2026-08-28 | gate | clean implementation commit `9a20676…` 的 capacity/9/9 acceptance 与远程 run `33164609388` 4/4 通过。 | clean artifacts 为 acceptance `92e173eeee28` / `e4ffb866…0fc47`、capacity `ca5673061b0f` / `2382f913…f5039`；不复用 dirty evidence冒充 clean gate。 |
| 2026-08-28 | status | P2-06 实现与 clean-SHA/implementation-CI 门禁已完成；证据文档提交及其自身 exact-SHA CI 尚未完成。 | P2-06=`pending_on_docs_ci`、Phase 2=`in_progress`、P2-07=`not_started`；先完成步骤 6 再启动 P2-07。 |
