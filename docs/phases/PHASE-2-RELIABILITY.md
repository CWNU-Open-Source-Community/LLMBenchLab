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

## 阶段目标

把单进程 SQLite MVP 升级为以 PostgreSQL 为共享事实来源、Redis Streams 为可丢失/可重复通知层、独立 Worker 为唯一常规执行入口的可靠任务系统。在数据库时间、租约/fencing、逐题幂等、可恢复治理和 typed audit 保护下维持 `llmbenchlab-protocol-v1` 证据。本阶段不承诺 Provider exactly-once、生产 HA、无限横向扩展；已完成的 Mock-only 单机资格也不是生产或真实 Provider SLA。

## 当前功能范围

- PostgreSQL 多 Worker 目标、SQLite 单 Worker 兼容、双方言 Alembic 链至 `20260827_0004`。
- Redis at-least-once 通知；Run、取消、重试、租约、Response、终态、治理、attempt ledger 和 audit 全由数据库裁决。
- 原子 claim、数据库时间 lease/heartbeat、fencing、有限 retry/backoff、取消、过期接管、duplicate no-op 和 dead-letter。
- 停写只读 SQLite→空 PostgreSQL 的单向 importer；`0004` 后按依赖顺序复制 12 张应用表，并做 count/PK/content fingerprint。keyring 仍在数据库之外。
- managed API Run 的 active policy ID/hash 与显式 input/Token/cost override 冻结；global/provider/model/run 四层 concurrency、固定分钟 RPM/TPM、global/run lifetime request/Token/USD budget。
- 每个 Provider HTTP attempt 的 reserve→send-started→actual/conservative settlement 或 confirmed pre-send release never-delete ledger；materialized scope/bucket counter 仅作投影，任何高/低漂移 fail closed。
- 有限 backlog、typed `429`、database not-before、question quantum、dispatch/failure 分离和跨 Model due ordering。
- typed audit、分页 Run audit、task history counters、基于 Run 数据库时间戳的 queue/execution/end-to-end latency、严格规范化 Provider metadata 和非秘密 credential audit。
- Run Detail 展示 managed/delayed/exhausted、治理原因和明确 UTC not-before；旧 Run 与可信本地 CLI 明确为 `legacy_unmanaged`。
- 真实 PostgreSQL 竞争测试及 Mock-only enhanced capacity/acceptance；精确实现 SHA 的完整 capacity、9/9 acceptance、`P2-local-control-plane-v2` 多轮单机资格与远程 4/4 CI 已通过。

## 非目标

- 不新增标准 Benchmark、代码沙箱、Judge、Arena、Agent、认证、多租户或公共部署。
- 不把本地 ledger/Response 幂等描述为 Provider 请求或账单 exactly-once。
- 不把 fixed-minute limiter 描述为平滑 token bucket，不把 Mock 容量描述为真实 Provider 或生产 SLA。
- 单机资格不交付生产 SLA、Prometheus/OTel Exporter、告警规则、audit retention archive、备份恢复认证或 Worker 主循环进展探针。
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
| P2-02 PostgreSQL 迁移 | `slice_delivered` | `0002`/`0003` 可靠性与凭据基础、`0004` 治理/审计 schema、12 表 importer 与双方言测试已实现；本地及精确 SHA 远程真实 PG migration/check/integration 已通过，无自动反向回迁 |
| P2-03 Queue/Worker | `foundation_delivered` | Redis 通知、DB scan、claim、lease/heartbeat/fencing、ACK/no-op 已交付；Worker 主循环 progress/liveness 事实仍未交付 |
| P2-04 生命周期可靠性 | `foundation_delivered` | retry/backoff、取消、恢复、dead-letter、Response 幂等和三个确定性 DB crash-seam 场景已通过完整 Compose acceptance；Provider 外部副作用仍为 at-least-once |
| P2-05 并发治理 | `slice_delivered` | 四层 concurrency/RPM/TPM/lifetime budget、per-attempt ledger、backpressure、finite quantum、公平排序、counter 重算 fail-closed 与 ADR-0011 已实现；精确 SHA 的真实 PG/capacity/acceptance/CI 候选门禁已通过 |
| P2-06 可观测性 | `slice_delivered` | DB gauges、typed audit/history、Run latency、Provider metadata、credential audit 和 UI 状态已实现；Exporter/告警、retention archive、Worker progress/liveness、全日志源治理仍未完成 |
| P2-07 验证与运维 | `slice_delivered` | enhanced capacity/PG tests、正式 v2 单机资格、Operations/Performance/Deployment 与精确 evidence 已交付；backup/restore、完整失败矩阵与告警响应仍未完成 |

