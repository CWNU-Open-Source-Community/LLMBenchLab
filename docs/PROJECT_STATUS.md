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
- FastAPI、SQLAlchemy 2.x、Alembic 与 SQLite 后端；五个核心实体、UTC 时间、约束、索引，以及由 `0000`、`0001` 组成的可回滚两-revision 线性迁移链。
- Alembic 是唯一运行时 schema owner；setup/migrate 可安全识别、备份并无损收养已知未版本化 SQLite，未知漂移在 stamp 前被拒绝。
- Model CRUD 与 Mock/OpenAI-compatible Adapter；Key 只通过 `api_key_env` 在运行时读取，错误有限重试并脱敏。
- 受限 ZIP/目录 Dataset Loader、严格 Schema/JSONL 校验、路径与压缩炸弹防护、稳定 SHA-256，以及 15 道原创 `demo-general`。
- Exact Match、Multiple Choice、Numeric Evaluator；原始输出、解析结果、评分和错误证据分离持久化。
- 进程内 Evaluation Runner：原子领取、1–4 低并发、单题故障隔离、逐题进度、协作式取消、汇总指标和重启遗留状态处理。
- 19 个版本化 API 路由：系统、模型、Benchmark、Run、逐题 Response、Leaderboard 与 Dashboard Metrics；OpenAPI 可用。
- React 中文界面：Dashboard、Models、Benchmarks、New Run、Run Detail、Leaderboard，含轮询、筛选、Demo 标识和响应式错误/空/加载状态。
- 统一 Make 命令、setup/dev/smoke 脚本、锁文件、GitHub Actions、双服务 Docker Compose 与持久 SQLite volume。

## 进行中功能

- Phase 1 已固定为基线 commit `3db1e29`。
- Phase 2 可靠任务执行基础已开始；[ADR-0005](decisions/ADR-0005-durable-task-execution.md) 已在实现前接受，代码、迁移和故障验收尚未完成。
- PostgreSQL/Redis/独立 Worker、租约/心跳/fencing、幂等恢复及 SQLite→PostgreSQL 对账正在按 [当前工作日志](worklogs/2026-08-25-phase-2-reliable-execution-foundation.md) 推进。

## 尚未完成的功能

- Phase 2：PostgreSQL、Redis、独立 Worker、持久恢复、租约/心跳、并发与限流、可观测性仍在实施；当前只有 ADR/计划完成，尚不能宣称运行能力。
- Phase 3：MMLU-Pro、GPQA、IFEval、数据集插件和隔离代码评测。
- Phase 4：LLM/Pairwise Judge、个人 Arena 与长上下文评测。
- Phase 5：Agent/Tool Use、私有 Benchmark 与 Live Benchmark。
- Phase 6：多用户、鉴权、公共部署安全和正式版本发布。

## 已知问题

- Runner 与任务去重只存在于单个 API 进程；异常退出后不会续跑，启动时会把遗留 `running` Run 标为 `failed`。
- SQLite 只面向个人低并发；不支持多实例写入协调、持久队列、租约、背压或高可用。
- 每个 Run 内并发上限为 4，但不同 Run 之间还没有全局调度上限、Provider 预算或背压。
- 取消在题目边界协作生效，已经发出的 Provider 请求可能继续至响应或超时。
- OpenAI-compatible `base_url` 只做基本 URL 校验，仍有 SSRF、DNS 重绑定与题目外发风险；MVP 不得直接暴露公网。
- 没有认证、授权、TLS、限流、预算上限或生产级秘密管理；Compose 仅用于本地验证。
- 只提供 15 道原创 Demo 与三个确定性 Evaluator；没有正式公共 Benchmark、代码沙箱、Judge、Arena 或 Agent 能力。
- 当前本地 `uv` 环境选择 Python 3.14，测试出现 `pytest-asyncio` 与 FastAPI TestClient 的上游弃用警告，但无失败；CI 固定 Python 3.12。
- 前端 production build 成功，但 Recharts 主包触发大于 500 kB 的 Vite chunk 警告；不影响 MVP 功能，后续可按页面懒加载。

## 测试状态

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 后端全量测试 | 通过 | `130 passed, 0 failed`；86 条上游/刻意漂移用例 warning |
| 前端组件/格式测试 | 通过 | `4 files, 13 passed, 0 failed` |
| 离线 Smoke | 通过 | `1 passed, 0 failed`；临时 SQLite + Mock |
| 后端 Ruff lint/format | 通过 | `ruff check` 与 `ruff format --check` |
| 前端 ESLint/typecheck | 通过 | `npm run lint` 与 `npm run typecheck` |
| 前端 production build | 通过 | Vite build 完成，存在非阻断 chunk-size 警告 |
| Alembic | 通过 | 临时 SQLite upgrade/check/downgrade/upgrade；19 项 clean/legacy/adoption/drift-rejection 回归；实际旧库 1/1/15/1/15 条记录无损迁移 |
| Docker | 通过 | backend/frontend images 构建；Compose 双服务 healthy；API、代理、healthz、SPA 均可达 |
| 配置静态检查 | 通过 | Compose、YAML、Shell、Action workflow 检查 |

所有自动测试均使用 Mock、MockTransport 或 stub fetch；没有调用真实 Provider，也不要求 API Key。详细命令和结果见工作日志与 [TESTING.md](TESTING.md)。

## 最近工作日志

[2026-08-25-phase-2-reliable-execution-foundation.md](worklogs/2026-08-25-phase-2-reliable-execution-foundation.md)（进行中：PostgreSQL、Redis、独立 Worker 与可靠恢复基础）

## 当前任务入口

[NEXT_TASK.md](NEXT_TASK.md) 已定义 Phase 2 的第一个可靠执行切片：PostgreSQL、Redis、独立 Worker、租约/幂等与重启恢复，同时保持 Phase 1 API 和 `llmbenchlab-protocol-v1` 语义兼容。
