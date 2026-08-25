# Phase 2：可靠性与任务执行

- 状态：`planned`
- 前置阶段：[Phase 1 — MVP](PHASE-1-MVP.md)（须 `completed`）
- 后续阶段：[Phase 3 — Benchmarks](PHASE-3-BENCHMARKS.md)

## 阶段目标

把单进程 SQLite 执行方式升级为基于 PostgreSQL、Redis 和独立 Worker 的可靠任务系统，使 Run 可恢复、可取消、可审计，并在有限并发下保持幂等和成本可控。

## 功能范围

- PostgreSQL 共享数据库、正式迁移与 SQLite 数据迁移工具。
- Redis 持久任务队列、独立 Worker、租约、心跳和死信处理。
- 幂等领取、重试、超时、取消、崩溃恢复和重复投递防护。
- Run、Model、Provider 级并发/速率/预算限制及背压。
- 结构化日志、关联 ID、指标、就绪检查和审计事件。
- 故障注入、竞争条件、迁移/回滚与性能基线测试。

## 非目标

- 不新增标准大型 Benchmark、代码沙箱、Judge、Arena 或 Agent。
- 不实现 Kubernetes、多区域容灾或无限水平扩展。
- 不改变 Phase 1 评分语义；不兼容变化必须升级协议或 API 版本。

## 依赖

- Phase 1 全部验收通过，核心模型、Runner 边界和离线 Smoke Test 稳定。
- 开发/CI 可启动 PostgreSQL 与 Redis。
- 已记录一致性、任务交付语义和数据迁移 ADR。

## 任务拆分

| ID | 任务 | 输出 |
| --- | --- | --- |
| P2-01 | 一致性与容量设计 | ADR、状态机、SLO/容量假设 |
| P2-02 | PostgreSQL 迁移 | Schema 迁移、SQLite 导入、回滚验证 |
| P2-03 | Queue/Worker | 持久队列、租约、心跳、幂等键 |
| P2-04 | 生命周期可靠性 | 重试、取消、超时、恢复、死信 |
| P2-05 | 并发治理 | Provider 限流、预算、背压、公平调度 |
| P2-06 | 可观测性 | 指标、结构化日志、关联 ID、审计事件 |
| P2-07 | 验证与运维 | 故障注入、迁移演练、基准、Runbook |

## 验收标准

- [ ] API/Worker 任一进程重启后，未完成 Run 可安全恢复。
- [ ] 重复投递不会生成重复逐题响应或重复计费记录。
- [ ] 同一 Run 只有一个有效执行者；租约失效与接管行为可测试。
- [ ] 取消、超时、有限重试和死信均有端到端测试。
- [ ] SQLite 数据可校验迁移到 PostgreSQL，迁移与回滚演练通过。
- [ ] 并发、速率和预算限制可配置，过载会背压。
- [ ] 单个 Run/Question 可通过关联 ID 在日志、指标和审计事件中追踪。
- [ ] Phase 1 API/协议兼容测试与离线 Smoke Test 继续通过。

## 风险

| 风险 | 应对 |
| --- | --- |
| at-least-once 产生重复写 | 幂等键、数据库唯一约束、事务状态转换 |
| Redis 与数据库状态分裂 | 数据库作为事实来源，队列消息可重建，提供对账任务 |
| 租约过短/过长造成重复或停滞 | 心跳与可配置租约，使用故障注入校准 |
| 并发导致上游限流和费用失控 | Provider 令牌桶、预算硬上限、熔断和背压 |
| 数据迁移丢失快照 | 迁移前备份、行数/Hash 对账、可回滚演练 |

## 交付物

- PostgreSQL 迁移、SQLite 导入/核验工具和回滚说明。
- Redis 队列、独立 Worker、恢复/取消/重试/死信模块。
- 并发与预算策略、可靠性测试、监控面板和操作手册。
- 更新后的 ADR、Architecture、Deployment、Testing 与 Security 文档。

## 状态

`planned`。Phase 1 完成前不得开始生产化迁移；预研结论可记录，但不改变本阶段状态。
