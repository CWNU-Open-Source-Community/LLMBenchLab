# 下一任务：完成 P2-06 证据文档 CI，再启动 P2-07

> 状态：`pending_on_docs_ci`；P2-06 实现、clean-SHA capacity/9/9 acceptance、push 与 implementation SHA CI 已完成，只待本次证据文档提交自身的精确 SHA CI
> 对应阶段：[Phase 2 — Reliability](phases/PHASE-2-RELIABILITY.md)
> 当前计划：[Phase 2 可观测性与审计保留](plans/2026-08-28-phase-2-observability-retention.md)
> 当前日志：[2026-08-28 P2-06 工作日志](worklogs/2026-08-28-phase-2-observability-retention.md)
> 决策基础：[ADR-0005](decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0011](decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)、[ADR-0015](decisions/ADR-0015-observability-worker-progress-audit-retention.md)

## 当前事实

P2-01 已完成，不再重复资格或“重跑碰绿”：实现 SHA `b6a35fef1dd069ebb54b69955058915c722aa34d` 的 GitHub Actions [run `33146681285`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 4/4 成功，clean SHA 上的 `P2-local-control-plane-v2` 完成 1 warm-up + 5 measured、23/23 SLO，aggregate SHA-256 为 `a76d167bb664e2ee3ee7514c39ac738b76cef37776d7b66e1175a8596329d0d9`；证据文档 commit `875f13a253c40b7573d45c6287385e60f2bb8f04` 的 [run `33150080341`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33150080341) 也已 4/4 成功。这些结果只属于对应历史 SHA，不能替代当前 P2-06 证据文档门禁。

当前 P2-06 已按 [ADR-0015](decisions/ADR-0015-observability-worker-progress-audit-retention.md) 实现：

- Alembic head `20260828_0005` 新增 `worker_processes` 和 bounded audit scan indexes；SQLite→PostgreSQL importer 扩展为 13 表精确 digest，拒绝 live generation 并复制 stopped/stale facts；表非空时 `0005 -> 0004` 在 DDL 前拒绝。
- 长运行 Worker 注册唯一 generation，只在真实 scan/claim/lease-heartbeat/progress 后按 DB UTC 合并刷新；`/tasks/metrics` 和 exporter 只公开 expected/registered/live/stalled/shortfall 与聚合时间。dependency probe 固定声明不检查 main-loop progress。
- `GET /api/v1/metrics/prometheus` 输出固定 Prometheus text `0.0.4` gauge，使用一个 DB-time 快照、有界 15 分钟 audit 与 1 小时 latency 窗口、固定 enum label、整次 fail-closed 和每 API 进程 single-flight。
- `deploy/observability/` 提供固定八条告警规则、抓取示例与对应 Operations Runbook；仓库不部署 Prometheus、Alertmanager、OTel 或通知发送器。
- `llmbenchlab-audit-retention archive|verify|reconcile|restore|delete` 提供 canonical JSONL v1、严格文件/权限/大小/schema/hash/rollup 校验、真正离线 verify、精确 digest 绑定、默认不删除和 commit-outcome 分类。它不提供签名/WORM，也不替代整库+keyring backup。
- 全生产日志调用受字面量/无格式参数静态门禁；第三方动态消息固定化、结构化数值有限化，raw Uvicorn access handler 关闭。

当前验证事实：合并定向套件全绿；`make lint` 全绿（Ruff 152 files、ESLint、TypeScript typecheck）；`make test` 后端 `916 passed, 33 skipped`、前端 `38 passed`；`make smoke` 为 `1 passed, 7 deselected`；临时 PostgreSQL 16/Redis 7 integration 为 `33 passed, 0 skipped`，隔离 migration、frontend build、Compose config 与 Prometheus v3.5.0 八规则校验均通过。Clean acceptance `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-92e173eeee28/evidence.json`（SHA-256 `e4ffb8668fd3fa62d59b5d83f5c29eede35b327d88e6099345acd5950670fc47`）在 implementation SHA `9a20676dcf545040782f04c166205d0043345753` 上 `dirty=false`、9/9，Worker `2/2/2/0/0`、两级 populated refusal、两层空库往返与 cleanup C/V/N empty。Clean capacity `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-ca5673061b0f/evidence.json`（SHA-256 `2382f9138f09028f269d76c341b236dd4089d678c8a2323582045fac2b4f5039`）同样 `dirty=false`：1W/2W/burst `7.267474/12.962228/9.333604 q/s`，18 Runs/270 Responses/270 QuestionExecutions/271 reservations/1230 audit，0 question error/drift/duplicate/PEL/lag；expected Worker=2、stalled/shortfall=0，故障恢复后的瞬时 registered/live=3，cleanup C/V/N/image=0。该结果是 offline Mock 非 SLO。structured-extra High 与 Worker `__main__` logger Medium 均已修复，最终技术/安全复核为 0 Blocker/High/Medium。实现提交已 push，PR [#3](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/3) 的 [run `33164609388`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33164609388) 精确绑定该 SHA并4/4 success。默认用户 SQLite 尚未在 head且未擅自迁移；证据文档 SHA CI 绿色前 P2-06 仍不能写成 `completed`。

## 立即执行：P2-06 证据文档收尾

1. 复核本次证据文档 diff，确保 clean 与历史 dirty 结果不混用；capacity 的 `1 failed attempt` / `1 queue-notification error` 是注入故障的预期对账，不得笼统写成零，acceptance 也不得误写成清理了 build image。
2. 检查 Markdown 链接、`git diff --check`、staged diff、秘密/凭据/raw evidence/本机路径/生成物；只提交文档，不修改实现或 evidence artifact。
3. 形成独立 evidence-doc commit，普通 push 当前工作分支，通过 PR #3 触发 CI。
4. 等待该**文档提交精确 SHA** 的四个必需 GitHub Actions job 全部 success。失败时读取日志、修复并对新 SHA 重跑；不得用实现 SHA 的绿色替代。
5. 文档 SHA 绿色后，另做状态收尾提交：把 P2-06 标为 `completed`、计划步骤 6 标完成、NEXT_TASK 转为 P2-07；Phase 2 继续 `in_progress`。状态提交也须 push 并等待自身精确 SHA CI。

## P2-07：下一独立切片

P2-07 当前为 `not_started`。P2-06 文档门禁全绿后，新建独立计划、工作日志和必要 ADR，完成：

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

- P2-06：功能、文档、迁移与测试合同一致；全量本地/真实基础设施门禁通过；实现与证据文档各有独立 commit 并 push；对应精确 SHA 的四个必需 CI job 均全绿；状态/计划/日志记录完整且无秘密或生成归档。
- Phase 2：只有 P2-06 上述门禁和 P2-07 backup/restore、Redis 重建、告警处置与完整故障矩阵均有可复核证据后，才可评估 `completed`。
- 任一关键项未运行、失败或未形成精确 SHA 远程绿色时，相关任务不得标记 `completed`；当前 P2-06 保持 `pending_on_docs_ci`，Phase 2 保持 `in_progress`，且不得宣称生产 HA、灾难恢复 SLA、无限横向扩展、WORM 或 Provider exactly-once。

## 可直接复制给 Codex 的任务指令

```text
继续执行 docs/NEXT_TASK.md。P2-06 implementation SHA 9a20676dcf545040782f04c166205d0043345753 的 clean capacity/9/9 acceptance 与 GitHub Actions run 33164609388 4/4 已通过；不要重跑碰绿，也不要提前启动 P2-07。现在复核并提交 clean evidence 文档，普通 push 当前工作分支并等待该文档精确 SHA 的四个 CI job 全绿。绿色后再做状态收尾提交，把 P2-06 标 completed、NEXT_TASK 转入新建 P2-07 计划；Phase 2 仍保持 in_progress。自动化只用 Mock/Stub，禁止公开 raw evidence 或真实凭据。
```
