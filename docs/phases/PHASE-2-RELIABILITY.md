# Phase 2：可靠性与任务执行

- 状态：`in_progress`
- 前置阶段：[Phase 1 — MVP](PHASE-1-MVP.md)（`completed`）
- 后续阶段：[Phase 3 — Benchmarks](PHASE-3-BENCHMARKS.md)
- 核心决定：[ADR-0005](../decisions/ADR-0005-durable-task-execution.md)
- 凭据安全：[ADR-0007](../decisions/ADR-0007-web-provider-credentials.md)
- Provider transport：[ADR-0008](../decisions/ADR-0008-openai-compatible-sse-transport.md)
- 治理与审计：[ADR-0009](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)
- 交付边界修正：[ADR-0010](../decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)
- pre-send retry 修正：[ADR-0011](../decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)
- 单机资格：[ADR-0012](../decisions/ADR-0012-single-host-slo-capacity-qualification.md)
- 镜像指纹修正：[ADR-0013](../decisions/ADR-0013-stable-image-content-fingerprint.md)
- 双 backlog 资格：[ADR-0014](../decisions/ADR-0014-dual-backlog-slo-profile.md)
- 可观测性与审计保留：[ADR-0015](../decisions/ADR-0015-observability-worker-progress-audit-retention.md)
- Schema-equivalent 索引修复：[ADR-0017](../decisions/ADR-0017-schema-equivalent-governance-index-repair.md)
- Observational reservation 修正：[ADR-0018](../decisions/ADR-0018-observational-token-estimates-are-not-hard-reservations.md)

## 阶段目标

把单进程 SQLite MVP 升级为以 PostgreSQL 为共享事实来源、Redis Streams 为可丢失/可重复通知层、独立 Worker 为唯一常规执行入口的可靠任务系统。在数据库时间、租约/fencing、逐题幂等、可恢复治理和 typed audit 保护下维持 `llmbenchlab-protocol-v1` 证据。本阶段不承诺 Provider exactly-once、生产 HA、无限横向扩展；已完成的 Mock-only 单机资格也不是生产或真实 Provider SLA。

## 当前功能范围

- PostgreSQL 多 Worker 目标、SQLite 单 Worker 兼容；日常入口维护已把 `make dev DEV_WORKERS=N` 和默认双 Worker的 `make dev-multi` / `make docker-up WORKERS=N` 暴露为受保护入口，并按扩/缩方向同步 scale、API expected 与 expected/registered/live/stalled/shortfall。P2-06 implementation SHA `9a20676…` 将双方言 Alembic 链扩展至 `20260828_0005`，`20260829_0006` 仅修复早期 `0004` 三索引缺口；当前 data-only head `20260830_0007` 只按显式 hard reservation 语义重算 scope overdrawn。
- Redis at-least-once 通知；Run、取消、重试、租约、Response、终态、治理、attempt ledger 和 audit 全由数据库裁决。
- 原子 claim、数据库时间 lease/heartbeat、fencing、有限 retry/backoff、取消、过期接管、duplicate no-op 和 dead-letter。
- 停写只读 SQLite→空 PostgreSQL 的单向 importer；`0005` 按依赖顺序复制 13 张应用表并做 count/PK/content fingerprint，源有 live Worker generation 时拒绝，stopped/stale progress 可精确复制。keyring 仍在数据库之外。
- managed API Run 的 active policy ID/hash 与显式 input/Token/cost override 冻结；global/provider/model/run 四层 concurrency、固定分钟 RPM/TPM、global/run lifetime request/Token/USD budget。
- 没有显式 `input_token_reservation` 时，观测 input 估算不再写成 hard reservation 或参与 reserved cost/input/cost overdraw；Provider actual usage 仍完整保存。显式 input、显式 `max_tokens` output，以及由完整上界和冻结价格派生的 reserved cost 超额继续 fail closed。
- 每个 Provider HTTP attempt 的 reserve→send-started→actual/conservative settlement 或 confirmed pre-send release never-delete ledger；materialized scope/bucket counter 仅作投影，任何高/低漂移 fail closed。
- 有限 backlog、typed `429`、database not-before、question quantum、dispatch/failure 分离和跨 Model due ordering。
- typed audit、分页 Run audit、task history counters、基于 Run 数据库时间戳的 queue/execution/end-to-end latency、严格规范化 Provider metadata 和非秘密 credential audit。
- 固定低基数 Prometheus text exporter、八条仓库内告警规则/Runbook、DB-time Worker generation/progress 聚合、canonical audit archive/离线 verify/reconcile/restore/delete 和全日志源治理；这些 P2-06 功能、clean Compose、实现 SHA 与证据文档 SHA 远程门禁均已完成。
- Run Detail 展示 managed/delayed/exhausted、治理原因和明确 UTC not-before；旧 Run 与可信本地 CLI 明确为 `legacy_unmanaged`。
- 真实 PostgreSQL 竞争测试及 Mock-only enhanced capacity/acceptance；精确实现 SHA 的完整 capacity、9/9 acceptance、`P2-local-control-plane-v2` 多轮单机资格与远程 4/4 CI 已通过。

