# 2026-08-27 — Phase 2 并发治理、审计与性能基线工作日志

> 本日志随实施持续更新，只记录实际发生的事实；未运行的验证不得写成通过。

## 元信息

- 日期：2026-08-27
- 执行者：Codex
- 关联阶段：[Phase 2 — 可靠性与任务执行](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[Phase 2 并发治理、审计与性能基线执行计划](../plans/2026-08-27-phase-2-governance-audit-performance.md)
- 关联 ADR：[ADR-0009 — 数据库权威的执行治理、审计与公平调度](../decisions/ADR-0009-database-governance-audit-fair-scheduling.md)
- 当前状态：`in_progress`

## 目标与背景

Phase 2 的 PostgreSQL/Redis/租约可靠执行基础已经交付，但 P2-05 的跨 Run 并发、速率、预算、背压和公平调度尚未实现；P2-06 只有当前数据库 gauges 和应用日志，P2-07 也没有容量基线与完整 Runbook。本任务按 `docs/NEXT_TASK.md` 收敛这三个剩余切片，不扩展新的 Benchmark、Judge、Arena、Agent 或公共部署范围。

用户在本任务开始后先要求提交现有功能 PR。PR [#1](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/1) 已针对精确 SHA `ab15862eab4870dda01fb079b44b509a7d737627` 触发 CI；run `33078921254` 的后端、真实 PostgreSQL/Redis、真实 Compose 可靠性和前端四个必需 job 全部通过。随后才开始本任务的设计与实施。

## 初始仓库状态与保护边界

- 分支：`codex/complete-evaluation-workflow`，跟踪同名 origin 分支。
- 初始 HEAD：`ab15862eab4870dda01fb079b44b509a7d737627`。
- `git status --short --branch`：工作树干净，无 tracked/untracked 用户改动冲突。
- 必须保留的 Git 忽略状态：`.env`、`.secrets/`、本地 SQLite 数据库与 `artifacts/`；迁移、集成与压测只使用临时 DSN 或隔离 Compose project，不接触默认开发数据。
- 自动化只允许 Mock、Stub、MockTransport 和故障注入；不读取或调用真实 Provider Key。

## 范围

- 数据库权威的 global/provider/model/run 四层并发 permit、固定窗口速率、请求/Token/费用预留与结算。
- 对每个真实 Provider HTTP attempt 治理，包括 Adapter 内 retry；未知 usage/价格、进程崩溃和提交结果不确定不得按零释放。
- 确定性 backlog/backpressure 状态、稳定 API 字段/错误与恢复路径。
- 题目量子、协作让出和有限队列下的公平调度；失败预算与调度切片次数分离。
- append-only 应用审计事件、历史 counters、排队/执行/端到端延迟和 credential 非秘密事件。
- 真实 PostgreSQL/Redis、至少两个 Worker 的可复现容量/故障脚本、原始脱敏证据、基线和 Runbook。
- 双方言 migration、SQLite→PostgreSQL importer、API/前端提示、测试与所有联动文档。

## 非目标

- 不调用真实或付费 Provider，不实现 Provider exactly-once 或把本地 ledger 冒充账单。
- 不增加认证、多租户、KMS、Prometheus、分布式 tracing、Kubernetes、生产 HA 或 SLA。
- 不改变 `llmbenchlab-protocol-v1` 的题目、评分、总分分母、完成率或排行榜隔离。
- 不移除 Web write-only Key、AES-GCM credential、数据库外 keyring、legacy environment、真 SSE 或 nullable `max_tokens` 兼容路径。
- 不扩展 Phase 3 产品范围。

## 验收标准

- [x] ADR-0009 在任何实现代码前接受，并覆盖事实来源、原子性、恢复、背压、公平、审计和回滚。
- [ ] 并发 API 与多 Worker 下四层上限不突破；崩溃和租约接管后无永久 permit 占用。
- [ ] 每次 HTTP retry attempt 均有唯一预留/结算证据；重复交付和恢复不 double-count。
- [ ] 请求、Token 和费用硬限额可配置；未知 usage/价格和 `max_tokens=null` 按 ADR fail closed 或保守结算。
- [ ] backlog/rate/budget 具有稳定 API 语义，已提交 Run 保持可恢复且 Redis 故障不丢失。
- [ ] 持续高流量来源不能让低流量来源超过文档化量子/队列边界而无限饥饿。
- [ ] 历史 counters、p50/p95/p99 延迟和 append-only 审计可查询且不 double-count。
- [ ] credential 审计只含稳定非秘密标识，不含 Key、密文、nonce、Authorization 或 keyring 内容。
- [ ] 单 Run 可串联 admission、queue、claim/recovery、question reservation/evidence、settlement 和 terminal state。
- [ ] 真实 PostgreSQL/Redis 双 Worker 负载与故障实验留下环境、命令和脱敏原始证据，不宣称生产 SLA。
- [ ] 全量 lint/test/smoke/migration/integration/Compose/Phase 2 acceptance 通过，阶段 commits push 且精确 SHA CI 全绿。
- [ ] README 与 Architecture/API/Testing/Deployment/Security/Roadmap/Status/Phase/NEXT_TASK/Changelog 同步。

## 假设

- PostgreSQL 是受支持的多 Worker目标；SQLite 继续只支持单 Worker，但运行同一 schema 和状态机测试。
- 数据库时间裁决窗口、租约和 reservation 过期；进程单调时钟只用于本地等待。
- 硬 Token/费用预留要求 Run policy 冻结显式输入 reservation 与输出上限；任一缺失则在 Provider 调用前 fail closed，不能把 UTF-8 长度估计冒充任意 Provider tokenizer 的证明。
- 本地账本只能约束新的本地 admission；Provider 响应后、本地 commit 前崩溃仍可能产生重复外部计费。

## 风险

| 风险 | 影响 | 缓解措施 |
| --- | --- | --- |
| 四层锁顺序不一致 | PostgreSQL 死锁或限额突破 | 规范化 scope key，并始终按 global→provider→model→run 锁定 |
| Adapter retry 漏记 | RPM/预算低估 | 在 HTTP attempt 内部调用 hook，逐 attempt 使用唯一幂等键 |
| usage 未知或超出预留 | 错把费用按零或 ledger overdraw | 未知按完整预留结算；超额标记 overdrawn 并阻止后续 admission |
| Worker 崩溃遗留 active permit | 永久背压 | reservation 绑定 lease token，并联结当前 Run lease；失效 token 的 pre-send 释放、send-started 保守结算，并释放并发 |
| 长 Run 独占 Worker | 低流量来源饥饿 | 固定 question quantum、协作让出、有限 backlog 与最老服务时间排序 |
| 审计载荷泄密 | Key/响应内容持久化 | 固定事件 schema、字段 allowlist、opaque provider scope、不保存任意文本 |
| migration/导入遗漏新表 | 恢复或迁移证据漂移 | 0004 双方言往返、importer 全表 count/PK/content digest 和真实 PG 测试 |

## 实施步骤

1. [completed] 完成只读勘察，建立日志/计划并接受 ADR-0009。
2. [pending] 实现 0004 schema、治理 ledger/repository 与双方言/导入测试。
3. [pending] 接入 Adapter 每 attempt、Runner、lease/heartbeat/recovery 与公平调度。
4. [pending] 实现审计、历史 metrics、credential 事件、API 与前端诊断。
5. [pending] 建立双 Worker 容量脚本，运行负载/故障实验并编写 Runbook。
6. [pending] 执行全量门禁、秘密/diff 审查、文档同步、阶段 commit/push 和精确 SHA CI。

## 决定、偏差与发现

| 时间 | 类型 | 事实与影响 |
| --- | --- | --- |
| 2026-08-27 | delivery | 用户授权创建 PR #1；精确 SHA `ab15862…` 的四个 CI job 全绿，上一功能切片取得远程门禁证据。 |
| 2026-08-27 | discovery | OpenAI-compatible retry 位于 Adapter 内部；只包裹 `generate()` 会漏算每个 HTTP attempt，ADR 必须要求 attempt hook。 |
| 2026-08-27 | discovery | Worker 当前一次执行整个 Run；只改变 `due_run_ids()` 排序不能防止 12,032 题长 Run 独占，必须增加 question quantum。 |
| 2026-08-27 | decision | cooperative yield 不得消耗失败预算；新增失败计数并让既有 `attempt_count` 只记录领取次数，ADR-0009 明确替代 ADR-0005 对 attempt 上限的局部语义。 |
| 2026-08-27 | decision | Adapter 先持久化 `send_started` 再进入 HTTP；过期 `reserved` 可释放，`send_started` 后异常按完整预留结算。 |
| 2026-08-27 | decision | 旧 Run 不回填虚构 ledger/audit，也不在中途套新预算；缺治理快照者标记 `legacy_unmanaged` 后按旧边界收敛。 |
| 2026-08-27 | boundary | 数据库并发上限只约束受有效本地 lease 管理的 admission；崩溃后远端幽灵请求可能继续运行，不能宣称 Provider 侧 exactly bounded。 |

## 实际命令与结果

| 命令/检查 | 结果 |
| --- | --- |
| `git status --short --branch` | 初始工作树干净，分支与 origin 同步 |
| `gh pr create ...` | PR #1 创建成功；首次正文受 shell 反引号解释影响后立即用安全正文修正 |
| `gh pr checks 1 --watch --interval 10` | 精确 SHA `ab15862…` 的 4/4 必需 job 通过；CI run `33078921254` |
| README/AGENTS/PROJECT_STATUS/ROADMAP/Phase 2/NEXT_TASK/PLANS/ADR-0005/0007/0008 与相关代码只读勘察 | 已完成；未修改用户数据 |

## 实际修改、验证、已知问题与下一步

待实施后持续补充。当前仅进入设计阶段，不宣称 P2-05/P2-06/P2-07 或 Phase 2 完成。
