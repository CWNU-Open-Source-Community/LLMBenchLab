# 下一任务：启动 P2-07 恢复与运维闭环

> 状态：`ready`；P2-06 已完成仓库级闭环，下一步先建立 P2-07 独立计划、工作日志与必要 ADR
> 对应阶段：[Phase 2 — Reliability](phases/PHASE-2-RELIABILITY.md)
> 已完成计划：[Phase 2 可观测性与审计保留](plans/2026-08-28-phase-2-observability-retention.md)
> 已完成日志：[2026-08-28 P2-06 工作日志](worklogs/2026-08-28-phase-2-observability-retention.md)
> 决策基础：[ADR-0005](decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0011](decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)、[ADR-0015](decisions/ADR-0015-observability-worker-progress-audit-retention.md)

## 当前事实

P2-01 已完成，不再重复资格或“重跑碰绿”。P2-06 也已完成：implementation SHA `9a20676dcf545040782f04c166205d0043345753` 的 clean-SHA capacity/9/9 acceptance 与 [run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) 4/4 通过；evidence-doc commit `ec2959680459a14aa308bd4d9ebcc6bb7bfcf3a6` 的 [run `33165775037`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33165775037) 也精确 4/4 通过。

当前 P2-06 已按 [ADR-0015](decisions/ADR-0015-observability-worker-progress-audit-retention.md) 实现：

- Alembic head `20260828_0005` 新增 `worker_processes` 和 bounded audit scan indexes；SQLite→PostgreSQL importer 扩展为 13 表精确 digest，拒绝 live generation 并复制 stopped/stale facts；表非空时 `0005 -> 0004` 在 DDL 前拒绝。
- 长运行 Worker 注册唯一 generation，只在真实 scan/claim/lease-heartbeat/progress 后按 DB UTC 合并刷新；`/tasks/metrics` 和 exporter 只公开 expected/registered/live/stalled/shortfall 与聚合时间。dependency probe 固定声明不检查 main-loop progress。
- `GET /api/v1/metrics/prometheus` 输出固定 Prometheus text `0.0.4` gauge，使用一个 DB-time 快照、有界 15 分钟 audit 与 1 小时 latency 窗口、固定 enum label、整次 fail-closed 和每 API 进程 single-flight。
- `deploy/observability/` 提供固定八条告警规则、抓取示例与对应 Operations Runbook；仓库不部署 Prometheus、Alertmanager、OTel 或通知发送器。
- `llmbenchlab-audit-retention archive|verify|reconcile|restore|delete` 提供 canonical JSONL v1、严格文件/权限/大小/schema/hash/rollup 校验、真正离线 verify、精确 digest 绑定、默认不删除和 commit-outcome 分类。它不提供签名/WORM，也不替代整库+keyring backup。
- 全生产日志调用受字面量/无格式参数静态门禁；第三方动态消息固定化、结构化数值有限化，raw Uvicorn access handler 关闭。

P2-06 本地与 clean evidence 数值保持记录不变：合并定向、lint/test/smoke、双方言 migration、真实 PostgreSQL/Redis integration、frontend build、Compose config、Prometheus 规则、clean capacity/acceptance 与技术/安全终审均已通过；原始 evidence 仍不得公开。默认用户 SQLite 尚未在 head且未擅自迁移。

## 立即执行：建立 P2-07 工作包

1. 按 AGENTS/PLANS 先新建 P2-07 独立执行计划与工作日志，记录目标、非目标、验收、风险和实施步骤；不得复用 P2-06 日志冒充新任务记录。
2. 完整勘察现有 PostgreSQL backup/restore、keyring、Redis consumer group、告警 Runbook 与 Compose/CI 边界；若改变恢复协议或安全取舍，实施前新增 ADR。
3. 把 P2-07 拆成可独立验证的 backup/keyring、Redis rebuild、alert response 与故障矩阵步骤，先冻结恢复不变量和证据公开边界，再实施代码/脚本。
4. 自动化只使用 Mock/Stub；任何数据库/volume/keyring 删除或替换必须使用隔离、精确目标和 fail-closed guard，不触碰默认用户数据。

## P2-07：下一独立切片

P2-07 当前为 `ready`、尚未实现。新建独立计划、工作日志和必要 ADR 后，完成：

- PostgreSQL backup → 空目标 restore → Alembic `20260828_0005` head → 13 表 count/PK/content fingerprint → managed Run/ledger/audit/Worker stopped-or-stale facts 可读。
- 数据库与数据库外 keyring 独立备份/恢复：匹配 keyring 能解密，缺失/错误 keyring fail closed；日志/证据不得回显 Key 或 envelope。
- Redis 重建/consumer group 恢复、Worker 扩缩、八条告警响应、dead-letter、commit outcome unknown、governance integrity 与 remaining cancel/retry/lease/budget crash matrix 的真实 PostgreSQL/Redis 演练。
- audit archive 作为数据库恢复后的精确校验/补回工具参与演练，但不得把 archive 自身 restore 冒充整库、PITR、RPO/RTO 或 WORM 认证。

## 不变量与非目标

- 不改变 `llmbenchlab-protocol-v1` 的评分、分母、完成率、answered accuracy、排行榜隔离或不可变历史快照。
- API/Worker managed Run 继续以数据库/ledger 为事实来源；Redis、Prometheus、告警、日志、进程内 gate 和 materialized counter 都不是第二状态机。
- Provider 调用不是 exactly-once；本地幂等、ledger 和保守结算不能证明外部调用或账单恰好一次。
- 保留 write-only Key、AES-GCM credential、数据库外 keyring、legacy environment、真 SSE、严格 `[DONE]`、nullable `max_tokens` 和既有安全限制。
- 不新增 Phase 3 Benchmark、代码沙箱、Judge、Arena、Agent、认证、多租户、公共部署、Kubernetes 或生产 HA 声明。

## Definition of Done

- P2-06 已完成，不再重复其资格或证据门禁。
- P2-07：backup/restore、Redis 重建、告警处置与约定故障矩阵必须有隔离真实 PostgreSQL/Redis、Mock-only Compose、秘密审查、独立 commit/push 和精确 SHA CI 证据，才能标记 completed。
- Phase 2：只有 P2-07 也完成后才可评估 `completed`；在此之前保持 `in_progress`，不得宣称生产 HA、灾难恢复 SLA、无限横向扩展、WORM 或 Provider exactly-once。

## 可直接复制给 Codex 的任务指令

```text
继续执行 docs/NEXT_TASK.md。P2-06 已完成，不要重跑碰绿。现在按 AGENTS/PLANS 新建 P2-07 独立计划、工作日志和必要 ADR，先勘察并冻结 PostgreSQL+keyring 配对 backup/restore、Redis rebuild、八规则告警处置和剩余故障矩阵合同，再分步实现与验证。自动化只用 Mock/Stub，所有 destructive 操作只针对隔离、精确目标；Phase 2 在 P2-07 完成前保持 in_progress。
```
