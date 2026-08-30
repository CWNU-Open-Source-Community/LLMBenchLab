# 项目状态

> 更新时间：2026-08-30（Asia/Shanghai）

## 当前阶段

- Phase 0 — 项目治理和架构：`completed`（2026-08-24）
- Phase 1 — MVP 垂直链路：`completed`（2026-08-25）
- Phase 2 — 可靠性与任务执行：`in_progress`（可靠基础、治理/审计、P2-01 单机资格与 P2-06 已完整交付；P2-07 工作包已建立，状态为 `planned`，功能尚未实现）
- Phase 3 — 标准 Benchmark 与代码评测：`in_progress`（可信本地 MMLU-Pro/GPQA-Diamond 与 P3-06 Run Detail 热力图/live metrics 切片已交付；IFEval、沙箱与完整插件体系仍未完成）
- Phase 4–6：`planned`

## 当前版本与远程边界

`0.1.0` development baseline，REST API 为 `/api/v1`，评测协议为 `llmbenchlab-protocol-v1`；尚未发布正式 Release。

公开仓库：[`CWNU-Open-Source-Community/LLMBenchLab`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab)，当前开发分支为 `codex/complete-evaluation-workflow`。P2-06 实现 SHA [`9a20676dcf545040782f04c166205d0043345753`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/9a20676dcf545040782f04c166205d0043345753) 已普通 push 并进入 [PR #3](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/3)，其精确 SHA 的 GitHub Actions [run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) 四个必需 job 全部成功；绑定该 clean SHA 的 capacity 与 9/9 acceptance 也已通过。Evidence closeout 文档 commit [`ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6) 已 push，其精确 SHA 的 GitHub Actions [run `33165775037`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33165775037) 四个必需 job 全部成功，因此 P2-06 已完成仓库级收尾并标记为 `completed`。[ADR-0017](decisions/ADR-0017-schema-equivalent-governance-index-repair.md) / `20260829_0006` 数据库兼容修复实现 SHA [`8fb51b690ae6335b8ef93b3cbe54e039781fb173`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/8fb51b690ae6335b8ef93b3cbe54e039781fb173) 已普通 push，其精确 SHA 的 GitHub Actions [run `33263405214`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33263405214) 四个必需 job 全部成功，因此该维护任务为 `completed`。[ADR-0018](decisions/ADR-0018-observational-token-estimates-are-not-hard-reservations.md) / data-only `20260830_0007` 已修复 observational input estimate 误触发 overdraw；本地完整门禁、当前个人 SQLite 数据验真与最终 SHA [`cb00924ea3ba3d01ce5bc322b7eabdae1345baf3`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/cb00924ea3ba3d01ce5bc322b7eabdae1345baf3) 的 [run `33271095910`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33271095910) 4/4 全部通过，该维护为 `completed`。Phase 2 仍为 `in_progress`；P2-07 已建立 ADR-0016、独立计划和工作日志，状态为 `planned`，功能实现尚未开始。历史 P2-01 位于 [PR #2](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/2)：实现 SHA `b6a35fef1dd069ebb54b69955058915c722aa34d` 的 [run `33146681285`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 4/4 成功，证据文档 commit `875f13a253c40b7573d45c6287385e60f2bb8f04` 的 [run `33150080341`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33150080341) 也已 4/4 成功。

Run Detail 指标维护实现 SHA [`0003e4291769a851005ba46c7e59b156a6b789eb`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/0003e4291769a851005ba46c7e59b156a6b789eb) 已普通 push 并进入 [PR #5](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/5)；其精确 SHA 的 [GitHub Actions run `33286730109`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33286730109) 对 backend、真实 PostgreSQL/Redis integration、real-Compose acceptance 和 frontend 四个 job 全部成功，因此该维护为 `completed`。它不改变 Phase 2/3 或 P2-07 状态。

P3-06 的 [Run Detail 热力图/live metrics 计划](plans/2026-08-30-run-progress-heatmap-live-metrics.md) 状态为 `completed`。公共合同已从初版无可靠提交序的 cursor 改为固定 `512` 题 absolute-position blocks：progress index 在同一数据库读取快照返回 evidence-derived live metrics 与所有 block counts，block payload 只返回 position/outcome/score/latency/usage/cost/error type 白名单。该切片保持 `/api/v1` 与 `llmbenchlab-protocol-v1`，无 migration、ADR 或 SECURITY 边界修改；初版 cursor 失败先行套件记录为 `4 failed` 后已废弃。实现 SHA [`99791964621165c9cc7ec36b4b2d27fe04e6acd5`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/99791964621165c9cc7ec36b4b2d27fe04e6acd5) 已普通 push 到 `codex/complete-evaluation-workflow` 并进入 [PR #5](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/5)；精确 SHA 的 [GitHub Actions run `33289522923`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33289522923) 对 backend、backend-integration、full-stack-reliability、frontend 四个必需 job 全部成功。Phase 3 整体仍为 `in_progress`，P2-07 恢复为下一项且仍为 `planned`。

## 已交付基线

- Phase 0/1 的治理、架构、协议、数据格式、ADR、FastAPI/SQLAlchemy/Alembic、React/TypeScript、Mock 垂直链路、三类 Evaluator、Demo 数据、API/UI、离线测试和开源流程。
- PostgreSQL/Redis 可靠执行基础：数据库事实来源、Redis at-least-once 通知、独立 Worker、DB scan、租约/heartbeat/fencing、逐题幂等、有限 retry/backoff、取消、租约接管、dead-letter 和终态 Response 重算。
- OpenAI-compatible SSE、严格 `[DONE]`、JSON fallback、identity-only、wire/event/content/error 上限、idle read timeout、bounded error 与精确当前-Key 脱敏。
- Web write-only `api_key`、AES-256-GCM `model_credentials`、数据库外 API/Worker 共享 keyring、legacy `api_key_env`、origin/active-Run 门禁和 fail-closed repair/remove 路径。
- MMLU-Pro test 与 GPQA-Diamond 固定 revision/SHA 转换、可信本地 `llmbenchlab-evaluate prepare/run/resume/report`、请求上界确认和原子终态报告。该 CLI 仍要求独占数据库，未受 Phase 2 managed budget 保护。
- React 中文界面覆盖 Dashboard、Models、Benchmarks、Evaluation Runs、New Run、Run Detail、Leaderboard；Run 列表全状态筛选/分页/活动轮询，详情逐题分页，关键桌面/平板/移动布局已修复。Run Detail 现区分未得分、普通答错与执行异常，并在精确 Token 未知时显示 Run-wide 已知小计、输入/输出覆盖率和“不完整”提示。

## P3-06 Run Detail 热力图/live metrics（`completed`）

- 用户 Run `a3de7e4d-40b2-4d8c-994b-c713047393ae` 的只读证据对账为 total/completed/correct/error=`198/198/179/2`；198 条 Response 中 `score < 1` 为 19、`error_type` 非空为 2，因此互斥四态应为通过 179、普通答错 17、执行异常 2、未执行 0。旧卡片显示“错误题 2”实际只反映执行异常，已复现原问题。
- 同一 Run 的已知 input/output Token 为 `45,509 / 4,561,625`，各自覆盖 `196/198`；平均延迟为 `181,454.235 ms`。Run 精确 input/output/cost 仍为 `null`，新 UI 只能显示 known subtotal + reported coverage，不能把两条缺失 usage 当 0 或回填账单真值。这里未记录任何 Response 正文。
- 后端合同固定为 `GET /runs/{id}/progress` index 与 `GET /runs/{id}/progress/blocks/{block_index}` payload，`block_size=512`。outcome 优先级为 execution error、passed、wrong；没有 Response 的计划 position 才是 `not_run`。两个响应均 `no-store`，不包含 ID、题目/回答正文、error message 或 Provider metadata。
- 前端已实现虚拟化 ARIA grid、非仅颜色图例、hover/focus/tap 等价详情和独立 block reducer/poller；非空 block 追齐前显示“同步中”，terminal 先到仍追齐，当前 Responses 页码与 progress 更新互不重置。
- 本地证据：backend target `37 passed`；frontend target `32 passed`（Run Detail `20` + heatmap `12`）；完整 backend `964 passed, 33 skipped`、frontend `64 passed`；`make lint`、Mock smoke `1 passed, 7 deselected`、frontend build 与 `docker compose config --quiet` 通过。终态且 progress 已 reconciled 时只做一次最终 Run/当前 evidence 页刷新；同路由切换 `runId` 会把 evidence offset 重置为 0。12,032/20,000 题是自动化虚拟化边界，不是大型真实 Run 的手工 DevTools 性能测量。
- 目标 Run 实页显示通过 179、普通答错 17、执行异常 2、未执行 0，Token `45,509 / 4,561,625`、输入/输出覆盖均为 `196/198`；desktop/768/375 无横向溢出，console 无 warning/error，键盘与 Tooltip 验收通过。
- 实现 SHA `99791964621165c9cc7ec36b4b2d27fe04e6acd5` 已普通 push，PR #5 的 exact-SHA Actions run `33289522923` 四个必需 job 全部成功，因此本切片为 `completed`。Phase 3 整体仍为 `in_progress`；P2-07 恢复为下一独立任务并保持 `planned`。

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
- P2-06 的 `0005` 将 importer 逻辑合同扩展为 13 表精确 count/PK/content digest；source/target 必须位于唯一 current head，现为 data-only `0007`，13 表 schema/内容合同不变。live generation 在源 preflight 被拒绝，stopped/stale facts 可复制，终审又补强 committed target canonical integrity postverify。`0005 -> 0004` 在 `worker_processes` 非空时于 DDL 前拒绝，原有 `0004` governance/audit downgrade guard 继续保留。
- 生产日志源已统一治理：应用日志消息必须是无格式参数字面量，结构化字段按白名单和有限数值输出，第三方动态消息固定化且不能通过 allowlisted extra 注入，raw Uvicorn access handler 关闭。Archive 终审补充了 FIFO/非普通文件拒绝及 decode 前行数上限；retention 零行 mutation 仍须 postverify，PostgreSQL mutation 保持 advisory/row lock。
- 上述实现的全部实现门禁已完成：合并定向套件、`make lint`（Ruff 152 files、ESLint、TypeScript）、`make test`（后端 `916 passed, 33 skipped`、前端 `38 passed`）、Mock smoke（`1 passed, 7 deselected`）、临时 PostgreSQL 16/Redis 7 migration/check 与真实 integration（`33 passed, 0 skipped`）、隔离 SQLite migration/check、frontend build、Compose config、八规则 `promtool` 和修复后 76-file staged 技术/安全终审均通过；实现 SHA 已 push，精确 SHA run `33164609388` 4/4 成功。Clean acceptance `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-92e173eeee28/evidence.json` 的 SHA-256 为 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`，9/9 通过，Worker expected/registered/live/stalled/shortfall=`2/2/2/0/0`，cleanup C/V/N 全空。Clean capacity `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-ca5673061b0f/evidence.json` 的 SHA-256 为 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`；1W/2W/burst QPS=`7.267474/12.962228/9.333604`、wall=`8.255963/4.628834/6.428385s`，最终 18 Runs/270 Responses/270 question executions/271 reservations/1230 audit，0 question error/drift/duplicate/PEL/lag，Worker expected=2、shortfall=0，cleanup C/V/N/image 全零且 image counters=`1/1/0/0`。两份 evidence 均为 `dirty=false` 并绑定 `9a20676…`；这是 Mock-only、非 SLO。此前 dirty acceptance/capacity 继续作为历史证据保留。Evidence closeout 文档 commit `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6` 已 push，精确 SHA run `33165775037` 4/4 成功，P2-06 仓库级收尾完成。P2-06 当时默认用户 SQLite 尚未在 head，直接 `alembic check` 失败后按保护原则未擅自迁移。

## 2026-08-29 数据库兼容修复（`completed`）

- 旧库失败不是 SQLite 损坏，而是 revision=`20260827_0004` 的早期结构变体精确缺少三个后来加入 canonical `0004` 的索引；严格 preflight 因 revision/schema 不一致，在 `0005` 执行前按设计拒绝。
- [ADR-0017](decisions/ADR-0017-schema-equivalent-governance-index-repair.md) 已接受；该任务完成时的 head 为 schema-equivalent `20260829_0006`，现作为 `0007` 的历史下游 revision。preflight 只接受 canonical `0004/0005`，或仅缺一至三个已知 repair 索引的 fingerprint，以支持 SQLite repair 中断重入；PostgreSQL `0005` 的额外 metadata drift 同样拒绝。完整 schema/integrity/FK、索引定义、single-active policy 数据门禁仍 fail closed。`0006` 首条 DDL 前再次拒绝多条 active policy，并条件补建索引；`0006 -> 0005` 保留 canonical 对象。
- 真实失败备份副本已无损升级到 `0006` 并通过 integrity/FK/Alembic check；当前重建库由标准 preflight 自动备份后从 canonical `0005` 到 `0006`，startup gate、quick check、FK 和 metadata check 全部通过，业务计数与 Worker facts 保持。
- 本地 migration `52 passed`；最终完整 `make test` 为后端 `927 passed, 33 skipped`、前端 `38 passed`，`make lint`、Mock smoke 和 Compose config 均通过。实现 SHA `8fb51b690ae6335b8ef93b3cbe54e039781fb173` 已 push，[run `33263405214`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33263405214) 的 backend、真实 PostgreSQL/Redis、完整 Compose 与 frontend 四个 job 全部成功；本维护任务完成，不改变 Phase 2/P2-07 状态。

## 2026-08-30 本地数据恢复与静默启动（`completed`）

- 默认本地 SQLite 重建库逻辑为空；多份约 96 MiB 候选也因 freelist 较大但业务表全空，不能恢复数据。最新且 revision 最高的非空一致性备份为 `backend/data/llmbenchlab.db.pre-alembic-20260827T073137431634Z.bak`（SHA-256 `7e046c1e7cd4ec39c5fe6f57b34f130670e0d249a70bf052a84a23e085a59a53`），含 1 个 Mock Model、1 个 Demo Benchmark、15 Questions、1 个 completed Run 和 15 Responses；与更早两份非空备份在全部共有列的内容摘要一致。
- 停止 API/Worker/Vite 并确认默认库无占用/sidecar 后，只在该备份的一致性 staging 副本上执行 `0002→0006`。迁移前后五张旧表共有列摘要都为 `d1b3b74b7726f9e7903fbd3f445ad258d5f5aa4b885c976582f2d53e1d30302f`；恢复后的默认库 `quick_check=ok`、外键错误 0、Alembic current=head，API/Web 实际读取到 `1/1/1` 个 Model/Benchmark/Run。原始恢复源未修改；重建空库另存为 `backend/data/llmbenchlab.db.pre-original-data-restore-20260829T170121Z.bak`（SHA-256 `ec2ef8b2d5c9a338ce3e5f94c68a3c5742d288a798df2b7a6096960a48610c90`）。
- `make dev` 现在只在控制台显示地址和日志位置；API、Worker、Vite 详细输出分别 append 到私有的 Git 忽略日志，单服务 Make 入口仍保留前台诊断。离线启动器 `3 passed`，真实本地启动/live/health/ready/API/Web 探针通过；完整 `make test` 为后端 `930 passed, 33 skipped`、前端 `38 passed`，lint/build/smoke/Compose config 也已通过。实现 commit `5075bdb5e9b53f527a43e5aff7b7d2c7b48c5c9b` 已 push，[run `33265171953`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33265171953) 四个必需 job 全部成功，因此本维护为 `completed`。
- 该维护只恢复个人本地 SQLite Demo 数据并改进开发入口，不是 P2-07 的 PostgreSQL+keyring backup/restore、Redis 重建或灾难恢复认证；Phase 2/P2-07 状态不变。

## 2026-08-30 已下载标准评测集本地加载（`completed`）

- `artifacts/benchmarks/` 中三个 Git 忽略 dataset-v1 ZIP 均由当前 Loader 校验通过：GPQA-Diamond `198` 题、MMLU-Pro Direct `12,032` 题、MMLU-Pro Official-CoT `12,032` 题。原始 `dataset-cache` 仍只作为固定来源缓存，没有被当作导入包。
- 在现有 `make dev` 服务无活动 Run 时，先用 SQLite 在线 backup 创建 `0600` 的 `backend/data/llmbenchlab.db.pre-benchmark-import-20260829T173032Z.bak`（SHA-256 `d8c71c44b0ee364030d0053788f34bd985a443ba30a9cd17f1069a9737d10206`），再通过正式 `/api/v1/benchmarks/import` 逐个导入，三个请求均为 `201`。
- 该次导入完成时，默认个人 SQLite 有 `4` 个 Benchmark、`24,277` 道题；逐集持久化题数与 manifest 一致。当时原有 `1` 个 Model、`1` 个 completed Run 和 `15` 条 Response 不变，active Run 为 `0`；`quick_check=ok`、外键错误 `0`、Alembic head=`20260829_0006`，API 列表/逐集 total 也已对账。后续 Run 与 `0007` 维护事实见下节，不回写本条历史快照。
- 目标 Loader/标准转换器离线测试 `40 passed`；未调用真实 Provider，未修改 Schema/API/协议或产品代码。仓库记录 commit [`0163b67c00eb59ae59db5f3adb679ad85c799142`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/0163b67c00eb59ae59db5f3adb679ad85c799142) 已 push，其精确 SHA 的 [run `33266167547`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33266167547) 四个必需 job 全部成功；本地加载维护为 `completed`，不改变 Phase 3/P2-07 状态。

## 2026-08-30 小型评测集本地加载（`completed`）

- 从固定官方/维护者 revision 准备 6 套各 100 题的 Git 忽略 dataset-v1 ZIP：GSM8K、中文 MGSM、HellaSwag、WinoGrande、TruthfulQA Binary 五套稳定 mini 子集，加上完整 100 题的中文 XCOPA validation。题型仅使用当前可自动评分的 numeric/multiple-choice；seed 42 的稳定选择/转换、源/archive/Dataset Hash、许可、源路径/行数、所选源行和答案分布均记录在本地 provenance 清单。TruthfulQA 固定 CSV 没有官方 split；本地 Best Answer 对 Best Incorrect Answer 的 binary mini 不能与官方 MC1/MC2 全量分数混用。
- 导入前 SQLite online backup 冻结为 Models/Benchmarks/Questions/Runs/Responses=`2/4/24,277/5/1,000`，并保留当时唯一活动 Run 的 `765/12,032` 进度。六个正式导入请求均返回 `201`；默认个人 SQLite 现有 `10` 个 Benchmarks、`24,877` 道 Questions，六个新 Benchmark 的 API/manifest/数据库题数与 Dataset Hash 一致。
- 导入任务没有创建、取消、重置或修改 Run；当时既有 12,032 题 Run 继续推进，Response 数量按预期增长。导入完成后的并发客户端活动随后请求取消该大 Run，并创建了 MGSM mini Run；这些变化有独立 Run/audit 时间线，不是导入副作用。本任务没有直接触发 Provider。导入后 `quick_check=ok`、外键错误 `0`、head=`20260830_0007`；用金标做评分器格式自检时 600/600 reference answers 被接受（不是模型 600/600 成绩），工程目标测试 `40 passed`。记录 commit [`8faa2093b2c3308994d50e42a31063cdbf5264a6`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/8faa2093b2c3308994d50e42a31063cdbf5264a6) 已 push，其精确 SHA 的 [run `33296049611`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33296049611) 四个必需 job 全部成功。本维护不改变产品代码、Schema、API、协议、Phase 3 或 P2-07 状态。

## 2026-08-30 observational Token overdraw 修复（`completed`）

- 只读核查 Run `2181503c-eab2-4699-bede-db48bd078f95` 发现：15 题完成 7 题后以 `failed/exhausted`、`governance_global_overdrawn` 终止；7 个 Provider attempt 均为 `settled_actual/succeeded`，没有 conservative settlement、429 或 HTTP retry。第七次非 hard 输入估算/reservation 为 59，而 Provider actual 为 75；冻结 policy 与 Run override 的 request/Token/cost hard limit 全为 `null`，因此这是本地语义缺陷，不是 OpenCode Go 套餐额度结算。
- [ADR-0018](decisions/ADR-0018-observational-token-estimates-are-not-hard-reservations.md) 已接受：只有显式 `evaluation_runs.input_token_reservation` 才构成 input/cost hard reservation；没有显式值时不再写入新的 attempt input reservation/reserved cost，但 Provider actual usage 仍完整保存。显式 input、显式 `max_tokens` output，以及由完整上界和冻结价格派生的 reserved cost 超额仍 fail closed。
- 应用 Alembic head 已前进到 data-only `20260830_0007`。该 migration 不改 schema、ledger、actual usage、Response、audit 或 Run 终态，只重算 `governance_scopes.overdrawn`；upgrade/downgrade 在任何更新前拒绝 active reservation。前端 overdrawn 文案改为“实际用量曾被判定超过预留”，既适用于新 hard overdraw，也不会误述升级前保留的历史终态。
- 本地验证已完成：backend `946 passed, 33 skipped`，真实 PostgreSQL+Redis integration `33 passed`，双方言 migration upgrade/downgrade/upgrade/check、`make lint`、frontend `39 passed`/build、Mock smoke `1 passed`、real-Compose `9/9` 与 Compose config 均通过。当前个人 SQLite 已到 `0007`；四层 scope `overdrawn` 从 4 降为 0，7 Responses/7 ledger、407 input/599 output、13 张业务表行数均保留，`quick_check=ok`、FK=0。未调用真实 Provider。首次实现 SHA 的 acceptance-only `float(None)` 失败已保留，最终修正 SHA `cb00924…` 的 run `33271095910` 4/4 成功，因此本维护为 `completed`。

## 2026-08-30 Run Detail 错题与部分 Token 展示修复（`completed`）

- 目标 Run `a3de7e4d-40b2-4d8c-994b-c713047393ae` 的 198 条证据实际为正确 179、普通答错 17、执行异常 2；旧页面把只统计异常的 `error_questions=2` 标成“错误题”。页面现显示未得分 19，并明确拆分三类数量；当前页也分别统计未得分与执行异常。
- 196/198 条 Response 有 usage，已知输入 45,509、输出 4,561,625。protocol-v1 精确 Run Token 继续因两条缺失而保持 `null`；Responses API 追加分页无关的输入/输出已知小计和独立覆盖数，页面显示“已知小计”和“完整总量未知”，不会修改历史数据或冒充 Provider 账单。
- API/UI、OpenAPI、零/全/部分/非对称 usage、合法零 Token、分页、并行快照竞态与页内错题拆分的目标测试、完整本地门禁和目标实页核对均通过。实现 commit `0003e429…` 已普通 push；PR #5 的精确 SHA CI run `33286730109` 4/4 成功。本维护不改变 Phase 2/3 或 P2-07 状态。

## 状态与后续

- P2-06：状态为 `completed`；实现、clean-SHA Compose evidence、实现 commit 与 evidence closeout 文档 commit 的 push 和精确 SHA CI 均已完成。
- 0004 历史索引兼容修复：状态为 `completed`；实现 commit `8fb51b690ae6335b8ef93b3cbe54e039781fb173` 已 push，精确 SHA run `33263405214` 4/4 成功。
- 本地数据恢复与静默启动：状态为 `completed`；实现 commit `5075bdb5e9b53f527a43e5aff7b7d2c7b48c5c9b` 已 push，精确 SHA run `33265171953` 4/4 成功。
- 已下载标准评测集本地加载：状态为 `completed`；三个现有正式 ZIP 已导入并完成本地数据库/API/目标测试验证，`0163b67…` 的 run `33266167547` 4/4 成功。
- 小型评测集本地加载：状态为 `completed`；五套 100 题 mini 子集与完整 100 题 XCOPA validation 已生成并导入，Loader/Evaluator、API/数据库/Hash 与完整性验证通过；本导入任务没有创建或取消 Run，后续并发 Run 操作另行留有审计时间线；`8faa209…` 的 exact-SHA run `33296049611` 4/4 成功。
- observational Token overdraw 修复：状态为 `completed`；目标 head `0007`、本地完整验证、当前库迁移、最终修正 commit/push 与 exact-SHA CI 4/4 均完成。
- Run Detail 错题与部分 Token 展示修复：状态为 `completed`；实现 SHA `0003e429…` 已 push，PR #5 的精确 SHA CI run `33286730109` 4/4 成功。
- P3-06 Run Detail 热力图/live metrics：状态为 `completed`；实现 SHA `99791964621165c9cc7ec36b4b2d27fe04e6acd5` 已普通 push，PR #5 的精确 SHA CI run `33289522923` 4/4 成功；不改变 protocol-v1 或 Phase 3 整体 `in_progress` 状态。
- P2-07：状态为 `planned`，已建立 [ADR-0016](decisions/ADR-0016-postgresql-keyring-recovery-and-redis-rebuild.md)、exact-head amendments [ADR-0017](decisions/ADR-0017-schema-equivalent-governance-index-repair.md) / [ADR-0018](decisions/ADR-0018-observational-token-estimates-are-not-hard-reservations.md)、[独立计划](plans/2026-08-28-phase-2-recovery-operations.md) 和 [工作日志](worklogs/2026-08-28-phase-2-recovery-operations.md)。PostgreSQL backup/restore、数据库与 keyring 配对恢复、Redis 重建、Worker 扩缩/告警处置和剩余故障矩阵的功能实现尚未开始；P2-06 的 audit archive 自身 restore 不能替代整库恢复认证。P2-07 recovery-manifest-v1 的尚未实施 exact head 现为 `20260830_0007`。
- Phase 3：IFEval、通用 Dataset Plugin SDK、代码题 schema/隔离沙箱、完整分组 UI 和安全红队；Phase 4–6 尚未开始。

## 已知边界与风险

- SQLite 只用于个人本地单 Worker；多 Worker 证据必须来自 PostgreSQL。Compose 是本地开发/验收拓扑，不是生产 HA。
- Provider 调用不是 exactly-once。Worker 在 Provider response 后本地 commit 前崩溃可能重复上游计算或费用；本地 ledger/Response 幂等只能保证数据库事实不 double-count。
- 无显式 input reservation 的估算不是费用上界；`0007` 只防止该观测值被误解释为 hard overdraw，不会推断 Provider tokenizer、限制真实账单或替代显式 Token/cost policy。
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
| 2026-08-29 DB compatibility repair | migration `52 passed`；最终完整 backend `927 passed, 33 skipped`、frontend `38 passed`；lint/smoke/config、真实失败备份副本与当时默认库 startup/check 全绿 | 该任务当时 head `20260829_0006`；`8fb51b6…` 的 run `33263405214` 4/4 成功 |
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
| 2026-08-30 本地恢复/静默启动 | 启动器 `3 passed`；完整 backend `930 passed, 33 skipped`、frontend `38 passed`；lint/build/smoke/config、恢复库 digest/quick/FK/head 与真实 API/Web 读取通过 | 默认库恢复 `1/1/15/1/15`；`5075bdb…` 的 run `33265171953` 4/4 成功，不改变 P2-07 |
| 2026-08-30 标准评测集本地加载 | 三个 ZIP Loader 校验通过；API 导入 `201/201/201`；数据库/API 对账为 `4` Benchmarks、`24,277` Questions；`quick_check=ok`、FK `0`、head `0006`；目标测试 `40 passed` | 本地加载完成，原 Model/Run/Response 保持；`0163b67…` 的 run `33266167547` 4/4 成功，无 Provider 调用 |
| 2026-08-30 小型评测集本地加载 | 六个 ZIP Loader 校验、重复生成与 archive/Dataset Hash 对账通过；API 导入 `201` × 6；金标评分器格式自检 600/600（非模型成绩）；目标测试 `40 passed`；数据库为 `10` Benchmarks、`24,877` Questions，`quick_check=ok`、FK `0`、head `0007` | 六套均为 100 题且可直接选择；导入任务未创建/取消 Run，既有大 Run 与随后创建的 MGSM mini Run 的 Response/Provider 流量属于并发客户端操作，不归因于导入；`8faa209…` 的 run `33296049611` 4/4 成功 |
| 2026-08-30 observational overdraw 修复 | backend `946 passed, 33 skipped`；真实 PG+Redis integration `33 passed`；双方言 migration 往返/check、`make lint`、frontend `39 passed`/build、Mock smoke `1 passed`、real-Compose `9/9`、Compose config 与当前库 backup/migrate/check 通过 | 当前 SQLite head=`20260830_0007`，scope `4→0`，7 Responses/7 ledger/407 input/599 output、13 表行数、quick/FK 保持；无真实 Provider；`cb00924…` 的 run `33271095910` 4/4 成功 |
| 2026-08-30 Run Detail 指标修复 | backend API/Smoke 目标 `11 passed`、frontend Run Detail/format `20 passed`；完整 backend `951 passed, 33 skipped`、frontend `47 passed`；lint/build/Mock smoke/Compose config/实页验收通过 | 目标 Run 19/17/2、已知 Token `45,509/4,561,625` 和 `196/198` 覆盖可见；无真实 Provider；`0003e429…` 的 run `33286730109` 4/4 成功 |
| P3-06 Run Detail 热力图/live metrics | 初版 cursor 后端 red suite `4 failed`（预期且合同已废弃）；fixed-block backend/frontend target `37/32 passed`（20 Run Detail + 12 heatmap）；完整 backend `964 passed, 33 skipped`、frontend `64 passed`；lint/smoke/build/config/目标 Run 浏览器验收通过 | `99791964621165c9cc7ec36b4b2d27fe04e6acd5` 已 push；PR #5 exact-SHA run `33289522923` 4/4；切片 `completed`，Phase 3 仍 `in_progress`，P2-07 为下一任务 |
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
- [本地数据恢复与静默启动](worklogs/2026-08-30-restore-data-quiet-startup.md)
- [加载已下载的标准评测集](worklogs/2026-08-30-load-downloaded-benchmarks.md)
- [准备并加载小型模型评测数据集](worklogs/2026-08-30-small-benchmark-datasets.md)
- [修复 observational Token overdraw](worklogs/2026-08-30-fix-observational-token-overdraw.md)
- [修复 Run Detail 错题与部分 Token 展示](worklogs/2026-08-30-fix-run-detail-metrics.md)
- [Run Detail 热力图与实时指标](worklogs/2026-08-30-run-progress-heatmap-live-metrics.md)

## 当前任务入口

[NEXT_TASK.md](NEXT_TASK.md) 现提供 P3-06 热力图/live metrics 完成后的 P2-07 最小只读 recovery verifier 入口。P3-06 实现、push 与精确 SHA CI 已完成并标为 `completed`；P2-07 恢复为下一项但仍为 `planned`。Phase 2 与 Phase 3 继续保持 `in_progress`。