`slice_delivered` 表示该垂直切片及其候选门禁已交付，不表示整个阶段完成。Phase 2 必须保持 `in_progress`。

## 验收标准与当前结论

- [x] 可靠执行基础：API/Worker restart、真实 lease-owner `SIGKILL`、Redis stop/start、duplicate delivery、pending/running cancel 和 lease takeover 有历史真实 PostgreSQL/Redis/Compose 证据。
- [x] 本地幂等：重复通知不生成重复 Response/终态聚合；Provider 外部调用/费用明确不保证 exactly-once。
- [x] 治理实现：`0004`、四层 policy/ledger、managed Run freeze、typed backpressure、quantum/fair ordering、typed audit/history、Provider/credential evidence 已提交并 push。
- [x] 完整性实现：counter 低报/高报、policy hash/column 与 Run override 漂移在 repository/API/Worker/importer 边界 fail closed；confirmed pre-send release 不消耗零 HTTP retry。
- [x] **治理候选门禁**：精确 SHA `665244e095905083b606b8e98e946ed1a02dc0fc` 的真实 PostgreSQL integration、增强 capacity、9/9 acceptance、全量 lint/test/smoke/migration/Compose 与远程 CI 均通过。
- [x] **crash seam 验收**：`reserved`→send-start、`send_started`→settlement、Response commit→最终恢复三条 deterministic DB seam injection 在完整 Compose acceptance 通过；它们不冒充精确时刻 `SIGKILL`。
- [x] **远程实现门禁**：GitHub Actions run [`33099260233`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33099260233) 对精确实现 SHA 4/4 成功。
- [x] **P2-01 完成**：clean SHA `b6a35fef1dd069ebb54b69955058915c722aa34d` 从零完成 1 warm-up + 5 measured、23/23 SLO、逐轮 hard invariant/cleanup 与 `qualified` 容量模型；aggregate SHA-256 `a76d167b…d0d9`。证据文档 commit `875f13a253c40b7573d45c6287385e60f2bb8f04` 已普通 push，[GitHub Actions run `33150080341`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33150080341) 对该精确 SHA 4/4 成功。结论只适用于固定 Mock 单机 profile。
- [ ] **P2-06 正式闭环未通过**：没有 exporter/告警、audit retention archive 或 Worker progress/liveness；现有 dependency probe 不能证明主循环正在推进。
- [ ] **P2-07 正式闭环未通过**：没有数据库+keyring backup/restore 认证、完整故障矩阵和告警处置演练。

## 已实际运行的中间证据

| 验证 | 实际结果 | 限制 |
| --- | --- | --- |
| `make lint` | 最新本地冻结树通过 | 同一实现 SHA 远程 lint/test 通过 |
| 最新本地 `make test` | 后端 `829 passed, 29 skipped`；前端 `38 passed` | v2 实现冻结树通过 |
| 最新真实 PostgreSQL/Redis integration | `29/29 passed` | 本地通过；同一实现 SHA 远程 integration 通过 |
| `make smoke` | `1 passed, 7 deselected`，仅 Mock | 最新本地冻结树通过；未调用真实 Provider |
| 定向治理/API/Worker | 目标套件零失败；独立审计记录 `218 passed`；完整性边界集合 `18 passed` | 已由最终全量、真实 integration 与候选 evidence 补充 |
| SQLite/PostgreSQL Alembic | 隔离 SQLite 与临时 PostgreSQL 16 的 prepare/upgrade/downgrade/upgrade/check 通过 | 本地通过；精确实现 SHA 的远程 integration 亦通过 |
| Compose config | `docker compose config --quiet` exit 0 | 不等于服务/容量 acceptance |
| enhanced capacity | `665244e…` 上通过；evidence SHA-256 `40deadeb…0588` | 有限 policy、4×202/2×429、yield/fairness/fault/reconciliation；Mock-only 非 SLA |
| full Compose acceptance | `665244e…` 上 9/9；evidence SHA-256 `ab311665…ddec` | 三条 deterministic seam 与 cleanup 均通过 |
| 正式 v2 单机资格 | `b6a35fe…` 上 1+5、23/23；aggregate SHA-256 `a76d167b…d0d9` | 每轮 22/330/330/331、hard invariant 与 exact-project cleanup 通过；Mock-only 非生产 SLA |
| 远程 CI | run `33146681285` 4/4 | 精确 v2 实现 SHA 全绿；PR #2 未合并 |
| 设计/计时修复远程 CI | SHA `1cd19c51ed309316047a18ed3b2a308647af495d`，run `33081854406`，4/4 | 不包含当前治理实现 |

