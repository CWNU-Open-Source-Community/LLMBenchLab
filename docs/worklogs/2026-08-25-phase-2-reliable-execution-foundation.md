# 2026-08-25 — Phase 2 可靠任务执行基础

> 本日志记录实际执行过程、证据与偏差。所有命令默认从仓库根目录运行；任何未通过的关键验收都必须如实保留。

## 元信息

- 日期：2026-08-25
- 执行者：Codex
- 关联阶段：[Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- 任务入口：[NEXT_TASK.md](../NEXT_TASK.md)
- 关联计划：本日志“执行计划”章节
- 关联 ADR：[ADR-0005 — Durable task execution](../decisions/ADR-0005-durable-task-execution.md)
- 当前状态：in_progress

## 初始仓库状态

- 当前分支：`main`。
- 仓库尚无首个 commit；`git status --short --branch` 显示 `## No commits yet on main`，Phase 0/1 文件全部为未跟踪状态。
- 暂存区为空；未发现可与本任务区分的用户暂存修改。
- 现有工作属于用户的完整 Phase 1 基线，不得重置、覆盖或删除；按 `NEXT_TASK.md` 要求，先将该已验证基线单独提交，再开始 Phase 2 修改。
- 本任务不得 push，不得调用真实模型 Provider；测试只允许 Mock/故障注入和本地容器依赖。

## 目标与背景

把当前由 API 进程内 `EvaluationTaskManager` 执行的任务，推进为最小但真实可靠的持久化执行链：API 只持久化并通知任务，独立 Worker 从数据库竞争租约并执行；PostgreSQL 是部署目标数据库，Redis 提供可丢失后仍可恢复的 at-least-once 唤醒/投递，数据库始终是任务状态事实来源。系统必须通过条件更新、租约令牌、心跳、唯一响应和有限重试，在并发领取、进程重启、Redis 故障、取消及租约过期下保持 `llmbenchlab-protocol-v1` 的逐题证据和评分含义不变。

## 范围

- 在实现前新增 ADR，明确数据库、队列、Worker、事实来源、at-least-once、租约、心跳、幂等、恢复、失败终态及回滚语义。
- 增加 PostgreSQL 运行配置、Alembic 前进/回退迁移和 SQLite 本地兼容策略。
- 以独立 Worker 替代 API 进程内执行；API 提交 Run 后返回 `202`，不执行模型任务。
- 增加 Redis 队列/通知抽象，同时由数据库对待执行、可重试与租约过期任务进行兜底协调。
- 增加原子领取、租约 fencing、心跳、attempt、last error、有限重试、取消、过期接管和 dead-letter 证据。
- 让逐题响应写入和进度更新在重复投递下幂等，保持 `(run_id, question_id)` 唯一性和协议 v1 聚合口径。
- 增加结构化日志、关联 ID、存活/就绪检查和任务指标。
- 更新 Compose/CI，使 PostgreSQL、Redis、API、Worker、frontend 能完成健康/就绪启动。
- 用自动化与真实本地进程/容器故障场景验证 `NEXT_TASK.md` 要求，并同步所有指定文档。

## 非目标

- 不改变 `llmbenchlab-protocol-v1` 的题目集合、逐题计分、总分分母、完成率或排行榜含义。
- 不实现新的模型 Provider，不调用真实模型，不使用真实 API Key。
- 不承诺 Provider 侧恰好一次计费；进程在远端响应后、本地证据提交前崩溃时，at-least-once 恢复可能再次调用 Provider。测试中只使用 Mock。
- 不引入分布式多区域调度、完整 Kubernetes 编排或 Phase 3 认证/RBAC。
- 不覆盖或清理用户现有 SQLite 数据、备份、`.env` 或其他未提交内容。

## 验收标准

- [x] Phase 1 基线已作为独立首个 commit 固定；后续每个实施阶段仍须独立 commit，且没有 push。
- [x] ADR 在实现前落库，覆盖 PostgreSQL、Redis、独立 Worker、数据库事实来源、at-least-once、租约/心跳/fencing、幂等、恢复、失败和回滚。
- [ ] `POST /api/v1/runs` 返回 `202` 后 API 不执行任务；独立 Worker 可完成离线 Mock Run。
- [ ] 两个 Worker 并发竞争时只有一个有效租约；陈旧 Worker 的写入被 fencing 条件拒绝。
- [ ] Worker/API 重启不会丢失 Run；已提交逐题响应不会重复，租约过期可被接管。
- [ ] Redis 暂时不可用时有明确降级证据，数据库协调仍可恢复任务；恢复后队列可继续工作。
- [ ] 取消、任务异常、逐题异常、重试耗尽、dead-letter 和租约过期行为确定且有测试。
- [ ] PostgreSQL migration `upgrade -> downgrade -> upgrade`、`alembic check` 通过；SQLite 兼容/退出策略有测试和文档。
- [ ] `make lint`、`make test`、`make smoke` 以及既有 130 项后端、13 项前端回归不退化。
- [ ] Compose 中 PostgreSQL、Redis、API、Worker、frontend 可 `up --build --wait`，健康/就绪检查通过，测试数据可清理。
- [ ] README、Architecture、API、Testing、Deployment、Security、Roadmap、Project Status、Changelog、Next Task、Phase 2 与本日志反映真实证据。

## 假设

- PostgreSQL 是容器/部署目标；SQLite 保留为单机开发与自动化测试兼容路径，但也必须由 Alembic 管理。
- Redis 只保存可重复投递的唤醒消息；Run、租约、attempt、取消与最终证据全部由数据库裁决，因此 Redis 丢消息或短时不可用不会改变事实状态。
- Run 级租约足以支持当前一次 Run 内有限并发；逐题唯一约束和“跳过已有响应”恢复逻辑提供结果幂等。
- 旧 Run 快照继续按协议 v1 解读；执行恢复机制属于执行策略/审计信息，不改变同一问题响应的评分函数。
- 本机具备 Docker/Compose、uv、Node/npm，可执行真实 PostgreSQL/Redis 故障验证；若环境事实不符，必须记录未通过项而非伪造证据。

## 风险

| 风险 | 影响 | 计划缓解 | 当前结果 |
| --- | --- | --- | --- |
| 重复投递或租约接管后双写 | 重复响应、错误进度或聚合 | 唯一约束、条件更新、单调租约令牌、所有关键写入校验有效租约 | 待验证 |
| Worker 在 Provider 返回后、DB 提交前崩溃 | 可能重复远端调用/计费 | 明确 at-least-once 边界；本地证据幂等；只用 Mock 做故障测试 | 待验证 |
| Redis 故障阻塞创建或丢任务 | Run 永久 pending | 先提交数据库；队列 best-effort；Worker 定期从数据库 reconciliation | 待验证 |
| PostgreSQL 与 SQLite 锁/时间/DDL 语义不同 | 单机测试通过但部署失败 | 两种数据库专项测试；PostgreSQL 容器迁移往返和并发领取实测 | 待验证 |
| API 重启错误地终止 running Run | 破坏恢复语义 | 删除启动时“全部 running 标失败”；只由租约过期与重试策略恢复 | 待验证 |
| Phase 2 变更评分口径 | 历史不可比 | 冻结协议 v1；复用既有聚合与响应模型；增加协议回归断言 | 待验证 |
| 初始仓库无 commit | 无法区分基线与 Phase 2 | 工作日志先创建但不纳入基线；Phase 1 文件单独首提，之后阶段提交 | `3db1e29` 已固定基线 |

## 执行计划

- Owner: Codex
- Status: in_progress
- Created: 2026-08-25 11:44 CST
- Updated: 2026-08-25 12:02 CST

### 实施步骤与提交边界

1. [completed] 完成文档、Runner、迁移、测试和基础设施审计；创建本日志；提交不含本日志的 Phase 1 基线。
2. [completed] 编写 ADR，并把 Phase 2/Project Status 标为真实进行中；审查后提交文档阶段。
3. [in_progress] 实现可靠执行字段、PostgreSQL/SQLite Alembic 迁移、原子租约与幂等持久化；迁移和持久化门禁通过后提交。
4. [pending] 实现数据库事实来源、Redis at-least-once 通知/消费和独立 Worker；单元、API、Runner 回归通过后提交。
5. [pending] 完成 Compose/CI、就绪/存活、结构化日志/指标以及并发领取、重启、Redis 故障、取消、租约过期等故障验证；相关门禁通过后提交。
6. [pending] 运行完整 lint/test/smoke/迁移/Compose 门禁，完成安全与 diff 审查，更新所有指定文档和本日志，并提交收尾。

### 提交记录

| 阶段 | Commit | 验证摘要 |
| --- | --- | --- |
| Phase 1 基线 | `3db1e29` | 152 个既有 Phase 0/1 文件；`.env`、DB、backup、node_modules/dist 均未纳入；无真实密钥模式命中 |
| ADR | 本阶段提交（hash 在下一阶段回填） | `make lint` 通过；独立只读审查无 P0/P1；Phase 2 如实为 in_progress |
| 持久化与迁移 | 待提交 | 待执行 |
| Worker 与队列 | 待提交 | 待执行 |
| 故障验证与基础设施 | 待提交 | 待执行 |
| 文档收尾 | 待提交 | 待执行 |

## 验证矩阵

| 验收项 | 命令或场景 | 预期 | 实际 |
| --- | --- | --- | --- |
| 代码质量 | `make lint` | 全部通过 | 待执行 |
| 完整回归 | `make test` | 后端/前端无退化 | 待执行 |
| 离线垂直链路 | `make smoke` | API + 独立 Worker + Mock 完成 | 待执行 |
| 迁移往返 | SQLite 与 PostgreSQL `upgrade/downgrade/upgrade` + `alembic check` | 两种数据库均通过 | 待执行 |
| 并发领取 | 两个 Worker 同时领取同一 Run | 仅一个有效租约 | 待执行 |
| Worker 重启 | 执行中杀死并重启 Worker | 租约过期后接管，响应不重复 | 待执行 |
| API 重启 | Worker 执行期间重启 API | Run 不受影响并完成 | 待执行 |
| Redis 故障 | 创建/执行期间暂停 Redis 后恢复 | 状态明确且由 DB 恢复 | 待执行 |
| 取消/租约过期 | pending/running 取消及过期接管 | 确定终态、旧 Worker 被 fencing | 待执行 |
| Compose | `docker compose config`、`docker compose up --build --wait` | 五服务 ready/healthy | 待执行 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
| --- | --- | --- | --- |
| 11:44 CST | discovery | 仓库无 commit，所有 Phase 0/1 文件未跟踪；暂存区为空 | 必须先建立基线，Phase 2 文件从后续提交开始 |
| 11:44 CST | discovery | 当前 API 创建 Run 后调用进程内 `EvaluationTaskManager`；应用启动会把全部 running Run 标为 failed | Worker 分离和租约恢复需要替换两条路径 |
| 11:44 CST | discovery | `evaluation_responses` 已有 `(run_id, question_id)` 唯一约束，但 Runner 插入/进度尚非重复投递幂等 | 保留约束并增加冲突安全写入与租约条件 |
| 11:48 CST | milestone | Phase 1 基线以 root commit `3db1e29` 固定，Phase 2 工作日志未混入该提交 | 后续 diff/commit 可精确审计，不覆盖原有工作 |
| 11:55 CST | decision | 接受 ADR-0005：PostgreSQL/DB 为事实来源，Redis Streams 仅通知，单调 token 对每次执行写 fencing | 后续实现不得用 Redis/内存裁决状态，也不得宣称 Provider exactly-once |
| 12:02 CST | review | ADR/状态文档独立只读复核未发现 P0/P1；`make lint` 全部通过 | ADR 阶段满足提交门禁，下一步只按已接受语义实施 migration/持久化 |

## 实际修改

| 文件/模块 | 修改内容 | 状态 |
| --- | --- | --- |
| 本工作日志 | 固化任务目标、范围、风险、验收、计划和提交边界 | 已完成（尚未提交） |
| `docs/decisions/ADR-0005-durable-task-execution.md` | 固定拓扑、交付、租约、幂等、恢复、取消、dead-letter、安全与回滚语义 | 已完成（待 ADR 阶段提交） |
| Project Status、Roadmap、Phase 2 | 将 Phase 2 从 planned 更新为真实的 in_progress 并链接 ADR/工作日志 | 已完成（待 ADR 阶段提交） |

## 测试结果

- ADR 阶段：`make lint` 退出 0；Ruff check/format、ESLint、TypeScript 全部通过。
- ADR/状态一致性审查：无 P0/P1；没有提前宣称代码、迁移或故障验收已完成。

## 未运行验证

- 全部 Phase 2 验证待实现后运行。
- 真实模型调用明确禁止，不会运行。

## 安全检查

- 未读取或输出 `.env` 内容；现有 `.env`、SQLite 数据库、备份、依赖目录保持忽略。
- 未执行 reset、清库、覆盖数据库、push 或真实 Provider 调用。

## 结果与下一步

Phase 1 基线已固定，ADR 已在任何 Phase 2 代码改动前接受。下一步审查并提交 ADR/状态/工作日志阶段，然后开始可靠性字段和 `0002` 迁移；在该提交完成前不改 Runner 行为。
