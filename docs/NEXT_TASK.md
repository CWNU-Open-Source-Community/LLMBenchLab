# 下一任务：Phase 2 可观测性、保留与恢复闭环

> 状态：`ready`；治理/审计与 P2-01 已交付，Phase 2 仍保持 `in_progress`，现在可以开始 P2-06/P2-07
> 对应阶段：[Phase 2 — Reliability](phases/PHASE-2-RELIABILITY.md)
> 决策基础：[ADR-0005](decisions/ADR-0005-durable-task-execution.md)、[ADR-0009](decisions/ADR-0009-database-governance-audit-fair-scheduling.md)、[ADR-0010](decisions/ADR-0010-phase-2-governance-delivery-boundaries.md)、[ADR-0011](decisions/ADR-0011-confirmed-pre-send-release-retry-generation.md)、[ADR-0012](decisions/ADR-0012-single-host-slo-capacity-qualification.md)、[ADR-0013](decisions/ADR-0013-stable-image-content-fingerprint.md)、[ADR-0014](decisions/ADR-0014-dual-backlog-slo-profile.md)

## 现在从哪里继续

Phase 2 治理/审计垂直切片已经作为实现 SHA `665244e095905083b606b8e98e946ed1a02dc0fc` 提交并 push：Alembic `20260827_0004`、六类治理/审计表与 12 表 importer、global/provider/model/run 四层 per-attempt 治理、固定窗口 RPM/TPM 和 lifetime request/Token/USD budget、policy/hash 与 Run override 冻结、materialized counter/ledger 漂移 fail-closed、confirmed pre-send retry generation、有限 question quantum/backlog、公平排序、typed audit/history、Provider metadata、credential audit、前端治理状态与真实 PostgreSQL 竞争测试。P2-01 随后在独立实现 SHA `b6a35fef1dd069ebb54b69955058915c722aa34d` 交付 ADR-0012～0014、v2 四 cell 多轮资格、恢复/连接模型和 exact-project cleanup。

