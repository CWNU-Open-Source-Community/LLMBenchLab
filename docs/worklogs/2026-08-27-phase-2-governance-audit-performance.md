# 2026-08-27 — Phase 2 并发治理、审计与性能基线工作日志

> 本日志在 2026-08-28 收尾。只记录实际发生的事实；历史失败、中间证据与最终精确候选结果分别标识。

## 元信息

- 日期：2026-08-27 至 2026-08-28
- 执行者：Codex
- 分支：`codex/complete-evaluation-workflow`
- 关联阶段：[Phase 2 — 可靠性与任务执行](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[Phase 2 并发治理、审计与性能基线执行计划](../plans/2026-08-27-phase-2-governance-audit-performance.md)
- 关联决定：[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](../decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0011](../decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)
- 当前状态：`completed`（本治理/审计候选切片；Phase 2 整体仍为 `in_progress`）

## 目标与背景

Phase 2 已有 PostgreSQL/Redis、独立 Worker、租约/fencing、幂等 Response、重试/取消/dead-letter 和真实故障基础。本任务收敛 P2-05/P2-06/P2-07 的治理、审计与容量切片：数据库权威的四层额度、逐 Provider attempt ledger、确定背压、有限公平、typed history/audit、非秘密 Provider/credential evidence，以及真实 PostgreSQL/Redis 的可复现容量与故障工具。它不扩展 Benchmark、Judge、Arena、Agent 或公共部署，也不承诺 Provider exactly-once、生产 HA 或 SLA。

用户先要求提交已有功能 PR。PR [#1](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/1) 的前置精确 SHA `ab15862eab4870dda01fb079b44b509a7d737627` 对应 run `33078921254`，四个必需 job 全部成功。随后 ADR 设计提交 `8df122b` 的 CI 因 Runs-page timer 竞态 3/4；修复 SHA `1cd19c51ed309316047a18ed3b2a308647af495d` 对应 run `33081854406` 为 4/4。治理实现及两次 admission 锁修复最终收敛到 SHA `665244e095905083b606b8e98e946ed1a02dc0fc`；该 SHA 的 run `33099260233` 四个必需 job 全部成功。

## 初始仓库状态与保护边界

- 本任务初始 HEAD 为 `ab15862eab4870dda01fb079b44b509a7d737627`，工作树当时干净并跟踪同名 origin 分支。
- `.env`、`.secrets/`、本地 SQLite、keyring 和 artifacts 均不得提交；迁移、integration 与容量测试只允许隔离临时数据库/Compose project。
- 自动化模型路径只允许 Mock、MockTransport、stub 和故障注入，不读取或调用真实 Provider Key。
- SQLite 继续只支持单 Worker；多 Worker 正确性和容量结论只来自真实 PostgreSQL。

## 范围与验收边界

- global/provider/model/run 四层 concurrency、固定 UTC 分钟 RPM/TPM、global/run lifetime request/Token/USD budget。
- managed API Run 冻结 active policy ID/hash 和显式输入/Token/费用 override；可信本地 CLI 按 ADR-0010 保持 `legacy_unmanaged`。
- Adapter 内每个实际 HTTP retry attempt 使用唯一 reservation；`reserved`、`send_started`、actual/conservative settlement 与 confirmed pre-send release 均有 never-delete 证据。
- 四层 materialized scope/minute counter 只能是 ledger 投影；高报或低报，以及冻结 policy/override 漂移，都必须在 admission/mutation/reconcile/import 前 fail closed。
- backlog typed `429`、database not-before、question quantum、dispatch/failure 分离和跨 Model 有限公平。
- append-only 应用 audit、Run audit、task history/latency、Provider request/model/fingerprint/finish metadata、credential 固定非秘密事件和 UI 治理状态。
- 增强 capacity/acceptance 与真实 PostgreSQL 竞争测试；最终证据必须来自冻结候选 SHA。

Phase 2 仍不满足的正式 closure 包括：SLO/容量模型、Exporter/告警、审计 retention archive、备份恢复认证、Worker progress/liveness 与剩余生产式故障矩阵。三条 Provider/本地 commit 确定性 seam 已通过完整 Compose acceptance；它们仍不是 Provider exactly-once 或亚毫秒 `SIGKILL` 证明。治理切片门禁绿色后，Phase 2 继续保持 `in_progress`。

## 实际修改

### 数据库与 importer

- 新增 Alembic `20260827_0004` 和六类表：`governance_policies`、`governance_scopes`、`governance_minute_buckets`、`question_executions`、`provider_call_reservations`、`audit_events`。
- `evaluation_runs` 增加 dispatch/failure、policy/governance、override 与 not-before 字段；`evaluation_responses` 增加 Provider request ID、returned model、system fingerprint、finish reason 和 HTTP attempt 数。
- active policy 由 SQLite/PostgreSQL partial unique index 强制唯一；policy 以 canonical hash 固定，managed Run 创建时冻结 policy 和 override。
- SQLite→PostgreSQL importer 从六表扩为 12 表，按依赖顺序执行停写源/空目标/单事务 copy 与 count/PK/content fingerprint；治理 active/reserved 状态和 materialized/ledger 漂移在 preflight fail closed。
- 所有新增持久时间和窗口使用数据库 UTC 时钟；Run started/finished 索引支持历史延迟查询。

### 治理 repository 与执行链路

- 实现 canonical provider origin、四层锁序和 DB-authoritative scope/bucket materialization；并发、RPM/TPM、request/Token/USD lifetime budget 在同一事务 reserve。
- reserve、mark-send-started、finish/release、lease reconcile、renew 等关键路径先从 never-delete ledger 重算并核对 materialized counters；任一高/低漂移抛出稳定 `GovernanceIntegrityError`，API/Worker 独立短事务仅记录固定完整性事件。
- OpenAI-compatible Adapter 的每个内部 retry 和 Mock Adapter 都走 per-attempt hook；未知 usage、失租或 send-started 后的不确定结果按完整预留 conservative settlement，不能按零释放。
- 终审发现 confirmed pre-send release 先递增 ordinal，可能在零 HTTP 时耗尽 `max_retries=0`。ADR-0011 已接受：旧 release row 保持终态，新的 ledger generation 从当前未发送 ordinal 恢复；lease reconciler 不重复推进 generation。
- Runner 的同步数据库操作移出事件循环；question quantum 与 cooperative yield 不增加 `failed_attempt_count`，dispatch/claim 单独计数，due work 以可恢复字段排序。

### API、审计、安全与前端

- 新增 active policy GET/PUT；PUT 必须提交完整有界 policy 文档。Run admission 在 backlog 满时返回 typed `429`，数据库 truth 不依赖 Redis。
- 新增 Run governance/status/reason/not-before/policy/override 字段、分页 Run audit、task history counters 和基于 Run 数据库时间戳的 queue/execution/end-to-end p50/p95/p99。
- audit event key 唯一且 payload/identity 使用固定 allowlist；应用 audit 可由数据库管理员修改，不冒充 WORM。读取时保留事件不满足 schema/hash 会 fail closed。
- credential create/replace/source-switch/rejection/decrypt-failure 只记录固定 action/reason/source、Model ID 和允许的 key ID，不保存 origin、Key、ciphertext、nonce、Authorization 或 keyring。
- Provider request ID、returned model、system fingerprint、finish reason 仅在字符、长度和凭据形态校验后持久化；不安全值按 ADR-0010 归一化为 `null`，不写高基数 redaction event。
- Run Detail 展示 managed/delayed/exhausted 状态、治理原因和明确 UTC 的 not-before；治理延迟与排队/执行状态文案保持可区分。

### 容量、真实 PostgreSQL 测试与文档

- `phase2_capacity.py` 已增强为显式有限 policy，输入/output/Token/cost reservation 有界，question quantum 小于 15 题 Demo Run；加入并发 backlog 精确 `202/429`、跨高/低流量 Model 公平、双 Worker、Worker/Redis 故障及 ledger/audit 对账证据。
- 真实 PostgreSQL integration 测试已增加四层 concurrency、RPM/TPM、global/run lifetime budget、并发 backlog、settlement/reconcile race 和 audit replay；本地真实 PostgreSQL/Redis 运行 `29/29 passed`，同一实现 SHA 的远程 integration job 亦成功。
- acceptance harness 已加入 `reserved`、`send_started`、`response_committed` 三条 deterministic database seam injection，分别断言 pre-send release/ordinal、conservative settlement 和本地 Response/ledger/audit exactly-once。场景明确不声称 `SIGKILL` 精确命中亚毫秒缝隙。
- README、API、Architecture、Security、Testing、Deployment、Operations、Performance、Roadmap/Phase/Status/NEXT_TASK/Changelog/计划已按实现边界、精确 SHA、evidence hash 与 CI 结果同步。

## 决定、偏差与发现

| 日期 | 类型 | 事实与影响 |
| --- | --- | --- |
| 2026-08-27 | discovery | retry 位于 Adapter 内部；只包裹 `generate()` 会漏算 HTTP attempt，因此采用 per-attempt hook。 |
| 2026-08-27 | discovery | Worker 原先整 Run 独占；仅调整 claim 顺序不能防长 Run 饥饿，因此增加有限 question quantum。 |
| 2026-08-27 | decision | `reserved` 未发送可释放；`send_started` 后异常或失租按完整预留 conservative settlement。 |
| 2026-08-27 | decision | 旧 Run 不回填虚构 ledger/audit；缺治理快照者保持 `legacy_unmanaged`。 |
| 2026-08-28 | ADR-0010 | CLI 不伪造 synthetic ledger；metadata unsafe→`null`；Run latency 来自 DB Run timestamps；credential audit 不保存 origin。 |
| 2026-08-28 | ADR-0011 | confirmed pre-send release 不消耗 Provider retry；用新 ledger generation 保留当前未发送 ordinal。 |
| 2026-08-28 | review fix | materialized counter 低报可绕过限额；关键路径和 importer 改为 ledger 重算 fail closed。 |
| 2026-08-28 | capacity failure | 首轮精确 SHA capacity 在并发拒绝路径超时，定位为 HTTPException 前事务未 rollback、admission 锁占用到 session 结束；SHA `ecf93f7…` 显式 rollback 后修复。 |
| 2026-08-28 | capacity failure | 第二轮在 PostgreSQL 出现 Run 创建 `Model→global` 与 attempt `global→…→Model` 反序死锁；SHA `665244e…` 先锁 global governance scope，再锁 Model，恢复 canonical 顺序。 |
| 2026-08-28 | scope boundary | 三条 crash seam 已通过完整 acceptance；正式 SLO/Exporter/告警/retention archive/backup restore/Worker progress 留在 Phase 2 closure，不能据当前切片宣称 Phase 2 完成。 |

## 实际命令与结果

以下结果均为实际运行；表中保留了早期中间证据，而标为“最新本地共享树”或“最新候选树”的命令发生在最后一轮完整性修复之后。

| 命令/检查 | 实际结果 |
| --- | --- |
| `git status --short --branch`（初始） | 工作树干净，分支与 origin 同步 |
| `gh pr create ...` / PR #1 | 创建成功；首次正文被 shell 反引号解释后立即修正为安全正文 |
| 前置 SHA `ab15862…` Actions run `33078921254` | 4/4 必需 job 通过 |
| ADR 设计 SHA `8df122b` | 3/4；前端 35/36，定位为 timer 断言竞态 |
| `npm test -- --run tests/runs-page.test.tsx`（timer 修复后） | 1 file / 6 tests 通过 |
| `npm test`、`npm run lint`、`npm run typecheck`、`npm run build`（timer 修复后） | 9 files / 36 tests 与静态/build 通过；仅既有大 chunk warning |
| 修复 SHA `1cd19c51ed309316047a18ed3b2a308647af495d`，run `33081854406` | 4/4 必需 job 通过；`8df122b` 的失败记录保留 |
| `make lint`（最新候选树） | Ruff/format、ESLint、TypeScript 通过 |
| `make test`（文档收尾前重跑） | 后端 `604 passed, 29 skipped`；前端 `38 passed` |
| 真实 PostgreSQL/Redis integration（最新本地共享树） | `29/29 passed` |
| `backend/tests/test_phase2_acceptance_script.py` 等 seam 定向门禁 | `19 passed`；Ruff、格式检查、`py_compile`、acceptance self-check 通过 |
| `make smoke`（最新候选树） | `1 passed, 7 deselected`，仅 Mock；第一次因 sandbox uv cache 权限失败，获授权后重跑通过 |
| 治理/Adapter/Runner/audit/migration 定向套件 | 一次目标集合零失败；独立测试审计记录 `218 passed` |
| integrity boundary + governance API + Worker/process boundaries | `18 passed` |
| 隔离 SQLite Alembic | `upgrade head -> downgrade 0001 -> upgrade head -> alembic check` 全通过 |
| `docker compose config --quiet`（最新候选树） | exit 0 |
| `python3 scripts/phase2_capacity.py --self-check-only`（最新候选树） | self-check 通过 |
| `npm run build`（最新候选树） | production build 通过；仅既有大 chunk warning |
| `make phase2-capacity`（精确 `665244e…`） | passed；1W `7.306981`、2W `13.396740`、burst `8.585309` q/s；4×202+2×429、yield/fairness/fault/reconciliation 通过；evidence SHA-256 `40deadeb…0588`；cleanup 为空 |
| `make phase2-acceptance`（精确 `665244e…`） | 9/9 passed；含三条 deterministic DB seam、SIGKILL/Redis/cancel/migration；evidence SHA-256 `ab311665…ddec`；cleanup 为空 |
| 精确 SHA `665244e…` Actions run `33099260233` | Backend lint/test、PostgreSQL/Redis integration、Frontend lint/test/build、Real Compose acceptance 4/4 成功 |
| staged diff / secret review | 两路独立审查 Blocker 0 / High 0；命中均为测试 canary；无 `.env`、Key、私钥、数据库、日志、artifact 或构建产物进入提交 |

## 精确候选证据

- 实现提交：`f5876919936c246029f1cc669831fddeabca6a81`；rejected admission rollback 修复：`ecf93f73eb6bc31ae41339ac42f92b6075e34443`；canonical lock-order 修复：`665244e095905083b606b8e98e946ed1a02dc0fc`。均普通 push 到 `origin/codex/complete-evaluation-workflow`，未 force push，PR #1 保持 open。
- Capacity：`.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-51cfadee04f5/evidence.json`，SHA-256 `40deadebc357bbb24a07c91b05eb39f3d2fb7de11a28da9a7f95871c7acd0588`；记录 `dirty=false`、18 Runs/270 Responses/271 reservations/1229 audit，active/reserved/overdrawn 与 queue PEL/lag 均归零。
- Acceptance：`.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-afe52c2d54cb/evidence.json`，SHA-256 `ab311665ff0cb834efdd648cd634f943a4cbc5b8b00728ac8597a288a877ddec`；9/9 passed，三条 deterministic DB seam、真实 lease-owner SIGKILL、Redis stop/start、取消/重复投递与 populated downgrade refusal/空库往返均通过。
- 两份 evidence 均为 Mock-only/offline、`dirty=false`，秘密扫描无命中，Compose cleanup 的 remaining containers/volumes/networks 均为空。它们不是 Provider 性能、费用、生产 SLO/SLA 或 exactly-once 证明。

## 已知问题与下一步

1. 按 [NEXT_TASK.md](../NEXT_TASK.md) 新建独立工作日志与计划，定义受支持拓扑的正式 SLO/容量模型和多轮统计方法。
2. 交付受控 Exporter/告警、Worker DB-time progress/liveness 与 audit retention archive/restore，保持低基数和固定非秘密 schema。
3. 演练 PostgreSQL 与数据库外 keyring 配对 backup/restore、Redis 重建和剩余失败矩阵；每个独立切片继续 commit/push 并等待精确 SHA CI。
4. Phase 2 在上述正式 closure 未完成前继续 `in_progress`；不得把本次 Mock 候选证据外推为生产 HA/SLA 或 Provider exactly-once。
