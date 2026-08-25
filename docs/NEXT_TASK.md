# 下一任务：Phase 2 可靠任务执行基础

> 建议开始时间：Phase 1 合并并建立基线 commit 后  
> 对应阶段：[Phase 2 — Reliability](phases/PHASE-2-RELIABILITY.md)  
> 前置状态：Phase 0、Phase 1 已完成；当前版本 `0.1.0` development baseline

## 背景

Phase 1 已交付可离线运行的 FastAPI、React、SQLite 垂直链路。Run 由 API 进程内的 `EvaluationTaskManager` 执行，使用原子状态领取、低并发、单题错误隔离和协作式取消；进程重启时，遗留 `running` Run 会被标为 `failed`，不会自动续跑。这一设计适合个人本地 MVP，但不支持多 API 实例、可靠恢复或高写并发。

## 当前仓库状态

- 五个核心实体、由 `0000` 与 `0001` 组成的两-revision Alembic 线性迁移链及 SQLite 默认配置已稳定。
- `EvaluationRunner` 与 Adapter/Evaluator 已分层，130 个后端测试及离线 Smoke Test 通过。
- REST API、六个前端页面、13 个 Vitest 用例、CI、Compose 与运行文档已完成。
- `llmbenchlab-protocol-v1` 的评分和快照语义不得因任务执行架构变化而改变。
- 尚无持久队列、Worker 心跳、租约、断点恢复、结构化审计事件或 PostgreSQL 数据迁移工具。

## 目标

在不改变 Phase 1 API 和评测协议语义的前提下，设计并实现 Phase 2 的最小可靠执行切片：以 PostgreSQL 为共享事实来源，以 Redis 支撑任务通知/协调，以独立 Worker 安全领取 Run，并证明 API 或 Worker 重启后任务可恢复且不会产生重复 Response。

## 范围

1. 先新增 ADR，明确数据库事实来源、队列投递语义、租约、幂等、恢复和回滚策略。
2. 增加 PostgreSQL 配置与迁移验证，同时保留 SQLite 作为 Phase 1 本地兼容模式，除非 ADR 明确替代路径。
3. 抽取 Runner 调度接口，使 API 只持久化并投递任务，独立 Worker 执行现有逐题逻辑。
4. 使用 Redis 实现任务通知、取消信号或租约协调；数据库仍是 Run/Response 的最终事实来源。
5. 使用条件更新、幂等键和唯一约束保证同一 Run 只有一个有效执行者，同一题不会重复落库。
6. 增加 lease、heartbeat、attempt、last_error 等必要字段及可回滚 Alembic 迁移。
7. 实现 Worker/API 重启恢复、租约过期接管、取消、有限重试和死信/永久失败语义。
8. 增加结构化日志、Run/Question 关联 ID、ready/liveness 检查与基础任务指标。
9. 扩展 Compose 和 CI 集成测试，提供 PostgreSQL、Redis、API、Worker、frontend 的开发拓扑。

## 非目标

- 不新增 MMLU-Pro、GPQA、IFEval 或代码执行数据集。
- 不实现 LLM Judge、Arena、Agent、长上下文、多用户、鉴权或公共部署。
- 不改变 `llmbenchlab-protocol-v1` 的得分、完成率、回答准确率和可比性规则。
- 不追求 Kubernetes、多区域容灾、exactly-once 消息系统或无限水平扩展。
- 自动测试仍不得调用真实 OpenAI-compatible 服务或要求 API Key。

## 预计修改模块

- `backend/app/runners/`：调度抽象、Worker、租约、恢复与幂等执行。
- `backend/app/models/`、`backend/app/schemas/`：任务状态、attempt/lease/heartbeat 或审计实体。
- `backend/alembic/versions/`：可升级、可降级的数据迁移。
- `backend/app/api/v1/`：ready/任务诊断端点及兼容的 Run 创建/取消路径。
- `backend/tests/`：PostgreSQL/Redis 集成、故障注入、并发领取、恢复和取消测试。
- `compose.yaml`、`Makefile`、`.env.example`、`.github/workflows/ci.yml`：新服务与门禁。
- `docs/`：ADR、Architecture、API、Testing、Deployment、Security、Phase 2 状态和工作日志。