## 非目标

- 不新增标准 Benchmark、代码沙箱、Judge、Arena、Agent、认证、多租户或公共部署。
- 不把本地 ledger/Response 幂等描述为 Provider 请求或账单 exactly-once。
- 不把 fixed-minute limiter 描述为平滑 token bucket，不把 Mock 容量描述为真实 Provider 或生产 SLA。
- 单机资格不交付生产 SLA；P2-06 也不部署 Prometheus/Alertmanager/OTel/通知发送器，不提供 WORM、自动 audit 删除、整库/keyring 备份恢复认证或当前容器 exact-generation health handoff。
- 不改变题目、评分分母、聚合、排行榜隔离或 protocol-v1 语义。

## 支持边界与不变量

- PostgreSQL 是受支持的多 Worker 事实来源；SQLite 只支持个人本地单 Worker。Redis、进程内计数和 materialized counters 都不是第二事实来源。
- API 先提交数据库 Run，再 best-effort `XADD`；通知失败不撤销已接受 Run。Worker 用数据库扫描覆盖 commit/XADD 裂缝、Redis 暂停和重复消息。
- 同一 Run 同时最多一个有效 owner/token；Response、进度、治理 settlement、取消和终态写入必须验证未过期 lease 与 fencing token。
- `(run_id, question_id)` 最多一条 Response；终态前从持久化 Response 重算 protocol-v1 聚合。
- managed Run 创建时冻结 policy/hash 与 override；运行中 policy 切换不能改变历史 Run。policy、Run override、scope/bucket projection 与 ledger 任一不一致都 fail closed，并只尝试写固定非秘密完整性事件。
- 只有已成功提交 `send_started` 或无法确认 send-start 结果的 attempt 消耗 HTTP retry。按 ADR-0011，confirmed pre-send release 保留终态旧 ledger row，并以新 generation 从当前未发送 ordinal 恢复。
- `send_started` 后 usage/commit 不确定按完整预留 conservative settlement；不能按零释放。Provider 响应后本地 commit 前崩溃仍可能造成重复外部调用/费用。
- cooperative yield 不增加 `failed_attempt_count`；dispatch/claim 与失败预算分离。
- API/Worker managed Run 受治理；按 ADR-0010，可信本地 `llmbenchlab-evaluate` CLI 继续 `legacy_unmanaged`、必须独占数据库，且没有全局 RPM/TPM/USD 硬保证。
- write-only `api_key`、AES-256-GCM `model_credentials`、数据库外共享 keyring、legacy `api_key_env`、origin/active-Run 门禁继续有效。Key、Authorization、ciphertext、nonce、keyring、Provider URL、题目/prompt/response正文不得进入 audit。
- Provider request ID/returned model/system fingerprint/finish reason 仅在固定字符、长度和凭据形态检查后保存；不安全值为 `null`，不生成含 Provider 控制文本的 redaction event。
- audit 是应用 append-only、event-key 幂等并有 hash/schema read validation，不是数据库管理员不可篡改的 WORM。
- OpenAI-compatible SSE、严格 `[DONE]`、JSON fallback、wire/event/content 上限和聚合后 Key 脱敏保持不变。

## 任务状态

