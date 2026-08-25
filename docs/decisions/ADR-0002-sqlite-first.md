# ADR-0002：MVP 默认使用 SQLite，Phase 2 再迁移 PostgreSQL

- **Date**: 2026-08-24
- **Deciders**: LLMBenchLab maintainers
- **Scope**: Phase 1 本地持久化

## Status

Accepted

## Context

MVP 需要持久化 Model、Benchmark、Question、EvaluationRun 和 EvaluationResponse，并支持进度轮询、结果明细和排行榜。目标用户是本地单人开发者；快速安装、离线运行和可复制备份比高并发写吞吐更重要。

同时，Run 在后台逐题提交结果，SQLite 的单写者特性、多进程协调限制和锁等待必须被明确约束。项目未来需要可靠恢复、并发 Worker 和公共部署，因此数据访问不能与 SQLite 特有行为紧耦合。

## Decision

Phase 1 默认使用 SQLite 文件数据库，并遵循以下边界：

- `DATABASE_URL` 可配置；默认指向项目数据目录中的 SQLite 文件，测试使用临时文件数据库；
- 所有表结构变化使用 Alembic 迁移，禁止把 `create_all` 当作正式迁移机制；
- SQLAlchemy 2.x ORM/Repository 隔离数据库细节，业务层不拼接 SQLite 专有 SQL；
- 启用外键约束；可启用 WAL 和合理 `busy_timeout` 改善本地读写体验，但它们不被误解为分布式锁；
- 网络模型调用发生在事务外。每题结果与进度用短事务提交，Run 终态与聚合指标在一个事务内更新；
- MVP 官方拓扑是单个后端进程、低并发执行。不得用多个 API/Runner 进程共同消费同一 SQLite 文件；
- SQLite 文件通过 Compose Volume 持久化。备份时停止写入或使用 SQLite 一致性备份方式，不能只在写入中随意复制主文件；
- Phase 2 迁移到 PostgreSQL，并以迁移测试验证约束、JSON 字段、时间、Decimal 和查询语义。

SQLite 只是默认实现，不是领域协议。Run 唯一领取仍要通过状态条件更新和进程内去重实现，不能只依赖内存布尔值。

## Alternatives

### MVP 直接要求 PostgreSQL

提供更强并发、行级锁和生产路径，但要求每位本地用户安装或运行额外服务，削弱离线 Demo 和个人项目的启动体验。本阶段没有足够负载证明该成本合理。

### JSON/JSONL 文件持久化

实现初期简单，但关系完整性、并发更新、分页、聚合、迁移和崩溃一致性会迅速变复杂，也难以安全维护 Run 状态机。

### 内存数据库且不持久化

适合单元测试，不满足重启后查看历史记录、排行榜和可复现审计的核心目标。

### 同时正式支持 SQLite 与 PostgreSQL

会扩大 Phase 1 的迁移、CI 和查询兼容测试矩阵。当前仅要求代码避免不必要的 SQLite 锁定，正式 PostgreSQL 支持放入 Phase 2 验收。

## Consequences

### Positive

- 零额外服务即可运行，Mock Smoke Test 可完全离线。
- 单文件便于个人备份、重置测试和调试。
- SQLAlchemy/Alembic 保留清晰的 PostgreSQL 迁移路径。

### Negative

- 并发写入受限，较大 Benchmark 或多个并行 Run 可能遇到锁等待。
- 不能安全支撑多 API Worker、跨主机执行或高可用。
- SQLite 与 PostgreSQL 在 JSON、时间、Decimal、布尔和约束行为上存在差异，迁移需要专项测试。

### Operational consequences

- 文档必须把“单进程、可信本地环境”写为硬限制。
- 遇到 `database is locked` 不得无限重试或吞掉错误；Run 应进入可诊断的失败状态。
- 在 Phase 2 完成前，不宣称系统具备生产级任务恢复或水平扩展能力。
