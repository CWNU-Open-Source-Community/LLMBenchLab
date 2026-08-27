# 下一任务：Phase 2 并发治理、审计与性能基线

> 建议开始时间：正式数据/真实 API 本地评测切片的阶段 commit 与远程门禁通过后
> 对应阶段：[Phase 2 — Reliability](phases/PHASE-2-RELIABILITY.md)
> 前置状态：Phase 0–1 `completed`；Phase 2 `in_progress`

## 背景

Phase 2 的可靠执行基础已经落地：PostgreSQL 是共享事实来源，Redis Streams 只提供可丢失、可重复的 at-least-once 通知，独立 Worker 通过数据库租约、心跳和 fencing token 执行 Run。API/Worker 重启、实际租约 owner 强杀、Redis 中断、取消、重复投递、迁移往返及 SQLite→PostgreSQL 导入均有真实本地证据，且没有改变 `llmbenchlab-protocol-v1` 的评分含义。

这还不是完整的 Phase 2。当前缺少 Provider/Model/Run 级并发与速率治理、预算硬边界、完整背压与公平调度；现有 `/tasks/metrics` 只是数据库当前事实 gauges，不是历史 counters、延迟分布或审计事件；也尚无可复核的容量/性能基线。因此 Phase 2 必须继续保持 `in_progress`。

用户在 2026-08-27 要求优先形成可用真实 API URL/Key 运行的完整客观题流程；[ADR-0006](decisions/ADR-0006-local-real-provider-evaluation.md) 已批准可信本地提前切片。仓库现有固定来源 MMLU-Pro/GPQA-Diamond、`prepare/run/resume/report` CLI、Provider 预检、有界 Runner 和完整终态报告，但这条路径只有确认前的 HTTP attempts 上界，没有全局 RPM/TPM 或金额硬预算，也不解决多 Worker 公平治理。因此它强化了继续执行本任务的必要性，而不是 P2-05 已完成的证据。

## 当前仓库事实

- 可靠性决策由 [ADR-0005](decisions/ADR-0005-durable-task-execution.md) 固定；数据库而非 Redis/进程内存裁决 Run、租约、attempt、取消和终态。
- 新 Run 由 API 先提交数据库，再 best-effort XADD；通知失败仍可由 Worker 的数据库 reconciliation 恢复。
- PostgreSQL 支持受限多 Worker；SQLite 只用于本地兼容和单 Worker 开发，不是多 Worker 部署目标。
- 本地 Response 由 `(run_id, question_id)` 唯一约束和租约 token 保证幂等；Provider 调用或计费不承诺 exactly-once。
- 应用日志已有请求/Run correlation 和脱敏 JSON，但只覆盖 LLMBenchLab 应用 logger；Worker probe 也只证明依赖能力，不证明主循环 liveness。
- 可信本地真实评测由 CLI 直接领取指定 Run；操作者必须停止连接同一数据库的常规 API/Worker。模型发现、canary 和正式题目请求都使用内存中的 Key，自动化仅使用 MockTransport/Mock。
- 已提交的可靠性基础 commits 为 `2be2392`、`3c975c7`、`2006d3f`、`b3289b1`、`103ab79`；详见当前工作日志与 Project Status。

## 目标

在不改变 API v1 与 `llmbenchlab-protocol-v1` 评分语义、不调用真实模型的前提下，完成 Phase 2 剩余的最小垂直切片：定义并实现可持久恢复的并发/速率/预算治理和确定性背压，补齐历史可观测 counters、延迟与审计事件，并用真实 PostgreSQL/Redis 的负载和故障实验形成容量/性能基线与 Runbook。

## 开始前必须完成

1. 阅读 `README.md`、`AGENTS.md`、`docs/PROJECT_STATUS.md`、`docs/ROADMAP.md`、`docs/phases/PHASE-2-RELIABILITY.md`、ADR-0005、现有 lease/Worker/queue/metrics 实现和本工作日志。
2. 检查 Git 状态，保护所有未提交工作；创建新的工作日志并列出阶段 commit、push 与远程 CI 边界。
3. 先写新的 ADR，再写实现。ADR 必须明确配额事实来源、预留/结算/释放、重试与恢复、时钟、原子性、过载响应、公平性、审计保留和回滚语义。
4. 不得把 Redis 的瞬时计数当成预算或任务事实来源；若使用 Redis 加速，必须有数据库可恢复裁决和故障语义。

## 范围

### P2-05：并发、速率、预算与背压

- 定义全局、Provider、Model 和 Run 层级的并发上限与优先级；说明限制的组合顺序和数据库事实字段。
- 为请求/Token/费用预算定义原子预留、实际结算、释放与超限语义；未知 usage/pricing 不得静默按零结算。
- 为 retry、租约接管、取消、dead-letter 和 Worker 崩溃定义不会永久占用额度、也不会重复结算本地证据的恢复路径。
- 增加确定性背压：明确哪些请求延迟、拒绝或保持 pending，使用稳定且文档化的 API 错误/状态；不得丢失已经提交的 Run。
- 增加有限公平调度，证明低流量 Model/Provider 不会被持续高流量来源无限饿死。

### P2-06：历史指标与审计

- 保留现有 DB gauges，同时增加可解释的历史 counters、队列/领取/重试/取消/dead-letter 事件和端到端/排队/执行延迟。
- 定义 append-only 审计事件的 schema、关联 ID、保留、脱敏和完整性边界；不得把普通应用日志冒充不可篡改审计。
- 让单个 Run 的 admission、排队、claim、heartbeat/recovery、题级结果、结算和终态可以用稳定标识串联。
- 为指标与审计增加重启、重复投递、Redis 故障和迁移测试，避免 counter double-count 或证据漂移。