## 验收标准

- [ ] API 创建 Run 后无需在 API 进程内执行即可立即返回 `202` 与 Run ID。
- [ ] 两个 Worker 同时竞争同一 Run 时，只有一个成功获得有效租约。
- [ ] Worker 在题目之间崩溃并重启后，Run 能从持久证据恢复，不重复写入已完成题目。
- [ ] API 重启不影响已投递任务；Redis 暂时不可用时有明确、可恢复的状态与错误。
- [ ] 取消、任务级失败、题级失败、重试耗尽和租约过期均有确定状态转换及测试。
- [ ] PostgreSQL schema upgrade/downgrade/check 通过；SQLite 兼容或迁移退出策略有文档和测试。
- [ ] Phase 1 的 130 个后端测试、13 个前端测试及离线 Smoke Test 继续通过。
- [ ] 新增集成测试不调用真实模型，测试数据与容器可重复清理。
- [ ] Compose 可启动 PostgreSQL、Redis、API、Worker 和 frontend，并通过 ready/health 检查。
- [ ] API 与 `llmbenchlab-protocol-v1` 保持兼容；任何必要的不兼容变化有版本化和迁移说明。

## 必须运行的测试

```bash
make lint
make test
make smoke
(cd backend && uv run alembic upgrade head)
(cd backend && uv run alembic check)
docker compose config --quiet
docker compose up --build --wait
```

此外必须运行并记录：双 Worker 并发领取测试、Worker kill/restart 恢复测试、API restart 测试、Redis 短暂不可用测试、取消与租约过期测试、PostgreSQL migration downgrade/upgrade 往返测试。若 Docker 不可用，必须明确列出未运行项，Phase 2 不得标记完成。

## 需要更新的文档

- 新增任务工作日志和至少一份可靠执行 ADR。
- 更新 `docs/ARCHITECTURE.md`、`docs/API.md`、`docs/TESTING.md`、`docs/DEPLOYMENT.md`、`docs/SECURITY.md`。
- 更新 `docs/phases/PHASE-2-RELIABILITY.md`、`docs/ROADMAP.md`、`docs/PROJECT_STATUS.md`。
- 更新 `README.md`、`CHANGELOG.md` 和本文件。

## 风险

- Redis 的 at-least-once 投递可能造成重复执行；必须以数据库条件更新、幂等键和唯一约束抵御。
- Worker 崩溃可能发生在上游已收费但 Response 未提交之间；需要定义可审计的 retry/cost 语义，不能声称 exactly once。
- PostgreSQL 与 SQLite 行为差异可能隐藏事务和约束问题；两种数据库必须分别验证，不能只依赖 SQLite 单测。
- 取消、租约接管和超时存在竞争条件；状态机必须在 ADR 中列出合法转换，并用并发测试证明。
- 新服务会提高个人部署复杂度；保留清晰的 MVP 本地模式和数据迁移/回滚说明。

## 可直接复制给 Codex 的任务指令

```text
请在 LLMBenchLab 仓库执行 docs/NEXT_TASK.md 定义的“Phase 2 可靠任务执行基础”。开始前严格阅读 README.md、AGENTS.md、docs/PROJECT_STATUS.md、docs/ROADMAP.md、docs/phases/PHASE-2-RELIABILITY.md 和现有 Runner/迁移；检查 Git 状态并创建新的工作日志。先写 ADR，明确 PostgreSQL、Redis、独立 Worker、数据库事实来源、at-least-once 投递、租约、心跳、幂等、恢复与回滚语义，再实施最小可靠垂直切片。不得改变 llmbenchlab-protocol-v1 的评分含义，不得调用真实模型，不得覆盖用户未提交工作，不得 push。必须用并发领取、Worker/API 重启、Redis 故障、取消、租约过期、迁移往返和既有回归测试提供真实证据；任何关键验收未通过时保持 Phase 2 in_progress，并如实更新 README、Architecture、API、Testing、Deployment、Security、Roadmap、Project Status、Changelog、Next Task 和工作日志。
```