治理 SHA 的增强 capacity evidence SHA-256 为 `40deadebc357bbb24a07c91b05eb39f3d2fb7de11a28da9a7f95871c7acd0588`；完整 acceptance 9/9，evidence SHA-256 为 `ab311665ff0cb834efdd648cd634f943a4cbc5b8b00728ac8597a288a877ddec`。P2-01 冻结树的本地门禁为后端 `829 passed, 29 skipped`、前端 `38 passed`，lint、Mock smoke、前端 build、Alembic 与 Compose config 均通过；`P2-local-control-plane-v2` 在 clean SHA 上完成 1 warm-up + 恰好 5 measured、23/23 SLO 和逐轮 hard invariant/cleanup，aggregate SHA-256 为 `a76d167bb664e2ee3ee7514c39ac738b76cef37776d7b66e1175a8596329d0d9`。GitHub Actions [run `33146681285`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 对同一实现 SHA 4/4 成功；证据文档 commit `875f13a253c40b7573d45c6287385e60f2bb8f04` 也已 push，[run `33150080341`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33150080341) 对该精确 SHA 4/4 成功。P2-01 已完成，现在直接继续 P2-06/P2-07；不得重复候选或资格工作，也不得跳到 Phase 3。

## 已完成的候选门禁合同

以下合同已在 `665244e…` 上满足，保留作为后续修改必须重跑的回归基线。

### 1. 保护工作树并做定向回归

- 阅读 README、AGENTS、本文件、PROJECT_STATUS、ROADMAP、Phase 2、计划/工作日志及 ADR-0009/0010/0011。
- 运行 `git status --short --branch`，确认没有与用户工作重叠；不得回滚或格式化无关改动。
- 先运行治理 repository、Adapter、Runner、API/audit、credential、migration/importer、capacity-script 单测。
- 特别确认：scope/minute materialized counter 高报与低报均 fail closed；policy/hash 与 Run override 漂移不能绕过限额；`max_retries=0` 的 confirmed pre-send release 仍从未发送 ordinal 恢复。

### 2. 真实 PostgreSQL integration

在隔离 PostgreSQL 运行全部 integration marker，至少实际覆盖并记录：

- global/provider/model/run concurrency 竞争；
- 四层 RPM/TPM 原子限制；
- global/run lifetime request、Token、USD budget；
- 并发 backlog 精确 admission 数和 typed `run_backlog_full`；
- finish settlement 与 lease reconciliation 竞争只有一个终态事实；
- audit replay 幂等、同 key 不同 payload fail closed；
- active policy 并发切换、Run 创建/Model 敏感更新锁序；
- `0004` PostgreSQL upgrade/check/downgrade guard/upgrade 和 12 表 importer 对账。

不得用 SQLite 单测替代上述真实 PostgreSQL 证据；所有 Provider 行为仍只用 Mock/Stub。

### 3. 增强 capacity 与 acceptance

在同一个冻结候选 commit 上运行增强 `make phase2-capacity`：

- policy read-back 的所有 concurrency/RPM/TPM/lifetime 数值必须有限；
- Run input reservation、output limit、Token/cost budget 必须有显式上界；
- question quantum 必须小于 15 题 Demo Run，并由 durable audit 证明 cooperative yield；
- 停止 Worker 后并发提交必须得到精确 backlog-limit 个 `202` 和其余 typed `429`，随后恢复 Worker 并 drain 到零；
- 高流量 Model 持续竞争时，低流量 Model 必须在高流量 backlog 排空前取得 claim/slice；
- 比较一/二 Worker 的吞吐与 p50/p95/p99，并验证 Worker loss、Redis interruption、ledger/audit/counter 对账和完整清理；
- evidence 必须记录候选 commit SHA、脚本 SHA-256、环境、配置、原始脱敏计数和限制说明，不得冒充生产 SLA。

`phase2_acceptance.py` 的脚本单测为 `19 passed`，完整 `make phase2-acceptance` 已在冻结候选上验证以下三条确定性数据库 seam injection 与真实 Worker/Redis 恢复：

1. reservation 已提交、`send_started` 尚未提交：接管只能 `released_pre_send`，不得消耗未发送 retry；
2. `send_started` 已提交、settlement 尚未提交：接管必须 conservative settlement，释放并发但不按零退回预算；
3. Provider response 已返回、Response/settlement 本地 commit 尚未确认：本地唯一事实不得 double-count，并明确外部调用/费用可能重复的 at-least-once 边界。

这些场景明确是 deterministic database seam injection，不宣称 `SIGKILL` 精确命中亚毫秒边界；一般的“Worker 在若干 Response 后 SIGKILL”也不能替代它们。任一完整 Compose 场景失败或未运行都必须保留为未通过/遗留，Phase 2 继续 `in_progress`。

### 4. 全量门禁、commit、push 与精确 SHA CI（已完成）

至少运行并记录：

```bash
make lint
make test
make smoke
make phase2-capacity
make phase2-acceptance
(cd backend && uv run alembic upgrade head)
(cd backend && uv run alembic check)
docker compose config --quiet
git diff --check
```

- 检查 staged diff、调试残留、生成物、真实密钥/Authorization/Cookie、审计 payload 和文档虚假完成标记。
- 形成一个独立、可审查的 Phase 2 候选 commit，push 到 `origin/codex/complete-evaluation-workflow`，继续使用当前 PR #2；禁止 force push。
- 等待该精确 commit SHA 的四个 GitHub Actions 必需 job 全部成功。任何失败都读取日志、修复、创建新 commit、push 并等待新 SHA；不能用本地通过替代远程绿色。
- commit/SHA、branch、Actions URL、job 结论、capacity/acceptance artifact path 与 SHA-256、命令计数和清理结果已补入 Changelog、Project Status、Phase 2、计划与工作日志。

### 5. P2-01 单机控制面资格（已完成）

- 历史 v1 clean-SHA 1+5 aggregate 只通过 15/18，继续永久记录为 `failed/not_qualified`；未删除 measured-02，也未只重跑失败 cell。
- ADR-0014 在任何 v2 样本前冻结 warmed `3/5/8s` 与 cold `6/8/10s` 两个 AND backlog 门禁、两个 distinct validated Worker、22/330/330/331 对账和 project-image cleanup。
- clean SHA `b6a35fe…` 的全新 v2 invocation 保留 1 warm-up + 5 measured，23/23 SLO 全部通过，容量模型为 `qualified`。该结论只适用于 evidence 记录的 Mock-only 单机 profile，不是生产、真实 Provider、HA、费用或 exactly-once 证明。

## 当前工程任务：Phase 2 正式闭环

治理切片与 P2-01 已交付，证据文档 commit `875f13a…` 的精确 SHA CI 也已 4/4 成功。现在按 P2-06、P2-07 推进；下列范围全部完成后，Phase 2 才能评估是否改为 `completed`。

### P2-06：Exporter、告警、retention 与 Worker progress

- 为现有 DB gauges、typed counters 和 latency 提供受控 exporter；定义 label cardinality、抓取失败、数据库压力和脱敏边界。
- 为 backlog、dead-letter、governance integrity、overdraw、queue degraded、Worker stalled 和恢复时长定义告警规则、持续时间、严重度、静默与 Runbook 链接。
- 增加 Worker 主循环 progress/liveness 事实，例如最后 scan/claim/heartbeat/progress 的 DB-time 证据；不能继续把 dependency probe 冒充 Worker 正在推进。
- 定义 audit retention class 的实际保留、archive、删除/校验、恢复与失败语义；archive 不得含 Key、ciphertext、nonce、URL、题目/prompt/response正文，且不能宣称不可篡改存储。
- 若加入 resume canary 独立事件，先用固定安全 enum/bitset 设计 schema；不得写 Provider 控制文本。

### P2-07：备份/恢复与完整运维演练

- 演练 PostgreSQL backup→空目标 restore→Alembic head→12 表 count/PK/content fingerprint→managed Run/ledger/audit 可读。
- 数据库和 keyring 必须独立备份/恢复；既要验证匹配 keyring 可解密，也要验证缺失/错误 keyring fail closed，日志和证据不得回显 Key 或 envelope。
- 演练 audit archive restore、Redis 重建/consumer group 恢复、Worker 扩缩、dead-letter、commit outcome unknown 和治理完整性告警处置。
- 补齐尚未覆盖的取消/重试/租约/预算/崩溃组合矩阵，并在真实 PostgreSQL/Redis 下执行。

## 不变量与非目标

- 不改变 `llmbenchlab-protocol-v1` 的评分、分母、完成率、answered accuracy、排行榜隔离或不可变历史快照。
- 不调用真实或付费 Provider；自动化只使用 Mock、MockTransport、stub 和故障注入。
- API/Worker managed Run 受 `0004` 治理；可信本地 CLI 按 ADR-0010 继续 `legacy_unmanaged`，操作者必须独占数据库且没有全局 RPM/TPM/USD 硬保证。
- Provider 调用不是 exactly-once；本地幂等、ledger 和保守结算不能证明外部调用/账单恰好一次。
- 不把 Redis、进程内计数或 materialized counter 当成第二事实来源；ledger/DB truth 漂移必须 fail closed。
- 不把审计称为 WORM；数据库管理员仍可修改数据，读取完整性检查只提供应用层 fail-closed。
- 不移除 write-only Key、AES-GCM credential、数据库外 keyring、legacy environment、真 SSE、严格 `[DONE]`、nullable `max_tokens` 或既有安全限制。
- 不新增 Phase 3 Benchmark、代码沙箱、Judge、Arena、Agent、认证、多租户、公共部署或 Kubernetes。

## Definition of Done

- 已完成切片：治理/审计与 P2-01 已交付；P2-01 已有独立实现 commit、实际 evidence、secret/diff/cleanup 审计和实现 SHA 4/4 CI，证据文档 commit `875f13a…` 自身的精确 SHA CI 也已 4/4 成功；v1 失败与 v2 通过事实同时保留。
- Phase 2 整体：除上述切片外，Exporter/告警、audit retention archive、Worker progress/liveness、备份恢复和剩余故障矩阵均实现并验证；所有 Phase 2 验收项有可复核证据。
- 任一关键项未运行或失败时，Phase 2 必须保持 `in_progress`，不得宣称生产 HA、完整可观测性、灾难恢复 SLA、无限横向扩展或 Provider exactly-once。

## 可直接复制给 Codex 的任务指令

```text
请执行 docs/NEXT_TASK.md 的“Phase 2 可观测性、保留与恢复闭环”。治理/审计候选 `665244e095905083b606b8e98e946ed1a02dc0fc`、P2-01 v2 实现 `b6a35fef1dd069ebb54b69955058915c722aa34d` 与证据文档 `875f13a253c40b7573d45c6287385e60f2bb8f04` 的实际 evidence 和精确 SHA CI 已完成，不要重复实现、重跑碰绿或改写 v1/v2 证据。建立新的工作日志与执行计划，交付受控 exporter/告警、低基数标签、Worker DB-time progress/liveness、audit retention archive/restore；再演练 PostgreSQL 与数据库外 keyring 配对 backup/restore、Redis 重建和剩余故障矩阵。自动化只能用 Mock/Stub，不得调用真实 Provider；保持 protocol-v1、ledger/DB truth、write-only Key 和 fail-closed 边界。每个独立切片都须测试、提交、push 并等待精确 SHA CI；正式闭环未全部满足前 Phase 2 保持 in_progress，不得跳到 Phase 3 或宣称生产 HA/Provider exactly-once。
```