| ID | 状态 | 已交付与剩余范围 |
| --- | --- | --- |
| P2-01 一致性与容量设计 | `completed` | ADR-0012～0014、DB truth/lease/fencing/治理、v2 四 cell 多轮统计、恢复与连接模型已交付；clean SHA `b6a35fe…` 的 1+5 资格为 23/23、`qualified`；证据文档 commit `875f13a…` 已 push，精确 SHA CI 4/4 成功 |
| P2-02 PostgreSQL 迁移 | `slice_delivered` | 历史 `0002`～`0004` 与 12 表 importer 已通过精确 SHA 远程门禁；`9a20676…` 增加 `0005` / 13 表 importer、live Worker preflight 和 populated downgrade guard，clean Compose 与远程实现门禁已通过 |
| P2-03 Queue/Worker | `foundation_delivered` | Redis 通知、DB scan、claim、lease/heartbeat/fencing、ACK/no-op 已交付；`9a20676…` 增加 generation 级 DB-time scan/claim/lease-heartbeat/progress 与 stale 聚合，dependency probe 仍只表示 capability |
| P2-03 日常多 Worker入口维护 | `completed` | 本地 PostgreSQL 多进程与 Compose 默认双 Worker入口、SQLite fail-fast、fresh/watermark scan、all/running scale direction、五 gauges 与跨 Benchmark PG lease 回归已通过本地/远程门禁；不改变 P2-07 或 Phase 2 总状态 |
| P2-04 生命周期可靠性 | `foundation_delivered` | retry/backoff、取消、恢复、dead-letter、Response 幂等和三个确定性 DB crash-seam 场景已通过完整 Compose acceptance；Provider 外部副作用仍为 at-least-once |
| P2-05 并发治理 | `slice_delivered` | 四层 concurrency/RPM/TPM/lifetime budget、per-attempt ledger、backpressure、finite quantum、公平排序、counter 重算 fail-closed 与 ADR-0011 已实现；精确 SHA 的真实 PG/capacity/acceptance/CI 候选门禁已通过 |
| P2-05 observational overdraw 维护 | `completed` | ADR-0018 与 data-only `0007` 只重算 overdrawn 并保留 ledger/actual/Response/Run，active reservation 时拒绝；本地完整验证、当前库迁移和最终 SHA `cb00924…` 的 CI 4/4 均通过 |
| P2-06 可观测性 | `completed` | 固定 exporter/八规则、canonical retention CLI、Worker DB-time progress、`0005` / 13 表 importer、公共 retained-row 校验与全日志源治理已进入 clean commit `9a20676…`；clean capacity/9/9 acceptance、实现 CI 与 evidence-doc commit `ec29596…` 的 CI 4/4 均通过 |
| P2-07 验证与运维 | `planned` | [ADR-0016](../decisions/ADR-0016-postgresql-keyring-recovery-and-redis-rebuild.md)、[独立计划](../plans/2026-08-28-phase-2-recovery-operations.md) 与 [工作日志](../worklogs/2026-08-28-phase-2-recovery-operations.md) 已建立；功能尚未实现，后续从最小只读 verifier 开始 |

`slice_delivered` 表示该垂直切片及其候选门禁已交付，不表示整个阶段完成。Phase 2 必须保持 `in_progress`。

2026-08-30 的个人本地维护已从最新非空 SQLite 一致性备份恢复 1 个 Mock Model、1 个 Demo Benchmark、15 Questions、1 个 completed Run 和 15 Responses，并让组合开发启动器把三服务详细输出分流到私有 Git 忽略日志。该操作有 staging 迁移、共有列摘要、完整性/外键/head、真实本地启动与 API/Web 读取证据，但不包含 PostgreSQL/keyring、Redis 或告警恢复认证，不能计作 P2-07 实施或改变本阶段状态。

同日的 OpenCode Go `hy3` Run 又暴露 observational input estimate 被错误写成 hard reservation：7 个 attempt 全部 actual settlement，第七次 estimate/actual 为 59/75，而所有 hard policy/Run override 均为 `null`，四层 scope 却被标记 overdrawn。ADR-0018/`0007` 修正这一派生语义并把 UI 文案改为“实际用量曾被判定超过预留”；旧 ledger、actual usage、7 条 Response 和 failed/exhausted Run 终态不改写。本地完整门禁、当前 SQLite 迁移、最终 SHA `cb00924…` 的 real-Compose 9/9 与精确 SHA CI 4/4 均通过，仓库级闭环完成。

