# 项目状态

> 更新时间：2026-08-30（Asia/Shanghai）

## 当前阶段

- Phase 0 — 项目治理和架构：`completed`（2026-08-24）
- Phase 1 — MVP 垂直链路：`completed`（2026-08-25）
- Phase 2 — 可靠性与任务执行：`in_progress`（可靠基础、治理/审计、P2-01 单机资格与 P2-06 已完整交付；P2-07 工作包已建立，状态为 `planned`，功能尚未实现）
- Phase 3 — 标准 Benchmark 与代码评测：`in_progress`（仅可信本地 MMLU-Pro/GPQA-Diamond 客观题提前切片）
- Phase 4–6：`planned`

## 当前版本与远程边界

`0.1.0` development baseline，REST API 为 `/api/v1`，评测协议为 `llmbenchlab-protocol-v1`；尚未发布正式 Release。

公开仓库：[`CWNU-Open-Source-Community/LLMBenchLab`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab)，当前开发分支为 `codex/complete-evaluation-workflow`。P2-06 实现 SHA [`9a20676dcf545040782f04c166205d0043345753`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/9a20676dcf545040782f04c166205d0043345753) 已普通 push 并进入 [PR #3](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/3)，其精确 SHA 的 GitHub Actions [run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) 四个必需 job 全部成功；绑定该 clean SHA 的 capacity 与 9/9 acceptance 也已通过。Evidence closeout 文档 commit [`ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6) 已 push，其精确 SHA 的 GitHub Actions [run `33165775037`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33165775037) 四个必需 job 全部成功，因此 P2-06 已完成仓库级收尾并标记为 `completed`。[ADR-0017](decisions/ADR-0017-schema-equivalent-governance-index-repair.md) / `20260829_0006` 数据库兼容修复实现 SHA [`8fb51b690ae6335b8ef93b3cbe54e039781fb173`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/8fb51b690ae6335b8ef93b3cbe54e039781fb173) 已普通 push，其精确 SHA 的 GitHub Actions [run `33263405214`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33263405214) 四个必需 job 全部成功，因此该维护任务为 `completed`。Phase 2 仍为 `in_progress`；P2-07 已建立 ADR-0016、独立计划和工作日志，状态为 `planned`，功能实现尚未开始。历史 P2-01 位于 [PR #2](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/2)：实现 SHA `b6a35fef1dd069ebb54b69955058915c722aa34d` 的 [run `33146681285`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 4/4 成功，证据文档 commit `875f13a253c40b7573d45c6287385e60f2bb8f04` 的 [run `33150080341`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33150080341) 也已 4/4 成功。

## 已交付基线

- Phase 0/1 的治理、架构、协议、数据格式、ADR、FastAPI/SQLAlchemy/Alembic、React/TypeScript、Mock 垂直链路、三类 Evaluator、Demo 数据、API/UI、离线测试和开源流程。
- PostgreSQL/Redis 可靠执行基础：数据库事实来源、Redis at-least-once 通知、独立 Worker、DB scan、租约/heartbeat/fencing、逐题幂等、有限 retry/backoff、取消、租约接管、dead-letter 和终态 Response 重算。
- OpenAI-compatible SSE、严格 `[DONE]`、JSON fallback、identity-only、wire/event/content/error 上限、idle read timeout、bounded error 与精确当前-Key 脱敏。
- Web write-only `api_key`、AES-256-GCM `model_credentials`、数据库外 API/Worker 共享 keyring、legacy `api_key_env`、origin/active-Run 门禁和 fail-closed repair/remove 路径。
- MMLU-Pro test 与 GPQA-Diamond 固定 revision/SHA 转换、可信本地 `llmbenchlab-evaluate prepare/run/resume/report`、请求上界确认和原子终态报告。该 CLI 仍要求独占数据库，未受 Phase 2 managed budget 保护。
- React 中文界面覆盖 Dashboard、Models、Benchmarks、Evaluation Runs、New Run、Run Detail、Leaderboard；Run 列表全状态筛选/分页/活动轮询，详情逐题分页，关键桌面/平板/移动布局已修复。

## 已通过候选门禁的 Phase 2 切片

- Alembic 链已扩展到 `20260827_0004`。新增六类治理/审计表：`governance_policies`、`governance_scopes`、`governance_minute_buckets`、`question_executions`、`provider_call_reservations`、`audit_events`；加上既有业务/凭据表，SQLite→PostgreSQL importer 现按依赖顺序复制和对账全部 12 表。
- active policy 在 SQLite/PostgreSQL 都由 partial unique index 保证唯一；policy 有 canonical hash。managed API Run 创建时冻结 policy ID/hash 与 input reservation、lifetime request/Token/USD overrides，旧 Run 和可信本地 CLI 保持 `legacy_unmanaged`。
- global/provider/model/run 四层数据库权威治理已实现：concurrency、固定 UTC 分钟 RPM/TPM、global/run lifetime request/Token/cost；Redis 和进程内存不参与裁决。
- Adapter 的每个实际 HTTP retry attempt 都进入 reserve→send-started→actual/conservative settlement 或 confirmed pre-send release ledger。未知 usage、失租或 commit 不确定不按零释放。
- materialized scope/minute counter 只是 ledger 投影；高报/低报、policy/hash 或 Run override 漂移在 admission/mutation/reconcile/import 边界 fail closed，并只尝试记录固定非秘密完整性事件。
- [ADR-0011](decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md) 已修复零 HTTP 的 pre-send release 消耗 retry：旧 ledger row 保持终态，下一 generation 从未发送 ordinal 恢复，包括 `max_retries=0`。
- backlog local admission、typed `429`、database not-before、有限 question quantum、dispatch/failure 分离和跨 Model due ordering已接入；Run 不因 Redis 故障丢失。
- typed append-only 应用 audit、分页 Run audit、task history counters、数据库 Run 时间戳 queue/execution/end-to-end latency、严格规范化 Provider request/model/fingerprint/finish metadata 和固定非秘密 credential audit 已实现。
- 前端 Run Detail 已显示 managed/delayed/exhausted、治理原因和明确 UTC not-before；它不把治理延迟冒充 Worker 正在执行。
- enhanced capacity 脚本已加入有限 policy、显式 Token/费用边界、sub-15 question quantum、并发 backlog `202/429`、跨 Model 公平、双 Worker、Worker/Redis fault 与 ledger/audit 对账；真实 PostgreSQL 测试代码已加入四层 RPM/TPM/lifetime budget、backlog、settlement/reconcile race 和 audit replay。
- acceptance harness 已加入三条确定性数据库 seam injection：`reserved`→send-start、`send_started`→settlement、Response commit→最终恢复。它们明确不是“精确时刻 SIGKILL”声明；精确候选 SHA 的完整 Compose acceptance 已 9/9 通过。
- 精确 SHA `665244e…` 的增强 capacity 使用有限 policy、PostgreSQL 16、Redis 7 与两个 Worker 完成：并发 backlog 精确为 4 个 `202` + 2 个 typed `429`，cooperative yield 与跨 Model 公平顺序均有 durable audit 证据，最终 18 Runs/270 Responses/271 ledger/1229 audit 对账且无 active/reserved/overdrawn 漂移。
- P2-01 的 `P2-local-control-plane-v2` 已在干净 SHA `b6a35fe…` 从零执行 1 次 warm-up + 恰好 5 次 measured trial，本次 invocation 的 `discarded_trials=0`；四个 cell、23/23 SLO 与每轮 hard invariant 全部通过，容量模型为 `qualified`。aggregate SHA-256 为 `a76d167b…d0d9`，六轮均精确完成 22 Runs/330 Responses/330 QuestionExecutions/331 reservations，并清理本项目容器、卷、网络和唯一 build image。历史 v1 aggregate `f993c11f…e3b2` 继续保持 `failed/not_qualified`。

## P2-06 实现（`completed`）

- [ADR-0015](decisions/ADR-0015-observability-worker-progress-audit-retention.md) 已接受；实现 SHA `9a20676dcf545040782f04c166205d0043345753` 将 Alembic head 扩展到 `20260828_0005`。`worker_processes` 保存 generation 级 DB UTC `started/seen/scan/claim/progress/lease-heartbeat/stop`，主循环只在真实事件后合并刷新；JSON metrics 公开 expected/registered/live/stalled/shortfall 与最近时间，不公开 Worker/generation ID。dependency probe 固定声明 `main_loop_progress=not_checked`。
- `GET /api/v1/metrics/prometheus` 已实现固定 Prometheus text `0.0.4` gauge：一个 DB-time 读快照、15 分钟 typed-audit 窗口、1 小时 Run latency、硬读取上限、固定 enum label、整次 fail-closed 与每 API 进程 single-flight。`deploy/observability/` 提供固定八条规则和安全抓取示例；仓库不部署 Prometheus、Alertmanager 或通知发送器。
- `llmbenchlab-audit-retention archive|verify|reconcile|restore|delete` 已实现 canonical JSONL v1、严格权限/大小/行/schema/hash/rollup 校验、离线 verify、精确 digest 绑定、默认不删除、双方言事务与 commit outcome 分类。Archive 是敏感运维文件，hash 只用于完整性/绑定，不是签名或 WORM，也不替代 P2-07 的数据库+keyring 备份。
- P2-06 的 `0005` 将 importer 逻辑合同扩展为 13 表精确 count/PK/content digest；当前 source/target 仍必须位于唯一 current head `0006`，13 表语义不变。live generation 在源 preflight 被拒绝，stopped/stale facts 可复制，终审又补强 committed target canonical integrity postverify。`0005 -> 0004` 在 `worker_processes` 非空时于 DDL 前拒绝，原有 `0004` governance/audit downgrade guard 继续保留。
- 生产日志源已统一治理：应用日志消息必须是无格式参数字面量，结构化字段按白名单和有限数值输出，第三方动态消息固定化且不能通过 allowlisted extra 注入，raw Uvicorn access handler 关闭。Archive 终审补充了 FIFO/非普通文件拒绝及 decode 前行数上限；retention 零行 mutation 仍须 postverify，PostgreSQL mutation 保持 advisory/row lock。
- 上述实现的全部实现门禁已完成：合并定向套件、`make lint`（Ruff 152 files、ESLint、TypeScript）、`make test`（后端 `916 passed, 33 skipped`、前端 `38 passed`）、Mock smoke（`1 passed, 7 deselected`）、临时 PostgreSQL 16/Redis 7 migration/check 与真实 integration（`33 passed, 0 skipped`）、隔离 SQLite migration/check、frontend build、Compose config、八规则 `promtool` 和修复后 76-file staged 技术/安全终审均通过；实现 SHA 已 push，精确 SHA run `33164609388` 4/4 成功。Clean acceptance `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-92e173eeee28/evidence.json` 的 SHA-256 为 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`，9/9 通过，Worker expected/registered/live/stalled/shortfall=`2/2/2/0/0`，cleanup C/V/N 全空。Clean capacity `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-ca5673061b0f/evidence.json` 的 SHA-256 为 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`；1W/2W/burst QPS=`7.267474/12.962228/9.333604`、wall=`8.255963/4.628834/6.428385s`，最终 18 Runs/270 Responses/270 question executions/271 reservations/1230 audit，0 question error/drift/duplicate/PEL/lag，Worker expected=2、shortfall=0，cleanup C/V/N/image 全零且 image counters=`1/1/0/0`。两份 evidence 均为 `dirty=false` 并绑定 `9a20676…`；这是 Mock-only、非 SLO。此前 dirty acceptance/capacity 继续作为历史证据保留。Evidence closeout 文档 commit `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6` 已 push，精确 SHA run `33165775037` 4/4 成功，P2-06 仓库级收尾完成。P2-06 当时默认用户 SQLite 尚未在 head，直接 `alembic check` 失败后按保护原则未擅自迁移。

## 2026-08-29 数据库兼容修复（`completed`）

- 旧库失败不是 SQLite 损坏，而是 revision=`20260827_0004` 的早期结构变体精确缺少三个后来加入 canonical `0004` 的索引；严格 preflight 因 revision/schema 不一致，在 `0005` 执行前按设计拒绝。
- [ADR-0017](decisions/ADR-0017-schema-equivalent-governance-index-repair.md) 已接受；当前 head 为 schema-equivalent `20260829_0006`。preflight 只接受 canonical `0004/0005`，或仅缺一至三个已知 repair 索引的 fingerprint，以支持 SQLite repair 中断重入；PostgreSQL `0005` 的额外 metadata drift 同样拒绝。完整 schema/integrity/FK、索引定义、single-active policy 数据门禁仍 fail closed。`0006` 首条 DDL 前再次拒绝多条 active policy，并条件补建索引；`0006 -> 0005` 保留 canonical 对象。
- 真实失败备份副本已无损升级到 `0006` 并通过 integrity/FK/Alembic check；当前重建库由标准 preflight 自动备份后从 canonical `0005` 到 `0006`，startup gate、quick check、FK 和 metadata check 全部通过，业务计数与 Worker facts 保持。
- 本地 migration `52 passed`；最终完整 `make test` 为后端 `927 passed, 33 skipped`、前端 `38 passed`，`make lint`、Mock smoke 和 Compose config 均通过。实现 SHA `8fb51b690ae6335b8ef93b3cbe54e039781fb173` 已 push，[run `33263405214`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33263405214) 的 backend、真实 PostgreSQL/Redis、完整 Compose 与 frontend 四个 job 全部成功；本维护任务完成，不改变 Phase 2/P2-07 状态。

## 状态与后续

- P2-06：状态为 `completed`；实现、clean-SHA Compose evidence、实现 commit 与 evidence closeout 文档 commit 的 push 和精确 SHA CI 均已完成。
- 0004 历史索引兼容修复：状态为 `completed`；实现 commit `8fb51b690ae6335b8ef93b3cbe54e039781fb173` 已 push，精确 SHA run `33263405214` 4/4 成功。
- P2-07：状态为 `planned`，已建立 [ADR-0016](decisions/ADR-0016-postgresql-keyring-recovery-and-redis-rebuild.md)、其 exact-head amendment [ADR-0017](decisions/ADR-0017-schema-equivalent-governance-index-repair.md)、[独立计划](plans/2026-08-28-phase-2-recovery-operations.md) 和 [工作日志](worklogs/2026-08-28-phase-2-recovery-operations.md)。PostgreSQL backup/restore、数据库与 keyring 配对恢复、Redis 重建、Worker 扩缩/告警处置和剩余故障矩阵的功能实现尚未开始；P2-06 的 audit archive 自身 restore 不能替代整库恢复认证。
- Phase 3：IFEval、通用 Dataset Plugin SDK、代码题 schema/隔离沙箱、完整分组 UI 和安全红队；Phase 4–6 尚未开始。

## 已知边界与风险

- SQLite 只用于个人本地单 Worker；多 Worker 证据必须来自 PostgreSQL。Compose 是本地开发/验收拓扑，不是生产 HA。
- Provider 调用不是 exactly-once。Worker 在 Provider response 后本地 commit 前崩溃可能重复上游计算或费用；本地 ledger/Response 幂等只能保证数据库事实不 double-count。
- fixed UTC minute window 允许边界 burst，不等同平滑 token bucket。Mock capacity 不能推断真实 Provider、生产 SLA 或无限横向扩展。
- trusted-local CLI 按 [ADR-0010](decisions/ADR-0010-phase-2-governance-delivery-boundaries.md) 继续 `legacy_unmanaged`，没有全局 RPM/TPM/USD 硬保证；操作者必须停止常规 API/Worker 并独占数据库。
- audit 是应用 append-only、event-key 幂等且 read 时校验 schema/hash，但数据库管理员仍可修改，不能宣称 WORM。
- Provider metadata 不安全时归一化为 `null`；credential audit 不保存 origin。Key、Authorization、ciphertext、nonce、keyring、Provider URL、题目/prompt/response正文均不得进入 audit。
- Worker probe 只检查数据库/head/Redis 能力，不证明主循环仍在推进；Worker 主循环事实现在由 DB-time progress 聚合公开。没有 exact generation handoff 前，probe/容器 healthcheck 仍不得冒充当前进程 event-loop liveness。
- importer 会复制完整敏感评测内容和 credential ciphertext；只支持停写源→空目标单向导入。keyring 不随数据库复制，exit 3/4 禁止盲目重试。
- 远程 Provider 只允许 HTTPS（HTTP 仅 loopback），但仍无 destination allowlist、DNS rebinding 防护、出站隔离、认证、TLS 终止、生产 KMS 或多租户安全；不得直接暴露公网。
- 当前 Python 3.14 本地测试仍可能显示上游弃用 warning；CI 固定 Python 3.12。Vite build 仍有既有 Recharts 大 chunk warning。

## 测试状态

| 验证 | 实际结果 | 当前结论 |
| --- | --- | --- |
| P2-06 合并定向套件 | 全绿；随后完整 `make test` 也已通过 | 目标实现回归与全量门禁均通过 |
| P2-06 `make lint` | Ruff 152 files、format check、ESLint、TypeScript typecheck 全绿 | 实现 SHA `9a20676…` 的本地门禁通过 |
| P2-06 `make test` | 后端 `916 passed, 33 skipped`；前端 `38 passed` | 实现 SHA `9a20676…` 的本地门禁通过；只用 Mock/Stub |
| P2-06 Mock smoke | `1 passed, 7 deselected` | 完全离线通过 |
| P2-06 真实 PostgreSQL/Redis integration | 临时 PostgreSQL 16/Redis 7 migration/check 后 `33 passed, 0 skipped` | retention advisory/row-lock 与既有 lease/governance/importer 路径通过；首次 cleanup 被安全策略拒绝且未启动容器，修正明确目标后通过；实现 SHA 的远程 integration job 也已成功 |
| P2-06 migration | 临时 SQLite/真实 PG 往返与 check 全绿；当时默认用户 SQLite 非 head 的 check 失败后未迁移 | P2-06 当时 head `20260828_0005`；历史证据保持原状 |
| 2026-08-29 DB compatibility repair | migration `52 passed`；最终完整 backend `927 passed, 33 skipped`、frontend `38 passed`；lint/smoke/config、真实失败备份副本与当前库 startup/check 全绿 | current head `20260829_0006`；`8fb51b6…` 的 run `33263405214` 4/4 成功 |
| P2-06 build/config | frontend build 成功（保留 662.39 kB chunk warning）；Compose config exit 0 | 从根目录误跑 npm 的失败已记录并用正确目录重跑通过 |
| P2-06 Prometheus 规则 | `prom/prometheus:v3.5.0` 中 `promtool check rules` 成功，八条规则全部通过 | 临时容器验证；仓库仍不部署 Prometheus/Alertmanager |
| P2-06 dirty acceptance | 9/9；artifact `llmbenchlab-p2-11554c25ec2d/evidence.json`，SHA-256 `d5f058457dbc29875cbac4bc38345b810b5ed556ea538862d309116ceb629fde`，`dirty=true` | Worker `2/2/2/0/0`、`0005`/isolated `0004` populated refusal、两层空库往返、cleanup C/V/N empty |
| P2-06 dirty capacity | 历史 artifact `llmbenchlab-p2-c6de062ab77e/evidence.json`，SHA-256 `4aeb8271dd81e8671fc287942839f8d06862140ea9a6bf1d7ee5660265aa8453` 通过 | `dirty=true`；18/270/270/271/1229，0 error/drift/duplicate/PEL/lag，Worker expected=2、cleanup C/V/N/image=0；offline Mock、非 SLO |
| P2-06 clean acceptance | 9/9；artifact `llmbenchlab-p2-92e173eeee28/evidence.json`，SHA-256 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`，`dirty=false`，commit `9a20676…` | Worker `2/2/2/0/0`；cleanup C/V/N empty |
| P2-06 clean capacity | artifact `llmbenchlab-p2-ca5673061b0f/evidence.json`，SHA-256 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`，`dirty=false`，commit `9a20676…` | QPS `7.267474/12.962228/9.333604`，wall `8.255963/4.628834/6.428385s`；18/270/270/271/1230；0 question error/drift/duplicate/PEL/lag；expected=2、shortfall=0；cleanup C/V/N/image=0、image `1/1/0/0`；Mock、非 SLO |
| P2-06 补充静态检查 | 过宽 `scripts/` Ruff 命令暴露 93 条既有 modernization 告警；`--select E,F,I` 通过 | 如实保留首次结果，不把范围外历史告警归为本次回归 |
| P2-06 staged 技术/安全终审 | structured-extra High 与 Worker `__main__` logger Medium 已修复；76-file index 为 0 Blocker/High/Medium；hydration/import integrity 目标集 `67 passed` | 已进入实现 SHA `9a20676…` |
| P2-06 实现远程 CI | PR #3；`9a20676dcf545040782f04c166205d0043345753` 的 run `33164609388` 4/4 | 实现精确 SHA 门禁完成 |
| P2-06 evidence 文档远程 CI | `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6` 的 run `33165775037` 4/4 | 精确文档 SHA 门禁完成；P2-06 为 `completed` |
| P2-01 实现远程 CI | `b6a35fe…` run `33146681285` 4/4 | 精确实现 SHA 全绿；PR #2 已于 2026-08-28 合并 |
| P2-01 证据文档收尾 CI | `875f13a…` run `33150080341` 4/4 | 精确文档 SHA 全绿；P2-01 仓库级收尾完成 |
| 最新本地 `make lint` | Ruff/format、ESLint、TypeScript 通过 | 本地冻结树通过 |
| P2-01 冻结树 `make test` | 后端 `829 passed, 29 skipped`；前端 `38 passed` | v2 实现历史冻结树通过；当前 P2-06 全量见上方独立行 |
| P2-01 真实 PostgreSQL/Redis integration | `29/29 passed` | v2 实现历史冻结树通过；当前 P2-06 integration 见上方独立行 |
| 最新本地 `make smoke` | `1 passed, 7 deselected`，仅 Mock | 本地冻结树通过；没有调用真实 Provider |
| 定向治理/API/Worker | 目标套件零失败；早期独立审计 `218 passed`；完整性边界集合 `18 passed` | 已被最终全量、真实 integration 与精确候选 evidence 补充 |
| SQLite/PostgreSQL migration | 隔离 SQLite 与真实 PostgreSQL prepare/upgrade/downgrade guard/upgrade/check 通过 | 候选与远程 integration 覆盖 |
| 增强 capacity | 精确 `665244e…`，evidence SHA-256 `40deadeb…0588` | passed；Mock-only，cleanup 容器/卷/网络为空，不是生产 SLA |
| 完整 acceptance | 精确 `665244e…`，9/9，evidence SHA-256 `ab311665…ddec` | passed；含三条 deterministic DB seam，cleanup 为空 |
| 正式 v2 单机资格 | 精确 `b6a35fe…`，1+5、23/23，aggregate SHA-256 `a76d167b…d0d9` | passed/qualified；Mock-only 单机控制面，不是生产或真实 Provider SLA |
| 真实 Provider | 未运行（有意） | 所有自动化只使用 Mock/Stub/MockTransport |

详细命令与限制见 [当前 P2-06 工作日志](worklogs/2026-08-28-phase-2-observability-retention.md) 和 [TESTING.md](TESTING.md)。

## 最近工作日志

- [Phase 2 可靠执行基础](worklogs/2026-08-25-phase-2-reliable-execution-foundation.md)
- [完整客观评测流程](worklogs/2026-08-27-complete-evaluation-workflow.md)
- [Web Provider 凭据](worklogs/2026-08-27-web-provider-credentials.md)
- [Web Run UX 与生成预算](worklogs/2026-08-27-web-run-ux-and-generation-budgets.md)
- [OpenAI-compatible SSE](worklogs/2026-08-27-openai-compatible-sse-streaming.md)
- [Phase 2 治理、审计与性能](worklogs/2026-08-27-phase-2-governance-audit-performance.md)
- [Phase 2 正式 SLO 与容量模型](worklogs/2026-08-28-phase-2-slo-capacity-model.md)
- [Phase 2 可观测性与审计保留](worklogs/2026-08-28-phase-2-observability-retention.md)

## 当前任务入口

[NEXT_TASK.md](NEXT_TASK.md) 提供后续任务入口。P2-06 已完成仓库级收尾；P2-07 工作包已建立、状态为 `planned`，后续从最小只读 recovery verifier 开始实施。Phase 2 继续保持 `in_progress`。
