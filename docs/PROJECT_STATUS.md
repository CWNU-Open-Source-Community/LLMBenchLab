# 项目状态

> 更新时间：2026-08-25（Asia/Shanghai）

## 当前阶段

- Phase 0 — 项目治理和架构：`completed`（2026-08-24）
- Phase 1 — MVP 垂直链路：`completed`（2026-08-25）
- Phase 2 — 可靠性与任务执行：`in_progress`（2026-08-25 开始）
- 后续阶段：Phase 3–6：`planned`

## 当前版本

`0.1.0` development baseline（尚未发布正式 Release），REST API 为 `/api/v1`，评测协议为 `llmbenchlab-protocol-v1`。

## 已完成功能

- 完整的 Charter、Requirements、Architecture、Benchmark Protocol、Dataset Format、Roadmap、Phase 0–6、ADR、治理规则和开源协作文件。
- FastAPI、SQLAlchemy 2.x 与 Alembic 后端；PostgreSQL 是 Compose/共享部署目标和任务事实来源，SQLite 保留单 Worker 本地兼容；五个核心实体、UTC 时间、约束、索引，以及 `0000 -> 0001 -> 0002` 线性迁移链。
- Alembic 是唯一 schema owner；Compose 只允许一次性 `migrate` 服务执行迁移，API/Worker 只检查 head。setup/migrate 仍可安全收养已知未版本化 SQLite，未知漂移在 stamp 前被拒绝。
- Model CRUD 与 Mock/OpenAI-compatible Adapter；Key 只通过 `api_key_env` 在运行时读取，错误有限重试并脱敏。
- 受限 ZIP/目录 Dataset Loader、严格 Schema/JSONL 校验、路径与压缩炸弹防护、稳定 SHA-256，以及 15 道原创 `demo-general`。
- Exact Match、Multiple Choice、Numeric Evaluator；原始输出、解析结果、评分和错误证据分离持久化。
- Phase 2 可靠执行基础：API 只提交数据库事实并 best-effort 发送 Redis Streams 通知；独立 Worker 以数据库扫描/领取、租约、心跳、单调 fencing token、逐题幂等和有限 attempt 执行 Run。
- 数据库裁决取消、重试/退避、租约过期接管、终态聚合和 dead-letter；Redis 是 at-least-once 通知层，不是状态数据库，通知丢失时可由数据库对账恢复。
- 22 个版本化 `/api/v1` 操作：liveness、health、readiness、任务 gauges、服务信息、模型、Benchmark、Run、逐题 Response、Leaderboard 与 Dashboard Metrics；OpenAPI 可用。
- React 中文界面：Dashboard、Models、Benchmarks、New Run、Run Detail、Leaderboard，含轮询、筛选、Demo 标识和响应式错误/空/加载状态。
- LLMBenchLab 应用 JSON 日志、请求/Run/Question correlation ID、`/live`、`/health`、`/ready`、数据库派生任务 gauges，以及数据库/队列依赖能力 Worker probe。
- PostgreSQL/SQLite `0002` migration 往返；显式 SQLite→PostgreSQL 单向导入器以只读源、空目标、单目标事务和五表 count/PK/content digest 做对账，并区分提交前回滚、COMMIT 结果未知与提交后验证失败。
- 统一 Make 命令、setup/dev/smoke/故障验收脚本、锁文件和 GitHub Actions；六服务本地 Compose 由 `postgres`、`redis`、一次性 `migrate`、`api`、`worker`、`frontend` 组成，API/frontend 只绑定 loopback，PostgreSQL/Redis 不发布宿主端口。

## 进行中功能

- Phase 1 已固定为基线 commit `3db1e29`。
- Phase 2 可靠任务执行基础已按 [ADR-0005](decisions/ADR-0005-durable-task-execution.md) 交付并经过真实 PostgreSQL/Redis 与进程故障验证；实现、验证和阶段边界记录在 [当前工作日志](worklogs/2026-08-25-phase-2-reliable-execution-foundation.md)。
- Phase 2 总状态仍为 `in_progress`：P2-05 尚未实施；P2-06 和 P2-07 只有部分交付，不能称为完整可观测、生产 HA 或容量已验证。

## 尚未完成的功能

- Phase 2 / P2-01：正式 SLO、容量模型和容量基线。
- Phase 2 / P2-05：Provider 速率限制、预算硬上限、完整背压、公平调度和全局并发治理。
- Phase 2 / P2-06：历史 counters、延迟/恢复时长、完整任务审计、全日志源脱敏治理、Worker 主事件循环 liveness 和告警；当前 `/tasks/metrics` 只是数据库 gauges，应用 JSON logger 不覆盖全部 Uvicorn/第三方日志。
- Phase 2 / P2-07：性能/容量测试、完整操作 Runbook、告警响应和更完整的备份/恢复演练。现有故障证据证明可靠基础行为，不证明生产高可用或无限横向扩展。
- Phase 3：MMLU-Pro、GPQA、IFEval、数据集插件和隔离代码评测。
- Phase 4：LLM/Pairwise Judge、个人 Arena 与长上下文评测。
- Phase 5：Agent/Tool Use、私有 Benchmark 与 Live Benchmark。
- Phase 6：多用户、鉴权、公共部署安全和正式版本发布。