### P2-07：容量、性能与 Runbook

- 在真实 PostgreSQL/Redis、至少两个独立 Worker 和纯 Mock Adapter 下建立可重复负载脚本。
- 记录硬件/容器资源、数据规模、并发、吞吐、p50/p95/p99 排队与完成延迟、错误/重试率、数据库与队列压力；结果必须可复核，不能写成生产 SLA。
- 验证过载、Worker 缩放、租约到期、Redis 中断和数据库恢复时的治理/审计一致性。
- 编写限流、预算告警、积压、dead-letter、commit outcome unknown、扩缩 Worker 和安全回滚 Runbook。

## 非目标

- 不接入或调用真实 OpenAI-compatible Provider，不要求真实 API Key，不产生付费调用。
- 不继续扩展标准 Benchmark，不新增 IFEval、代码沙箱、LLM Judge、Arena、Agent、鉴权、多租户、计费系统或公共部署。
- 不承诺 Kubernetes、多区域容灾、严格全局 exactly-once、无限水平扩展或生产 SLA。
- 不改变逐题 evaluator、总分分母、完成率、回答准确率、排行榜隔离或历史快照语义；必要的不兼容变化必须另起协议/API 版本。
- 不把本任务扩张为 Phase 3；ADR-0006 已交付的客观题切片保持冻结，Phase 2 未完成前不启动新的 Benchmark 产品范围。

## 验收标准

- [ ] 新 ADR 在代码前接受，覆盖额度事实来源、原子预留/结算/释放、背压、公平、恢复、审计和回滚。
- [ ] 在并发 API 提交和多 Worker 领取下，全局/Provider/Model/Run 上限都不会被突破；重启和租约接管后无永久占用。
- [ ] rate/budget 超限具有稳定状态与 API 语义；已提交 Run 不因 Redis 故障或背压丢失。
- [ ] retry、duplicate delivery、取消、dead-letter 和 commit-uncertain 场景不会重复结算本地 Token/费用证据。
- [ ] 公平性测试证明受限低流量来源在持续竞争下能在文档化边界内获得执行机会。
- [ ] gauges、历史 counters、延迟和 append-only 审计的边界清楚；重复投递/恢复不会 double-count。
- [ ] 单个 Run 可通过关联 ID 串联 admission、queue、claim/recovery、question evidence、settlement 和 terminal state。
- [ ] 真实 PostgreSQL/Redis 负载实验产出环境、命令、原始脱敏证据和容量基线；不冒充生产 SLA。
- [ ] `make lint`、`make test`、`make smoke`、双方言迁移、真实基础设施 integration 和全栈故障回归继续通过。
- [ ] 每个阶段 commit 已 push 到 `origin`，且该精确 SHA 的 GitHub Actions 必需 job 全部成功；失败时不得把阶段或 Phase 标记为完成。
- [ ] `llmbenchlab-protocol-v1` 固定回归通过；所有测试仅使用 Mock/Stub/故障注入，没有真实 Provider 调用。
- [ ] README、Architecture、API、Testing、Deployment、Security、Roadmap、Project Status、Changelog、Phase 2、Next Task 和新工作日志与证据一致。

只有 P2-05、P2-06、P2-07 的全部关键验收都通过，Phase 2 才可评估是否从 `in_progress` 改为 `completed`；任何关键项失败或未运行都必须保留为 `in_progress`。

## 必须运行并记录的证据

```bash
make lint
make test
make smoke
make phase2-acceptance
(cd backend && uv run alembic upgrade head)
(cd backend && uv run alembic check)
docker compose config --quiet
```

此外必须有：并发 admission/claim 压测、各层限额竞争、预算预留/结算/释放、Worker kill/lease takeover、Redis stop/start、取消与 dead-letter、duplicate delivery、counter/audit 重放、低流量公平性和容量基线。基础设施不可用时必须明确列出未运行项，不得用单元测试替代真实 PostgreSQL/Redis 证据。

## 风险

- 分布式配额若同时由数据库和 Redis 裁决会产生双重事实；ADR 必须指定唯一权威和可恢复缓存语义。
- 预算预留在 Worker 崩溃、usage 未知或 commit acknowledgement 丢失时可能泄漏或重复结算；需要显式状态机与对账。
- 粗粒度锁可保证正确但损害吞吐；性能优化不得先于原子性证明，也不得绕开 fencing。
- 公平调度可能与吞吐、优先级和 Provider 限流冲突；必须记录权衡与饥饿上界。
- 高基数指标或含题目/响应的审计会泄露敏感数据并推高存储；必须限制字段、脱敏与保留期。

## 可直接复制给 Codex 的任务指令

```text
请在 LLMBenchLab 仓库执行 docs/NEXT_TASK.md 定义的“Phase 2 并发治理、审计与性能基线”。开始前阅读所有指定文档、ADR-0005 和现有 Worker/lease/queue/metrics，检查 Git 状态并创建新工作日志。先写 ADR，明确数据库事实来源下的并发、速率、预算预留/结算/释放、背压、公平、审计、恢复与回滚语义，再按 P2-05、P2-06、P2-07 实施。不得改变 llmbenchlab-protocol-v1，不得调用真实模型，不得覆盖用户未提交工作，不得 force push。每个阶段必须执行独立 commit，push 到 `origin`，并等待该精确 SHA 的 GitHub Actions 必需 job 全部成功；CI 失败时修复后重新 commit/push，绿色前不得宣称阶段完成。必须用真实 PostgreSQL/Redis、多 Worker 并发、故障与负载证据验收；任何关键项未通过时保持 Phase 2 in_progress，并如实同步全部状态、运维、测试和工作日志文档。
```