同日的 Run Detail 维护又修正了两个只读展示缺口：`error_questions` 继续只表示执行异常，页面改用 `completed_questions - correct_questions` 显示全部未得分并拆出普通答错；Responses API 追加分页无关的输入/输出已知 Token 小计和独立覆盖数，使精确 Run Token 为 `null` 时仍可显示明确不完整的证据。该维护不改 protocol-v1、数据库 schema、历史 Response/Run、治理 ledger 或 P2-07 范围；并行 Run/Responses 快照不一致时页面保守显示已知小计，不把旧值标成当前完整总量。

## 验收标准与当前结论

- [x] 可靠执行基础：API/Worker restart、真实 lease-owner `SIGKILL`、Redis stop/start、duplicate delivery、pending/running cancel 和 lease takeover 有历史真实 PostgreSQL/Redis/Compose 证据。
- [x] 本地幂等：重复通知不生成重复 Response/终态聚合；Provider 外部调用/费用明确不保证 exactly-once。
- [x] 治理实现：`0004`、四层 policy/ledger、managed Run freeze、typed backpressure、quantum/fair ordering、typed audit/history、Provider/credential evidence 已提交并 push。
- [x] 完整性实现：counter 低报/高报、policy hash/column 与 Run override 漂移在 repository/API/Worker/importer 边界 fail closed；confirmed pre-send release 不消耗零 HTTP retry。
- [x] **治理候选门禁**：精确 SHA `665244e095905083b606b8e98e946ed1a02dc0fc` 的真实 PostgreSQL integration、增强 capacity、9/9 acceptance、全量 lint/test/smoke/migration/Compose 与远程 CI 均通过。
- [x] **crash seam 验收**：`reserved`→send-start、`send_started`→settlement、Response commit→最终恢复三条 deterministic DB seam injection 在完整 Compose acceptance 通过；它们不冒充精确时刻 `SIGKILL`。
- [x] **远程实现门禁**：GitHub Actions run [`33099260233`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33099260233) 对精确实现 SHA 4/4 成功。
- [x] **P2-01 完成**：clean SHA `b6a35fef1dd069ebb54b69955058915c722aa34d` 从零完成 1 warm-up + 5 measured、23/23 SLO、逐轮 hard invariant/cleanup 与 `qualified` 容量模型；aggregate SHA-256 `a76d167b…d0d9`。证据文档 commit `875f13a253c40b7573d45c6287385e60f2bb8f04` 已普通 push，[GitHub Actions run `33150080341`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33150080341) 对该精确 SHA 4/4 成功。结论只适用于固定 Mock 单机 profile。
- [x] **P2-06 仓库级闭环完成**：clean implementation commit `9a20676dcf545040782f04c166205d0043345753` 已 push，clean capacity/9/9 acceptance 与 [run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) 4/4 通过；evidence-doc commit `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6` 的 [run `33165775037`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33165775037) 也精确 4/4 通过。
- [x] **0004 历史索引兼容修复完成**：schema-equivalent `20260829_0006`、仅允许三个已知索引缺失子集的可重入 SQLite preflight、PostgreSQL `0005` metadata 白名单控制流、重复 active/额外 drift 拒绝、真实失败备份副本升级及本地门禁均通过；实现 SHA [`8fb51b690ae6335b8ef93b3cbe54e039781fb173`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/8fb51b690ae6335b8ef93b3cbe54e039781fb173) 的 [run `33263405214`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33263405214) 4/4 成功。historical PG missing-index 分支仍仅有 Mock 回归，标准 CI 真实 PG 只覆盖 fresh canonical 分支。
- [x] **Observational overdraw 修复完成**：目标行为与 `0007` migration 已通过本地完整测试、双方言 migration 和当前个人 SQLite 验真；最终修正 SHA [`cb00924ea3ba3d01ce5bc322b7eabdae1345baf3`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/cb00924ea3ba3d01ce5bc322b7eabdae1345baf3) 的 [run `33271095910`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33271095910) 4/4 成功。
- [x] **Run Detail 指标维护完成**：API/UI 与零/全/部分/非对称 usage、页内错题拆分及并行快照回归已实现；本地 lint/test/smoke/build/config 和目标实页核对通过。实现 SHA [`0003e4291769a851005ba46c7e59b156a6b789eb`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/0003e4291769a851005ba46c7e59b156a6b789eb) 已 push，[PR #5](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/5) 的 [run `33286730109`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33286730109) 4/4 成功；不改变 P2-07 或 Phase 2 整体状态。
- [x] **日常多 Worker入口维护闭环**：离线启动器 `42 passed`，隔离 PostgreSQL 16 迁移到 head 后跨 Benchmark/唯一 lease 回归 `2 passed`，终审后的 fresh/watermark 与 exited-replica 回归、真实 Compose `2→1→2` gauges/cleanup 和完整本地门禁通过；implementation SHA `b06594c…` 的 run `33299883513` 4/4 成功。
- [ ] **P2-07 正式闭环未通过**：没有数据库+keyring backup/restore 认证、完整故障矩阵和告警处置演练。

## 已实际运行的中间证据

| 验证 | 实际结果 | 限制 |
| --- | --- | --- |
| `make lint` | 最新本地冻结树通过 | 同一实现 SHA 远程 lint/test 通过 |
| P2-01 冻结树 `make test` | 后端 `829 passed, 29 skipped`；前端 `38 passed` | v2 实现历史冻结树通过；当前 P2-06 全量见下方独立行 |
| P2-01 真实 PostgreSQL/Redis integration | `29/29 passed` | v2 实现历史冻结树通过；当前 P2-06 integration 见下方独立行 |
| `make smoke` | `1 passed, 7 deselected`，仅 Mock | 最新本地冻结树通过；未调用真实 Provider |
| 定向治理/API/Worker | 目标套件零失败；独立审计记录 `218 passed`；完整性边界集合 `18 passed` | 已由最终全量、真实 integration 与候选 evidence 补充 |
| SQLite/PostgreSQL Alembic | 隔离 SQLite 与临时 PostgreSQL 16 的 prepare/upgrade/downgrade/upgrade/check 通过 | 本地通过；精确实现 SHA 的远程 integration 亦通过 |
| Compose config | `docker compose config --quiet` exit 0 | 不等于服务/容量 acceptance |
| enhanced capacity | `665244e…` 上通过；evidence SHA-256 `40deadeb…0588` | 有限 policy、4×202/2×429、yield/fairness/fault/reconciliation；Mock-only 非 SLA |
| full Compose acceptance | `665244e…` 上 9/9；evidence SHA-256 `ab311665…ddec` | 三条 deterministic seam 与 cleanup 均通过 |
| 正式 v2 单机资格 | `b6a35fe…` 上 1+5、23/23；aggregate SHA-256 `a76d167b…d0d9` | 每轮 22/330/330/331、hard invariant 与 exact-project cleanup 通过；Mock-only 非生产 SLA |
| P2-01 远程 CI | run `33146681285` 4/4 | 精确 v2 实现 SHA 全绿；PR #2 已于 2026-08-28 合并 |
| 设计/计时修复远程 CI | SHA `1cd19c51ed309316047a18ed3b2a308647af495d`，run `33081854406`，4/4 | 不包含当前治理实现 |
| P2-06 lint/test | `make lint` 全绿（Ruff 152 files、ESLint、TS）；`make test` 后端 `916 passed, 33 skipped`、前端 `38 passed` | clean implementation commit 前冻结树通过；只用 Mock/Stub |
| P2-06 smoke/integration/migration/build/config | smoke `1 passed, 7 deselected`；临时 PG16/Redis7 migration/check 后 integration `33 passed, 0 skipped`；临时 SQLite head→`0001`→head/check、frontend build（2192 modules，保留 662.39 kB warning）、Compose config 全绿 | 默认用户 SQLite 未到 head且未擅自迁移；首次 integration cleanup 被安全策略拒绝且未启动容器，修正明确目标后通过；实现 SHA 的远程同类门禁已通过 |
| P2-06 规则门禁 | 临时 `prom/prometheus:v3.5.0` 容器中 `promtool check rules` 成功 | 八条规则全部通过；不表示仓库部署 Prometheus/Alertmanager |
| P2-06 dirty acceptance | 9/9；artifact `llmbenchlab-p2-11554c25ec2d/evidence.json`，SHA-256 `d5f058457dbc29875cbac4bc38345b810b5ed556ea538862d309116ceb629fde`，`dirty=true` | Worker `2/2/2/0/0`；`0005` populated 与 isolated `0004` refusal、两层空库往返、cleanup C/V/N empty 均通过 |
| P2-06 dirty capacity | 最新 artifact `llmbenchlab-p2-c6de062ab77e/evidence.json`，SHA-256 `4aeb8271dd81e8671fc287942839f8d06862140ea9a6bf1d7ee5660265aa8453` 通过 | 18 Runs/270 Responses/270 question executions/271 reservations/1229 audit；0 error/drift/duplicate/PEL/lag，expected Worker 2、cleanup C/V/N/image 0；offline Mock、非 SLO |
| P2-06 clean acceptance | `9a20676…` 上 9/9；artifact `llmbenchlab-p2-92e173eeee28/evidence.json`，SHA-256 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`，`dirty=false` | Worker `2/2/2/0/0`；两级 populated refusal、两层空库往返、cleanup C/V/N empty |
| P2-06 clean capacity | `9a20676…` 上通过；artifact `llmbenchlab-p2-ca5673061b0f/evidence.json`，SHA-256 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`，`dirty=false` | 1W/2W/burst `7.267474/12.962228/9.333604 q/s`；18/270/270/271/1230，0 question error/drift/duplicate/PEL/lag，expected Worker 2、cleanup C/V/N/image 0；offline Mock、非 SLO |
| P2-06 implementation remote gate | PR [#3](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/3)，[run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) | 精确 `9a20676dcf545040782f04c166205d0043345753`，四个必需 job 全 success |
| P2-06 补充 Ruff | 过宽 scripts 命令报告 93 条既有 modernization 告警；`--select E,F,I` 通过 | 保留首次结果，不扩大本切片清理范围 |
| P2-06 staged 技术/安全终审 | structured-extra High 与 Worker `__main__` logger Medium 已修复；76-file implementation index 为 0 Blocker/High/Medium；hydration/import integrity 目标集 `67 passed` | 已进入 clean implementation commit `9a20676…` |
| P2-06 evidence-doc remote gate | [run `33165775037`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33165775037) | 精确 `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6`，四个必需 job 全 success；P2-06 仓库级闭环完成 |
| 2026-08-29 DB compatibility repair | migration `52 passed`；完整 backend `927 passed, 33 skipped`、frontend `38 passed`；lint/smoke/config、真实失败备份副本与当前库 startup/check 全绿 | `8fb51b6…` 的 exact-SHA run `33263405214` 4/4；不改变 P2-07 planned 状态 |
| 2026-08-30 本地恢复/静默启动维护 | 启动器 `3 passed`；完整 backend `930 passed, 33 skipped`、frontend `38 passed`；lint/build/smoke/config、SQLite digest/quick/FK/head、真实 API/Web 读取通过 | 个人本地 Demo 数据恢复和开发 UX；`5075bdb…` 的 run `33265171953` 4/4 成功，且不是 P2-07 恢复认证 |
| 2026-08-30 多 Worker入口目标回归 | 启动器/fake Compose `42 passed`；隔离 PostgreSQL 16 在迁移到 `0007` 后跨 Benchmark/唯一 lease `2 passed`；终审后的隔离 Compose `2→1→2` 最终 gauges 分别收敛到 `2/2/2/0/0`、`1/1/1/0/0`、`2/2/2/0/0`，cleanup C/V/N/image tags=`0/0/0/0` | 首次空 PG 未迁移导致 fixture setup `UndefinedTable`，按真实部署顺序迁移后通过；fresh/watermark 与 exited-replica 两项终审问题已修复并复审为 0 Blocker/High/Medium；完整 backend `1003 passed, 35 skipped`、frontend `64 passed`，lint/Mock smoke/build/config/diff check 全绿；`b06594c…` run `33299883513` 4/4 成功 |
| 2026-08-30 observational overdraw 修复 | backend `946 passed, 33 skipped`；真实 PG+Redis integration `33 passed`；双方言 migration 往返/check、`make lint`、frontend `39 passed`/build、Mock smoke `1 passed`、本地 real-Compose `9/9`、Compose config 与当前库迁移验真通过 | 当前 SQLite head `0007`，scope `4→0`，7 Responses/7 ledger/407 input/599 output、13 表行数、quick/FK 保持；无真实 Provider；`cb00924…` run `33271095910` 4/4 成功 |

所有自动化模型行为只使用 Mock、MockTransport 或 stub；没有真实 Provider 或 API Key。

## 迁移与回滚边界

- importer 的 13 表依赖顺序包含原六张业务/凭据表、六张 governance/audit 表和 `worker_processes`；源必须停写且可读、不得有 live Worker generation，目标必须空，copy/对账在单一目标事务内完成。
- active policy/reservation、materialized/ledger 漂移和 schema fingerprint 不满足 preflight 时 fail closed。summary 只含 row count、PK/content digest，不输出业务内容或凭据材料。
- `0007` upgrade/downgrade 均在任何 flag 更新前拒绝 active reservation；它只重算 `governance_scopes.overdrawn`，不得用直接 UPDATE、删除 reservation 或改小 Provider actual usage 代替。
- keyring 不随数据库复制；恢复目标必须另行安全取得匹配 keyring。数据库与 keyring 同时泄漏可恢复 Provider Key，丢失 keyring 则 stored credential 不可恢复。
- exit `2` 为提交前回滚；exit `4` 为 COMMIT 结果未知；exit `3` 为提交已确认但后验证/输出失败。exit 3/4 禁止盲目重试。
- downgrade `0005` 前必须停止 Worker、保存所需 progress 并显式清空 `worker_processes`；migration 不会静默丢弃 process facts。进入 `0004` 后，仍必须停止 API/Worker、关闭 admission、对账 active reservation 并满足原六类 governance/audit guard；不得为代码回滚删除已结算 ledger/audit。

## 风险与剩余控制

| 风险 | 已有控制 | 剩余工作 |
| --- | --- | --- |
| 限额并发突破 | canonical scope、固定锁序、DB transaction、ledger 重算、真实 PG integration 与 v2 多轮资格 | 超出固定单机 profile 时重新测量并持续回归 |
| Provider 调用/费用重复 | send-start marker、保守结算、本地幂等及三条 crash seam acceptance | 外部 exactly-once 不可承诺 |
| 长 Run 饥饿 | finite quantum、due ordering、dispatch/failure 分离及 v2 每轮公平性硬门禁 | 更大规模或不同 Worker 拓扑需重新建模 |
| Worker 停滞不可见 | DB-time progress/liveness 聚合、exporter 与 `WorkerStalled` rule；dependency probe 保持 capability-only | 在 P2-07 演练告警处置/扩缩 |
| 审计增长/泄密 | 固定 allowlist、无正文/URL/Key、pagination、canonical archive/verify/精确 delete/restore | P2-07 验证异地存储与整库恢复边界 |
| 容量结论过度外推 | Mock-only、环境/config/evidence 指纹、1+5 多轮和明确支持 profile | 不冒充 Provider/生产 SLA；环境或 profile 变化必须重新资格 |
| 灾难恢复失败 | 13 表 importer、迁移 guard、audit archive 自身 restore、独立 keyring 边界 | PostgreSQL backup/restore、keyring 配对、Redis 重建与完整恢复演练 |

## 交付物与下一任务

已交付候选包括 `0004`、governance/audit 模型/repository、Adapter/Runner/Worker/API/UI、enhanced capacity/PG tests，以及 P2-01 v2 多轮资格。治理 SHA `665244e…` 已通过真实 integration/capacity/acceptance；SLO SHA `b6a35fe…` 已通过 23/23 本地资格与远程 4/4 CI。P2-06 implementation SHA `9a20676…` 与 evidence-doc SHA `ec29596…` 已分别通过精确 4/4 CI，仓库级闭环完成。ADR-0018 observational overdraw 维护也已在最终 SHA `cb00924…` 完成本地/当前库/远程闭环；后续才按 [NEXT_TASK.md](../NEXT_TASK.md) 开始 P2-07 最小只读 verifier。

## 状态

`in_progress`。P2-01、P2-06、observational overdraw 与 Run Detail 指标维护已完成，P2-05 主切片已交付。P2-07 状态为 `planned`，功能尚未实现，数据库+keyring backup/restore 和完整恢复演练仍缺失。不得把 Phase 2 标为 `completed`，不得宣称生产 HA、灾难恢复 SLA、无限横向扩展或 Provider exactly-once。
