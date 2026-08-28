# 下一任务：完成 P2-06 仓库门禁，再推进 P2-07 恢复闭环

> 状态：`in_progress`；P2-06 功能、主要本地门禁、dirty capacity/9/9 acceptance 与修复后 staged 技术/安全终审已完成，独立 commit/clean-SHA Compose/push 与精确 SHA CI 尚未完成
> 对应阶段：[Phase 2 — Reliability](phases/PHASE-2-RELIABILITY.md)
> 当前计划：[Phase 2 可观测性与审计保留](plans/2026-08-28-phase-2-observability-retention.md)
> 当前日志：[2026-08-28 P2-06 工作日志](worklogs/2026-08-28-phase-2-observability-retention.md)
> 决策基础：[ADR-0005](decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0011](decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)、[ADR-0015](decisions/ADR-0015-observability-worker-progress-audit-retention.md)

## 当前事实

P2-01 已完成，不再重复资格或“重跑碰绿”：实现 SHA `b6a35fef1dd069ebb54b69955058915c722aa34d` 的 GitHub Actions [run `33146681285`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 4/4 成功，clean SHA 上的 `P2-local-control-plane-v2` 完成 1 warm-up + 5 measured、23/23 SLO，aggregate SHA-256 为 `a76d167bb664e2ee3ee7514c39ac738b76cef37776d7b66e1175a8596329d0d9`；证据文档 commit `875f13a253c40b7573d45c6287385e60f2bb8f04` 的 [run `33150080341`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33150080341) 也已 4/4 成功。这些结果只属于对应历史 SHA，不能替代当前 P2-06 工作树门禁。

当前 P2-06 已按 [ADR-0015](decisions/ADR-0015-observability-worker-progress-audit-retention.md) 实现：

- Alembic head `20260828_0005` 新增 `worker_processes` 和 bounded audit scan indexes；SQLite→PostgreSQL importer 扩展为 13 表精确 digest，拒绝 live generation 并复制 stopped/stale facts；表非空时 `0005 -> 0004` 在 DDL 前拒绝。
- 长运行 Worker 注册唯一 generation，只在真实 scan/claim/lease-heartbeat/progress 后按 DB UTC 合并刷新；`/tasks/metrics` 和 exporter 只公开 expected/registered/live/stalled/shortfall 与聚合时间。dependency probe 固定声明不检查 main-loop progress。
- `GET /api/v1/metrics/prometheus` 输出固定 Prometheus text `0.0.4` gauge，使用一个 DB-time 快照、有界 15 分钟 audit 与 1 小时 latency 窗口、固定 enum label、整次 fail-closed 和每 API 进程 single-flight。
- `deploy/observability/` 提供固定八条告警规则、抓取示例与对应 Operations Runbook；仓库不部署 Prometheus、Alertmanager、OTel 或通知发送器。
- `llmbenchlab-audit-retention archive|verify|reconcile|restore|delete` 提供 canonical JSONL v1、严格文件/权限/大小/schema/hash/rollup 校验、真正离线 verify、精确 digest 绑定、默认不删除和 commit-outcome 分类。它不提供签名/WORM，也不替代整库+keyring backup。
- 全生产日志调用受字面量/无格式参数静态门禁；第三方动态消息固定化、结构化数值有限化，raw Uvicorn access handler 关闭。

当前验证事实：合并定向套件全绿，并包含 importer committed-target integrity、retention no-op postverify、外部 logger extra、FIFO/line cap 与 PostgreSQL advisory/row-lock 终审回归；`make lint` 全绿（Ruff 152 files、ESLint、TypeScript typecheck）；`make test` 后端 `916 passed, 33 skipped`、前端 `38 passed`；`make smoke` 为 `1 passed, 7 deselected`；临时 PostgreSQL 16/Redis 7 migration/check 后 integration 为 `33 passed, 0 skipped`；临时 SQLite head→0001→head/check、frontend production build、`docker compose config --quiet` 与 `prom/prometheus:v3.5.0` 的八规则 `promtool check rules` 通过。Dirty Compose acceptance 已 9/9 通过，evidence 为 `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-11554c25ec2d/evidence.json`（SHA-256 `d5f058457dbc29875cbac4bc38345b810b5ed556ea538862d309116ceb629fde`，`dirty=true`），Worker gauges `2/2/2/0/0`，`0005` populated refusal、isolated populated `0004` refusal、两层空库往返和 cleanup C/V/N empty 均成功。最新 dirty capacity 也通过，evidence 为 `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-c6de062ab77e/evidence.json`（SHA-256 `4aeb8271dd81e8671fc287942839f8d06862140ea9a6bf1d7ee5660265aa8453`，`dirty=true`）：18 Runs/270 Responses/270 question executions/271 reservations/1229 audit，0 error/drift/duplicate/PEL/lag，Worker expected 2 且 cleanup C/V/N/image 全空；这是 offline Mock 非 SLO 观察值。此前代码终审为 0 Blocker/High/Medium；随后 staged 安全审查发现的 structured-extra 反射 High 与 `python -m app.worker` logger Medium 均已修复，最新 76-file staged 技术/安全复核重新收敛为 0 Blocker/High/Medium。最新 hydration/import integrity 目标集 `67 passed`。默认用户 SQLite 尚未在 head，直接 `alembic check` 失败后未擅自迁移。当前仍无 P2-06 独立 commit、clean-SHA Compose、远程 SHA 或 CI 链接；不得把 P2-06 写成 `completed`。

## 立即执行：P2-06 仓库级收尾

### 1. 完成实现终审

- 复核 Worker registration/stop/late-flush、scan/claim/progress/heartbeat event 定义，确认 timer 无事件时零写入、stale 边界 `last_seen_at == cutoff` 为 live。
- 复核 exporter cancellation/single-flight：请求取消后 DB 线程终止前不得提前释放 gate；audit 第 `50,001` 行必须整次 `503`，不能截断；数据库/audit/renderer 错误不得返回部分 exposition。
- 复核 archive 从同一已打开 descriptor 完成 verify/delete/restore，`verify` fresh process 在无效 DB 配置下仍不导入/创建 engine；delete/restore 只处理 archive 列出的精确完整事实，不能做宽 cutoff 删除。
- 复核 `0005`、prepare fingerprint、13 表 importer、Compose expected Worker count 与 acceptance/capacity 两 Worker 拓扑一致；终审确认 importer committed-target canonical integrity、retention no-op postverify 和 PostgreSQL advisory/row-lock 门禁继续生效。
- 检查所有生产 logger 调用、第三方 handler、Uvicorn access 和固定错误，确认不反射异常、DSN、路径或 argv marker。

### 2. 完成剩余验证

以下门禁已经执行并记录，无后续代码变化时不需要为碰绿重复运行：

```bash
make lint
make test
make smoke
(cd backend && uv run pytest -m integration)
(cd frontend && npm run build)
docker compose config --quiet
```

迁移验证使用隔离临时 SQLite 和 PostgreSQL；不要为文档收尾擅自 upgrade 默认用户 SQLite。当前剩余命令/检查是：

```bash
make phase2-capacity
# dirty acceptance 已通过；commit 后仍须在 clean SHA 重跑两项
make phase2-acceptance
git diff --check
```

- 自动化只能使用 Mock/MockTransport/stub，不调用真实或付费 Provider。
- `prom/prometheus:v3.5.0` 内的 `promtool check rules` 已验证八条规则成功；最终 diff 只需确认规则文件未再变化，若变化则必须重跑。
- 已完成的真实 PostgreSQL 路径覆盖 `0005` upgrade/check/空库往返、audit retention advisory/row lock 往返和 13 表 importer；dirty acceptance 已证明 application populated `0005 -> 0004` 拒绝、isolated populated `0004 -> 0003` 拒绝及两层空库往返。clean SHA 仍须重跑并绑定 evidence。
- 检查 Markdown 内部链接、固定八条 Runbook anchor、exposition metric 名、规则引用、文档版本和实际实现一致。

### 3. Commit、push 与精确 SHA CI

- 复核完整 diff/staged diff，排除真实 Key、Authorization/Cookie、credential DSN、ciphertext/nonce/keyring、archive 产物、调试输出和无关生成物。
- 修复后 staged 技术/安全终审和最新 dirty capacity 已通过；形成一个独立、可审查的 P2-06 commit，在该 clean SHA 上重跑 capacity/acceptance 并绑定 evidence，再普通 push 到 `origin/codex/complete-evaluation-workflow`，继续通过 PR #2 触发 CI；禁止 force push。
- 等待该**精确 commit SHA** 的四个必需 GitHub Actions job 全部成功。任何失败都读取日志、修复、形成新 commit/push，再等待新 SHA；历史绿色或仅本地通过不能替代。
- 绿色后再把 P2-06 状态改为 `completed`，并补齐 commit、branch、Actions URL、命令、测试数、跳过/未运行项、Compose cleanup、secret scan 与 `git status --short` 证据。Phase 2 仍因 P2-07 保持 `in_progress`。

## P2-07：下一独立切片

P2-06 精确 SHA 全绿后，新建独立计划、工作日志和必要 ADR，完成：

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

- P2-06：功能、文档、迁移与测试合同一致；全量本地/真实基础设施门禁通过；独立 commit 已 push；该精确 SHA 四个必需 CI job 全绿；状态/计划/日志记录完整且无秘密或生成归档。
- Phase 2：只有 P2-06 上述门禁和 P2-07 backup/restore、Redis 重建、告警处置与完整故障矩阵均有可复核证据后，才可评估 `completed`。
- 任一关键项未运行、失败或未形成精确 SHA 远程绿色时，相关任务和 Phase 2 必须保持 `in_progress`，不得宣称生产 HA、灾难恢复 SLA、无限横向扩展、WORM 或 Provider exactly-once。

## 可直接复制给 Codex 的任务指令

```text
继续执行 docs/NEXT_TASK.md。当前 P2-06 功能以及 lint/test/smoke/integration/migration/build/config/rules、dirty capacity/9/9 acceptance 和修复后 staged 技术/安全终审已通过，不要重做 P2-01，也不要跳到 P2-07。现在形成独立 commit，在 clean SHA 重跑 capacity/acceptance，普通 push 当前工作分支并等待该精确 SHA 的四个 CI job 全绿；失败就修复并对新 SHA 重跑。P2-06 绿色后再独立推进 PostgreSQL+keyring backup/restore、Redis 重建、告警响应和剩余恢复矩阵。自动化只用 Mock/Stub；Phase 2 在 P2-07 完成前保持 in_progress。
```
