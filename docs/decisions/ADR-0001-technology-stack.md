# ADR-0001：采用 Python/FastAPI 与 React/TypeScript 的模块化单体

- **Date**: 2026-08-24
- **Deciders**: LLMBenchLab maintainers
- **Scope**: Phase 0–1 MVP

## Status

Accepted

## Context

LLMBenchLab 需要由个人开发者在本地快速启动，同时完成模型注册、数据集导入、后台逐题评测、客观评分、结果持久化与前端展示。项目还要具备类型边界、自动 API 文档、测试、迁移和未来扩展接口，但本阶段不需要微服务、分布式 Worker 或 Kubernetes。

关键约束包括：

- 默认离线 Mock 流程和 CI 不能访问付费模型；
- OpenAI-compatible 调用、Evaluator 和 Dataset Loader 要彼此解耦；
- 后端长任务不能阻塞创建 Run 的 HTTP 请求；
- 前端需覆盖桌面和常见移动宽度，但不需要重量级设计系统；
- 单人维护成本优先于理论上的无限扩展能力。

## Decision

采用前后端分离、后端模块化单体的技术栈：

- 后端：Python、FastAPI、Pydantic、SQLAlchemy 2.x 风格、Alembic、httpx、pytest、Ruff；
- 前端：React、TypeScript、Vite、React Router、原生 CSS/CSS Modules、Vitest、ESLint；
- 数据库：默认 SQLite，选择理由与迁移边界见 ADR-0002；
- 执行：FastAPI 进程内的受控 `EvaluationRunner`，使用 Task Registry、semaphore 和持久化状态，不引入 Redis；
- 接口：版本化 JSON REST API `/api/v1`，OpenAPI 由 FastAPI Schema 生成；
- 部署：本地前后端可分别启动，Docker Compose 作为可选封装；
- 开源许可：仓库启动时无既有许可证，因此项目代码采用 MIT License；第三方 Benchmark 内容仍分别遵守 manifest 的 `license`；
- 架构边界：API、应用服务、Dataset Loader、Runner、ModelAdapter、Evaluator、Repository 明确分层。Adapter/Evaluator 通过 Registry 扩展，不在 Runner 中堆叠供应商或题型条件分支。

Python/React 是两个独立依赖域，各自拥有 lint、test 和 build 流程。API Schema 与 ORM Model 分离；数据库变化必须通过 Alembic。网络请求不放在数据库事务中。

## Alternatives

### Django + Django REST Framework

成熟且内置管理能力强，但本项目不需要后台管理站点或完整认证体系。对于以 Pydantic Schema、异步 httpx 和小型 REST API 为核心的 MVP，初始结构和约定更重。

### 全栈 TypeScript（Node.js + React）

可共享语言和部分类型，但 Benchmark/Evaluator 未来更接近 Python 的数据科学与机器学习生态。团队当前更重视 Python 侧评测扩展和 pytest 工具链。

### 服务端渲染框架

Next.js 等方案可减少独立部署单元，但 MVP 是本地工具，不依赖 SEO；模型调用和任务状态仍需可靠后端。独立 SPA 让 API 边界更清楚，也方便后续替换 UI。

### 从一开始拆分 API、Worker、数据库服务

能更早支持横向扩展，却会引入消息代理、部署、幂等和可观察性的额外负担，超出个人 MVP 范围。

## Consequences

### Positive

- FastAPI/Pydantic 提供清晰校验与可用的 OpenAPI，SQLAlchemy/Alembic 提供可迁移持久化。
- React/TypeScript 可用明确类型实现轮询、表格和响应式页面。
- 模块化单体保持一次本地启动的低门槛，同时为 Adapter、Evaluator 和未来 Worker 留出接口。
- Mock adapter 可以在同一业务链路中完成完全离线测试，减少测试与生产逻辑分叉。

### Negative

- 前后端有两套依赖和工具链，类型不能天然共享，需要维护 API Schema/TypeScript 类型一致性。
- 进程内任务在进程退出时丢失执行上下文，不能自动恢复，也不适合多 API Worker。
- Python 异步、同步数据库会话和后台任务的边界需要谨慎处理，避免阻塞事件循环或跨任务共享 Session。

### Follow-up

- Phase 2 在保留 Runner/Adapter/Evaluator 接口的前提下引入 PostgreSQL、可靠队列与独立 Worker。
- 若新增生产依赖或改变容器边界，先新增 ADR，并同步架构、部署与测试文档。