## 已知问题

- SQLite 只面向个人本地、单 Worker 路径；多 Worker 竞争证据仅针对 PostgreSQL。当前 Compose 是可靠性开发/验收拓扑，不是生产 HA。
- 每个 Run 内并发上限为 4、每个 Worker 同时执行一个 Run，但不同 Run/Provider 之间没有完整全局限流、预算、背压或公平调度。
- 任务投递为 at-least-once，本地 Response 和聚合幂等，但 Provider 调用不是 exactly-once；Worker 在上游响应后、本地提交前崩溃可能造成重复请求或费用。
- 取消和失租会阻止后续题目与陈旧 Worker 写入，但已经发出的 Provider 请求可能继续至响应或超时。
- Redis 故障会让 API readiness 降级并增加调度延迟；数据库可继续提交/对账，但这不是 Redis 高可用保证。当前本地 Redis 无 ACL/TLS，只能位于隔离网络。
- Worker probe 只检查数据库/head/队列能力，不证明 Worker 主循环仍在领取、心跳或推进任务。API readiness 的 `asyncio.to_thread` 超时也不会取消底层同步数据库调用，最终上界取决于驱动/连接池 timeout。
- SQLite→PostgreSQL importer 会复制完整敏感评测内容且只支持空目标的单向导入；退出码 3/4 禁止盲目重试，工具不提供 PostgreSQL→SQLite 自动回迁。
- OpenAI-compatible `base_url` 只做基本 URL 校验，仍有 SSRF、DNS 重绑定与题目外发风险；MVP 不得直接暴露公网。
- 没有认证、授权、TLS、限流、预算上限或生产级秘密管理；Compose 仅用于本地验证。
- 只提供 15 道原创 Demo 与三个确定性 Evaluator；没有正式公共 Benchmark、代码沙箱、Judge、Arena 或 Agent 能力。
- 当前本地 `uv` 环境选择 Python 3.14，测试出现 `pytest-asyncio` 与 FastAPI TestClient 的上游弃用警告，但无失败；CI 固定 Python 3.12。
- 前端 production build 成功，但 Recharts 主包触发大于 500 kB 的 Vite chunk 警告；不影响 MVP 功能，后续可按页面懒加载。

## 测试状态

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 后端非集成测试 | 通过 | `205 passed, 5 deselected, 0 failed` |
| 真实基础设施集成 | 通过 | `5 passed, 205 deselected, 0 failed`；PostgreSQL 并发/取消、Redis PEL/ACK/重复投递、SQLite→PostgreSQL 导入 |
| 前端组件/格式测试 | 通过 | `4 files, 13 passed, 0 failed` |
| 离线 Smoke | 通过 | `1 passed, 0 failed`；临时 SQLite + 独立 WorkerService + Mock |
| 后端 Ruff lint/format | 通过 | `ruff check` 与 `ruff format --check` |
| 前端 ESLint/typecheck | 通过 | `npm run lint` 与 `npm run typecheck` |
| 前端 production build | 通过 | Vite build 完成，存在非阻断 chunk-size 警告 |
| Alembic / 数据导入 | 通过 | SQLite/真实 PostgreSQL upgrade/check/downgrade/upgrade；真实 PostgreSQL 16 导入成功、提交前回滚、双源竞争、COMMIT 结果未知和提交后验证失败路径通过 |
| Compose 故障验收 | 通过 | 默认 build 的隔离六服务拓扑完成 `8/8`：健康/协议基线、API restart、租约 owner `SIGKILL`/自然接管、Redis stop/start、两类取消/重复投递、PG migration 往返；清理后无项目容器/卷/网络残留 |
| 配置静态检查 | 通过 | Compose、YAML、Shell、Action workflow 与 diff 检查 |

所有模型相关自动化路径均使用 Mock、MockTransport 或 stub fetch；基础设施用例只连接隔离的 PostgreSQL/Redis，没有调用真实 Provider，也不要求 Provider API Key。详细命令和结果见工作日志与 [TESTING.md](TESTING.md)。

## 最近工作日志

[2026-08-25-phase-2-reliable-execution-foundation.md](worklogs/2026-08-25-phase-2-reliable-execution-foundation.md)（可靠执行基础实现、真实故障验证与剩余阶段边界）

## 当前任务入口

[NEXT_TASK.md](NEXT_TASK.md) 作为后续任务的唯一入口；在 Phase 2 保持 `in_progress` 的前提下，下一切片应优先收敛 P2-05，并为 P2-06/P2-07 的剩余验收留下可复核证据。