所有自动化模型行为只使用 Mock、MockTransport 或 stub；没有真实 Provider 或 API Key。

## 迁移与回滚边界

- importer 的 12 表依赖顺序包含原六张业务/凭据表和六张 governance/audit 表；源必须停写且可读，目标必须空，copy/对账在单一目标事务内完成。
- active policy/reservation、materialized/ledger 漂移和 schema fingerprint 不满足 preflight 时 fail closed。summary 只含 row count、PK/content digest，不输出业务内容或凭据材料。
- keyring 不随数据库复制；恢复目标必须另行安全取得匹配 keyring。数据库与 keyring 同时泄漏可恢复 Provider Key，丢失 keyring 则 stored credential 不可恢复。
- exit `2` 为提交前回滚；exit `4` 为 COMMIT 结果未知；exit `3` 为提交已确认但后验证/输出失败。exit 3/4 禁止盲目重试。
- downgrade `0004` 前必须停止 API/Worker、关闭 admission、对账 active reservation，并确保六类新表和 managed active Run 满足 guard；不得为代码回滚删除已结算 ledger/audit。

## 风险与剩余控制

| 风险 | 已有控制 | 剩余工作 |
| --- | --- | --- |
| 限额并发突破 | canonical scope、固定锁序、DB transaction、ledger 重算、真实 PG integration 与 v2 多轮资格 | 超出固定单机 profile 时重新测量并持续回归 |
| Provider 调用/费用重复 | send-start marker、保守结算、本地幂等及三条 crash seam acceptance | 外部 exactly-once 不可承诺 |
| 长 Run 饥饿 | finite quantum、due ordering、dispatch/failure 分离及 v2 每轮公平性硬门禁 | 更大规模或不同 Worker 拓扑需重新建模 |
| Worker 停滞不可见 | dependency probe、typed current gauges | DB-time progress/liveness、exporter 与 alert |
| 审计增长/泄密 | 固定 allowlist、无正文/URL/Key、pagination | retention archive/restore 和 cardinality 运行边界 |
| 容量结论过度外推 | Mock-only、环境/config/evidence 指纹、1+5 多轮和明确支持 profile | 不冒充 Provider/生产 SLA；环境或 profile 变化必须重新资格 |
| 灾难恢复失败 | 12 表 importer、迁移 guard、独立 keyring 边界 | PostgreSQL backup/restore、keyring 配对和 audit archive 演练 |

## 交付物与下一任务

已交付候选包括 `0004`、governance/audit 模型/repository、Adapter/Runner/Worker/API/UI、enhanced capacity/PG tests，以及 P2-01 v2 多轮资格。治理 SHA `665244e…` 已通过真实 integration/capacity/acceptance；SLO SHA `b6a35fe…` 已通过 23/23 本地资格与远程 4/4 CI。下一步按 [NEXT_TASK.md](../NEXT_TASK.md) 继续 Exporter/告警、retention archive、Worker progress/liveness 和 backup/restore。

## 状态

`in_progress`。P2-01 已完成，P2-05 已交付，P2-06/P2-07 也已有治理/审计/容量切片和远程门禁，但 Exporter/告警/retention/backup/Worker-progress 等阶段验收仍缺失。不得把 Phase 2 标为 `completed`，不得宣称生产 HA、完整可观测性、灾难恢复 SLA、无限横向扩展或 Provider exactly-once。
