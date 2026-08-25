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
- [x] `POST /api/v1/runs` 返回 `202` 后 API 不执行任务；独立 Worker 可完成离线 Mock Run。
- [x] 两个 Worker 并发竞争时只有一个有效租约；陈旧 Worker 的写入被 fencing 条件拒绝。
- [x] Worker/API 重启不会丢失 Run；已提交逐题响应不会重复，租约过期可被接管。
- [x] Redis 暂时不可用时有明确降级证据，数据库协调仍可恢复任务；恢复后队列可继续工作。
- [x] 取消、任务异常、逐题异常、重试耗尽、dead-letter 和租约过期行为确定且有测试。
- [x] PostgreSQL migration `upgrade -> downgrade -> upgrade`、`alembic check` 通过；SQLite 兼容/退出策略有测试和文档。
- [x] `make lint`、`make test`、`make smoke` 以及当前 205 项后端、13 项前端回归不退化。
- [x] Compose 中 PostgreSQL、Redis、API、Worker、frontend 可 `up --build --wait`，健康/就绪检查通过，测试数据可清理。
- [x] README、Architecture、API、Testing、Deployment、Security、Roadmap、Project Status、Changelog、Next Task、Phase 2 与本日志反映真实证据。

## 假设

- PostgreSQL 是容器/部署目标；SQLite 保留为单机开发与自动化测试兼容路径，但也必须由 Alembic 管理。
- Redis 只保存可重复投递的唤醒消息；Run、租约、attempt、取消与最终证据全部由数据库裁决，因此 Redis 丢消息或短时不可用不会改变事实状态。
- Run 级租约足以支持当前一次 Run 内有限并发；逐题唯一约束和“跳过已有响应”恢复逻辑提供结果幂等。
- 旧 Run 快照继续按协议 v1 解读；执行恢复机制属于执行策略/审计信息，不改变同一问题响应的评分函数。
- 本机具备 Docker/Compose、uv、Node/npm，可执行真实 PostgreSQL/Redis 故障验证；若环境事实不符，必须记录未通过项而非伪造证据。

## 风险

| 风险 | 影响 | 计划缓解 | 当前结果 |
| --- | --- | --- | --- |
| 重复投递或租约接管后双写 | 重复响应、错误进度或聚合 | 唯一约束、条件更新、单调租约令牌、所有关键写入校验有效租约 | SQLite/真实 PostgreSQL 仓储级与真实 Redis queue-first/ACK 不确定结果验证通过 |
| Worker 在 Provider 返回后、DB 提交前崩溃 | 可能重复远端调用/计费 | 明确 at-least-once 边界；本地证据幂等；只用 Mock 做故障测试 | Response/租约幂等自动化与实际 lease owner SIGKILL/自然接管通过；不承诺 Provider exactly-once |
| Redis 故障阻塞创建或丢任务 | Run 永久 pending | 先提交数据库；队列 best-effort；Worker 定期从数据库 reconciliation | 真实 Redis 停止时 API 0.047s 返回 202，DB-only reconciliation 完成；恢复后新 Run 正常入流 |
| PostgreSQL 与 SQLite 锁/时间/DDL 语义不同 | 单机测试通过但部署失败 | 两种数据库专项测试；PostgreSQL 容器迁移往返和并发领取实测 | 迁移、约束、claim/cancel 竞态已在双方言通过；PostgreSQL 全栈进程链与 head→0001→head 哈希往返通过 |
| API 重启错误地终止 running Run | 破坏恢复语义 | 删除启动时“全部 running 标失败”；只由租约过期与重试策略恢复 | 独立 Uvicorn 真实重启后 terminal Run、15 个 Response ID 集与分数不变 |
| Phase 2 变更评分口径 | 历史不可比 | 冻结协议 v1；复用既有聚合与响应模型；增加协议回归断言 | 既有回归、离线 Smoke、真实 Redis ACK/duplicate 与正式 Compose 八场景协议快照通过 |
| 初始仓库无 commit | 无法区分基线与 Phase 2 | 工作日志先创建但不纳入基线；Phase 1 文件单独首提，之后阶段提交 | `3db1e29` 已固定基线 |

## 执行计划

- Owner: Codex
- Status: in_progress
- Created: 2026-08-25 11:44 CST
- Updated: 2026-08-25 15:13 CST

### 实施步骤与提交边界

1. [completed] 完成文档、Runner、迁移、测试和基础设施审计；创建本日志；提交不含本日志的 Phase 1 基线。
2. [completed] 编写 ADR，并把 Phase 2/Project Status 标为真实进行中；审查后提交文档阶段。
3. [completed] 实现可靠执行字段、PostgreSQL/SQLite Alembic 迁移、原子租约与幂等持久化；双方言迁移、竞态、fencing 和回归门禁通过后提交。
4. [completed] 实现数据库事实来源、Redis at-least-once 通知/消费和独立 Worker；单元、API、Runner 回归、真实 Redis 与独立进程证据通过后提交。
5. [completed] 完成 Compose/CI、就绪/存活、结构化日志/指标以及 PostgreSQL 全栈并发领取、执行中重启、取消、租约过期等故障验证；相关门禁通过后提交。
6. [completed] 增加显式、只读源、单事务且可对账的 SQLite→PostgreSQL 导入路径；用真实 PostgreSQL 验证成功、回滚、并发互斥和无残留后独立提交。
7. [completed] 运行完整 lint/test/smoke/迁移/Compose 门禁，完成安全与 diff 审查，更新所有指定文档和本日志，并提交收尾。

### 提交记录

| 阶段 | Commit | 验证摘要 |
| --- | --- | --- |
| Phase 1 基线 | `3db1e29` | 152 个既有 Phase 0/1 文件；`.env`、DB、backup、node_modules/dist 均未纳入；无真实密钥模式命中 |
| ADR | `2be2392` | `make lint` 通过；独立只读审查无 P0/P1；Phase 2 如实为 in_progress |
| 持久化与迁移 | `3c975c7` | `make lint`；后端 153 passed/2 个显式基础设施 skip；前端 13 passed；Smoke 1 passed；SQLite migration 25 passed；真实 PostgreSQL migration/lease 通过 |
| Worker 与队列 | `2006d3f` | `make lint`；后端 177 passed/4 个显式基础设施 skip；前端 13 passed；Smoke 1 passed；真实 Redis 2 passed；独立 API/Worker 重启与 Redis stop/start 通过；复审无 P0/P1 |
| 故障验证与基础设施 | `b3289b1` | 默认含 build 的自动 Compose 验收 8/8；后端非集成、前端 test/lint/build、Ruff/format、Compose config 与 diff 门禁通过；复审无 P0/P1 |
| SQLite→PostgreSQL 导入 | `103ab79` | 离线 14 passed；真实 PostgreSQL 导入、事务回滚、双源并发互斥、COMMIT 未确认/提交后故障语义与既有 lease 共 3 passed；全后端非集成 205 passed；无随机库/容器残留 |
| 文档收尾 | 本阶段提交（hash 见 Git log） | 最终 lint/test/smoke、SQLite/真实 PG 迁移、真实 PG/Redis integration、默认 build Compose 8/8、文档链接/围栏/diff/残留审查通过；Phase 2 保持 in_progress |

## 验证矩阵

| 验收项 | 命令或场景 | 预期 | 实际 |
| --- | --- | --- | --- |
| 代码质量 | Ruff/format、ESLint、TypeScript、Compose config、diff check | 全部通过 | 当前工作树通过：Ruff/format、ESLint、TypeScript build、`docker compose config --quiet` 和 `git diff --check` 均退出 0 |
| 完整回归 | 后端非集成、前端 test/lint/build | 后端/前端无退化 | 当前阶段通过：后端非集成测试全部通过（导入专项在下一阶段单独统计）；前端 13 passed，lint/build 通过；基础设施集成在隔离容器另行验证 |
| 离线垂直链路 | `make smoke` | API + 独立 Worker + Mock 完成 | 通过 1 passed：API 提交后仍为 pending/0 Response，再由独立 WorkerService 完成 Mock Run；API 无 task manager |
| 迁移往返 | SQLite 与 PostgreSQL `upgrade/downgrade/upgrade` + `alembic check` | 两种数据库均通过 | 通过：SQLite 25 项；真实 PostgreSQL 16 空库/旧 running 聚合/active downgrade 拒绝/往返/check 均通过 |
| 并发领取 | 两个 Worker 同时领取同一 Run | 仅一个有效租约 | SQLite Barrier 与真实 PostgreSQL 双连接仅一个 claim；自动 Compose 同时运行两个 Worker，单一租约 owner 完成，队列最终 pending/lag 均为 0 |
| Worker 重启 | 执行中强杀实际租约 owner，不手工改租约 | 旧租约自然过期后由另一 Worker 接管，既有响应不重复 | 自动 Compose 通过：精确 SIGKILL owner，peer 以递增 token 接管；崩溃前 Response ID 保留，最终 15 个问题唯一且 protocol v1 聚合为 100 |
| API 重启 | Run 执行中重启 API 容器 | Run 事实不受 API 生命周期影响 | 自动 Compose 通过：barrier 时已有持久响应；API 重启后 Run 完成、15 个 Response 唯一且协议快照通过 |
| Redis 故障 | 创建/执行期间停止 Redis 后恢复 | 状态明确且由 DB 恢复 | 自动 Compose 通过：Redis 容器保持 exited 时 `/ready` 503、`/live` 与 DB health 200；POST 202 后 Worker 仅靠 DB 对账完成，恢复后新 Run 入流且 PEL/lag 为 0 |
| 取消/租约过期 | pending/running 取消、重复消息及过期接管 | 确定终态、旧 Worker 被 fencing、消息最终 ACK | 自动 Compose 通过：pending 取消为 0 Response；running 取消后计数冻结；显式重复 XADD 为 no-op，last-delivered-id 到达目标且 PEL=0；租约过期由 peer 自然接管 |
| Compose | `python3 scripts/phase2_acceptance.py`（默认 build） | 五个长运行服务 ready/healthy，八类故障场景与清理通过 | 首次最终门禁项目 `llmbenchlab-p2-1ff5a1cfd83a` 因 Python 3.9 无法解析 PostgreSQL 五位小数时间戳而失败，精确清理通过；修复并自检 0/1/5/6 位小数后，`llmbenchlab-p2-ad6e195965bb` 8/8 passed，最终 ready、pending=0、lag=0，`down -v` 后容器/卷/网络均为空 |
| SQLite→PostgreSQL | read-only source→随机空 PostgreSQL；提交前/COMMIT 确认/提交后注入失败；双源并发 | 成功时五表/协议事实一致；提交前失败全回滚；COMMIT 未确认状态不确定；提交后失败明确已提交；并发恰一成功；源不变 | 离线 14 passed、真实 PostgreSQL import + lease 3 passed；precommit 失败五表空；真实 commit 后确认丢失为 exit 4 且目标可已完整；postcommit snapshot/output 失败为 exit 3 且数据存在；并发败者因目标非空拒绝 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
| --- | --- | --- | --- |
| 11:44 CST | discovery | 仓库无 commit，所有 Phase 0/1 文件未跟踪；暂存区为空 | 必须先建立基线，Phase 2 文件从后续提交开始 |
| 11:44 CST | discovery | 当前 API 创建 Run 后调用进程内 `EvaluationTaskManager`；应用启动会把全部 running Run 标为 failed | Worker 分离和租约恢复需要替换两条路径 |
| 11:44 CST | discovery | `evaluation_responses` 已有 `(run_id, question_id)` 唯一约束，但 Runner 插入/进度尚非重复投递幂等 | 保留约束并增加冲突安全写入与租约条件 |
| 11:48 CST | milestone | Phase 1 基线以 root commit `3db1e29` 固定，Phase 2 工作日志未混入该提交 | 后续 diff/commit 可精确审计，不覆盖原有工作 |
| 11:55 CST | decision | 接受 ADR-0005：PostgreSQL/DB 为事实来源，Redis Streams 仅通知，单调 token 对每次执行写 fencing | 后续实现不得用 Redis/内存裁决状态，也不得宣称 Provider exactly-once |
| 12:02 CST | review | ADR/状态文档独立只读复核未发现 P0/P1；`make lint` 全部通过 | ADR 阶段满足提交门禁，下一步只按已接受语义实施 migration/持久化 |
| 12:12 CST | migration | 本地用户 SQLite 在自动完整性检查和备份后从 `0001` 升到 `0002`；源备份 head 为 `0001`、15 条 Response，升级后实体/证据计数不变且 `alembic check` clean | 继续保留被忽略的原 DB 与备份，不执行回退或覆盖 |
| 12:18 CST | review/fix | 只读审查发现旧 Phase 1 running 快照、running+cancel 迁移、API 取消竞态、完整 Response 终态窗口和 Runner 子协程泄漏风险 | 迁移按冻结 `mark_failed_without_resume` 收敛；取消改为条件更新；完整事实直接完成；失租/关停取消并等待题协程 |
| 12:25 CST | postgres evidence | 临时 `postgres:16-alpine` 隔离库完成空库升级、真实旧 running/cancel 聚合、active downgrade 拒绝、往返/check 及两连接 lease/claim-cancel 测试 | 跨方言迁移与核心锁语义获得真实证据；容器随后删除 |
| 12:28 CST | review/fix | 复审发现非末次 attempt 完整事实仍可能多耗 attempt，以及 SQLite cancel/claim 与 retry/cancel 窗口 | claim 条件排除完整 Response 集；reaper 扫描全部过期租约；取消使用单条 active UPDATE RETURNING；SQLite Barrier 连续 10 轮通过 |
| 12:43 CST | implementation | API 改为数据库 commit 后 best-effort XADD；独立 Worker 先做 DB reconciliation，再消费 Redis Streams；API 不再加载 Runner/Adapter | Redis 是低延迟通知而非事实来源；DB commit 失败绝不 publish，通知失败仍返回可恢复的 202 |
| 12:50 CST | redis evidence | 真实 `redis:7-alpine` 验证 publish-before-group、PEL、XAUTOCLAIM、ACK；真实 Runner/DB 的 queue-first 用例在 XACK 前检查 protocol v1 完整证据 | Redis 要求至少 6.2；服务端 ACK 成功但客户端报错后重复通知为严格 no-op |
| 12:54 CST | process evidence | 分离的 Uvicorn/Worker PID 与临时 SQLite 实测：API-only 保持 pending/attempt 0；Worker 启动后完成；API 与 Worker 分别停止/重新启动后事实不变 | 证明 API 生命周期不拥有任务、Worker 可从持久化 pending 恢复；PostgreSQL Compose 执行中强杀留待下一阶段 |
| 12:56 CST | fault evidence | 运行中停止真实 Redis 后创建 Run：API 0.047s 返回 202，Worker 仅靠 DB 对账完成 15 条；恢复 Redis 后入流与 ACK 恢复 | Redis 故障不丢数据库任务，也不改变评分；临时容器、进程和数据库随后清理 |
| 12:59 CST | review/fix | 三路只读复审发现 stop/read、stop/DB scan、active reaper、半开连接和 semaphore 等关停竞态 | read 与 stop 竞速取消；Redis 每类操作独立 deadline；active 时不 reap；Runner 在 semaphore 前后检查 stop，安全 drain 后由租约自然到期恢复且该 delivery 不 ACK |
| 13:02 CST | gate | `make lint`、`make test`、`make smoke` 和真实 Redis 集成全部通过；最终代码复审无 P0/P1 | Worker/队列阶段满足提交门禁；Phase 2 仍因 Compose/可观测性/全栈故障门禁未完成而保持 in_progress |
| 13:35 CST | implementation | Compose 固定为 PostgreSQL、Redis、一次性 migrate、API、独立 Worker 与 frontend；API/Worker 启动只检查 Alembic head，迁移仅由 migrate service 拥有 | 消除多进程并发跑迁移；数据库和 Redis 不发布宿主端口，API/frontend 只绑定 loopback |
| 13:43 CST | observability | 增加应用 JSON 日志/请求与 Run correlation、`/live`、DB-only `/health`、DB+head+Redis `/ready`、数据库事实指标和 Worker 依赖能力探针 | Redis 不可用明确降级而非伪装 ready；探针不是 Worker 主循环 liveness，数据库驱动 timeout 仍是同步探测的最终上界 |
| 13:52 CST | review/fix | 复审发现请求路径可能泄露、finish/cancel 竞态日志失真、Worker 异常 ACK 风险和旧 Compose orphan | 日志仅记录代码定义 route 模板；记录锁内真实终态；未处理异常不 ACK；Make Compose 命令增加精确 project orphan 清理但默认不删卷 |
| 14:03 CST | manual evidence | 隔离 Compose 双 Worker 实测 API 执行中重启、实际租约 owner SIGKILL、Redis stop/start、pending/running 取消、重复投递和迁移往返 | 所有 Run 保持 15 个问题唯一与 protocol v1 口径；使用精确项目名 `down -v` 后无残留，未触碰用户已有 8080 进程 |
| 14:11 CST | automated gate | 默认含 build 的 `scripts/phase2_acceptance.py` 在唯一项目 `llmbenchlab-p2-2a59e6283eda` 完成 8/8；证据敏感信息扫描通过 | 最终 ready、Redis pending/lag 为 0；同一个 15 题 baseline Run 的协议 v1 核心字段和 15 条 Response 在 head→0001→head 三时点哈希相同；清理后容器/卷/网络均为空 |
| 14:15 CST | gate | 后端非集成回归、Ruff/format、前端 13 tests/lint/build、Compose config 和 diff check 通过；代码只读复审无 P0/P1 | 故障验证与基础设施阶段满足提交条件；Phase 2 因数据导入与文档/完整阶段门禁尚未收口而继续 in_progress |
| 14:18 CST | implementation | 增加显式单向导入器：SQLite 以 URI `mode=ro` 与 `query_only` 读取一致快照；双方必须在 head、源无 active Run、目标五表为空；五表按依赖序复制 | 目标在单事务中获取 advisory lock 与 `ACCESS EXCLUSIVE`，提交前逐表 count/PK/content digest 对账；CLI 默认从环境变量读取含凭据 DSN，拒绝 argv password |
| 14:21 CST | review/fix | 独立审查发现集成测试可能误清非专用库、含密码 DSN 进入 argv、真实 PG 缺少回滚/并发证据，以及 pre-head 共享锁到表独占锁的并发死锁 | 集成测试改为 loopback 管理库内 CREATE/DROP 随机专用库；CLI 增加 `--target-env`；真实 PG 注入中途失败和双源 Barrier；advisory transaction lock 在 preflight 前串行化 |
| 14:23 CST | evidence correction | 首次把既有 PostgreSQL lease 测试与 importer 一起调用时，明确管理库尚未迁移，fixture 在 TRUNCATE 不存在表时退出 1 | 不将该次记为通过；先显式 `alembic upgrade head`/`check`，再运行 importer + lease，3 项全部通过 |
| 14:26 CST | gate | 导入专项离线 12 passed；真实 PostgreSQL 3 passed；全后端非集成 203 passed；Ruff/format/diff 通过；随机库与明确命名容器均无残留 | 导入阶段满足提交条件；提交前故障全回滚，提交后再做独立只读对账，后者失败时必须按“可能已提交”运维语义处理 |
| 14:29 CST | review/fix | 最终只读审查指出 P1：postcommit 使用默认 READ COMMITTED 多查询，且校验/输出失败会在已提交后仍被 CLI 笼统报为普通失败，可能诱导盲目重试 | 新增 `committed_but_verification_failed` 专用异常、CLI 状态与退出码 3；postcommit 改为单个 `REPEATABLE READ`、`READ ONLY` 快照；普通失败仍只表示提交前未完成 |
| 14:35 CST | evidence | 真实 PG 分别注入 postcommit snapshot 和 output OSError，均得到专用已提交异常；目标摘要与源一致，直接重试因非空拒绝；隔离级别实测 `repeatable read`/read-only `on` | 离线 13、真实 PG + lease 3、全后端非集成 204 全通过；Ruff/format/diff 通过；修复后临时随机库与容器再次确认为空 |
| 14:39 CST | review/fix | 复审指出第二个 P1：PostgreSQL 可能已执行 COMMIT，但客户端在收到确认前断连；隐式 context commit 会把这一不确定结果落入普通 exit 2 | 改为显式 `transaction.commit()`；其任何异常保守映射 `commit_outcome_unknown`/exit 4，提示目标可能为空或已完整、禁止盲重试；已确认 commit 后的连接清理异常归入 exit 3 |
| 14:45 CST | evidence | 真实 PostgreSQL 在实际 commit 完成后注入 acknowledgement loss，得到 exit 4 对应异常；目标五表完整且直接重试因非空拒绝；Engine pool dispose 改为 best-effort，不覆盖已判定数据库结果 | 离线 14、真实 PG + lease 3、全后端非集成 205 全通过；Ruff/format/diff 通过；第三次临时随机库/容器清理再次为空 |
| 14:47 CST | review | 最终只读复审确认两个 importer P1 均关闭，未发现剩余 P0/P1 | 显式 commit unknown、已确认 commit 后复核失败、稳定只读快照与 dispose 语义满足提交门禁 |
| 14:51 CST | failed gate | 导入提交后的首次默认 build Compose 最终门禁在项目 `llmbenchlab-p2-1ff5a1cfd83a` 运行到租约 owner 强杀场景时失败：宿主 Python 3.9 的 `datetime.fromisoformat` 拒绝 PostgreSQL 输出的五位小数时间戳 | 不把该次写成通过；脚本 finally 的 `down -v` 返回 0，容器/卷/网络均为空。修复仅规范化 1–6 位小数到六位，不改变应用、数据库或协议语义 |
| 14:56 CST | final compose gate | 时间戳解析器对 0/1/5/6 位小数在 Python 3.9 自检通过；新隔离项目 `llmbenchlab-p2-ad6e195965bb` 默认 build 完成 8/8 | 最终 ready、Redis pending/lag 均为 0；baseline Run/15 Responses 的迁移三时点哈希一致；精确清理无容器/卷/网络残留；失败与成功两份 evidence 均保留在 Git 忽略目录 |
| 15:13 CST | final review/fix | 完整 lint/test/smoke、双方言迁移和 5 项真实 PG/Redis integration 已完成；三路文档终审发现并修正四类 P1：Architecture 将过期接管误画成 `running -> pending`，本地 integration 命令缺 PG migration，空库 CI 与带数据 Compose hash 证据混写，以及外部 PG 导入后误建议启动默认 Compose | 实现实际保持过期 `running` 由新 token/attempt 直接接管；测试命令明确专用可破坏库与 head 前置；所有同类 hash 表述限定为 baseline Run/15 Responses、非全库快照；外部目标恢复使用其自身部署流程。复查无剩余 P0/P1，Phase 2 继续 `in_progress` |

## 实际修改

| 文件/模块 | 修改内容 | 状态 |
| --- | --- | --- |
| 本工作日志 | 固化任务目标、范围、风险、验收、计划、提交边界与真实命令证据 | 已完成本切片记录 |
| `docs/decisions/ADR-0005-durable-task-execution.md` | 固定拓扑、交付、租约、幂等、恢复、取消、dead-letter、安全与回滚语义；澄清旧 Phase 1 升级和完整事实终结窗口 | 已接受并随实现澄清 |
| `20260825_0002_reliable_execution.py` / migration preparation | 新增 attempt/lease/heartbeat/backoff/dead-letter 字段、约束、索引，识别 `0001` schema，拒绝 active downgrade，并安全收敛旧 running | 已完成并通过 SQLite/PostgreSQL 往返 |
| `EvaluationRun` / schemas / settings / session | 暴露可靠性审计字段，增加 Worker/Redis/数据库池配置，保留 SQLite 单机兼容 | 已完成 |
| `RunLeaseRepository` | DB 时钟条件领取、单调 token、心跳、逐题 fenced 幂等、有限退避、取消、过期 reconciliation、完整证据恢复和 dead-letter | 已完成 |
| `EvaluationRunner` | 通过租约仓储写入，恢复时跳过既有 Response；失租取消在途题；关停停止领取与启动新题，安全 drain 后依赖持久租约自然过期；聚合保持 protocol v1 | 已完成并仅由独立 Worker 加载 |
| API 取消与创建快照 | 新 Run 冻结 attempt/恢复语义；取消使用跨方言原子状态机；创建先 commit 再 best-effort 通知，API 不加载/执行 Adapter | 已完成 |
| Redis Streams | 版本化、限长 XADD；Consumer Group、PEL、游标式 XAUTOCLAIM、处理后 ACK；连接/读写均有上界，失败脱敏并降级 | 已完成；真实 Redis 7 验证通过 |
| 独立 Worker | 单 Run 执行、数据库兜底扫描、租约 reaper、队列唤醒、ACK/自然到期恢复、SIGTERM/grace 与独立 CLI | 已完成；独立进程和故障实测通过 |
| 本地脚本与配置 | `make worker`；`make dev` 监管 API/Worker/frontend；Redis 可选，Smoke 强制 DB-only，配置包含租约/心跳/poll/deadline | 已完成；Compose/部署配置已在本阶段补齐 |
| 测试 | 增加迁移/租约竞态、API commit/XADD 顺序与超时、Runner 关停、queue/Worker、fresh import、真实 Redis PEL/ACK/duplicate 用例 | 已完成并通过 |
| `compose.yaml` / Docker / nginx | 五个长运行服务加一次性 migrate；PostgreSQL/Redis 持久卷与健康检查；API/frontend loopback 端口；Worker 优雅停止；nginx 指向 API | 已完成并在默认 build 全栈验收通过 |
| 健康、就绪与探针 | `/live` 无外部依赖；`/health` 保持 DB-only；`/ready` 并行、有界检查 DB、Alembic head 与 Redis；Worker probe 区分 DB hard fail 与 Redis degraded | 已完成；明确 capability/readiness 边界，不冒充 Worker event-loop liveness |
| 应用日志与指标 | LLMBenchLab 应用 logger 输出脱敏 JSON；请求 ID/Run correlation；Worker/Runner 生命周期事件；`/tasks/metrics` 由数据库实时事实导出积压、租约、取消、retry/dead-letter 等 gauge | 已完成；不把 Uvicorn access log 或这些 gauge 宣称为完整历史审计/延迟 counters |
| CI / Make / 自动验收 | SQLite 与真实 PostgreSQL/Redis 分层 job；全栈 reliability job；`make phase2-acceptance`；隔离项目、随机 loopback 端口、八场景证据与强制清理 | 已完成；证据落于被忽略的 `.pytest_cache/artifacts` |
| `app/db/import_sqlite.py` | 显式、单向、只读源导入；head/integrity/FK/active/empty preflight；五表单事务复制、PG advisory+table lock；content-free 三阶段摘要；安全 CLI | 已完成；只支持 stopped SQLite→空 PostgreSQL，不覆盖或合并；COMMIT 未确认用 `commit_outcome_unknown`/exit 4，已确认提交后的复核故障用 `committed_but_verification_failed`/exit 3 |
| 导入测试 | SQLite 源只读/损坏/active/head/rollback/canonical；真实 PG 随机库成功、故障回滚、并发竞争、重复拒绝、协议字段和 secret output | 已完成；临时数据库和容器清理已二次确认 |

## 测试结果

- ADR 阶段：`make lint` 退出 0；Ruff check/format、ESLint、TypeScript 全部通过。
- ADR/状态一致性审查：无 P0/P1；没有提前宣称代码、迁移或故障验收已完成。
- 持久化/迁移阶段 `make lint`：Ruff check/format（68 files）、ESLint、TypeScript 全部通过。
- 持久化/迁移阶段 `make test`：后端 `153 passed, 2 skipped`；两个 skip 都是未设置显式 PostgreSQL DSN 的 destructive integration；前端 `13 passed`。
- `make smoke`：离线 Mock `1 passed, 4 deselected`；此时仍是 Phase 1 兼容的进程内调度，不能作为独立 Worker 验收。
- SQLite migrations：`25 passed`，覆盖 unversioned/versioned `0001` 采用、运行中旧任务 failed/cancelled 收敛、active downgrade 拒绝、约束及元数据往返。
- SQLite leases：`14 passed`，其中并发领取、claim/cancel、retry/cancel、真实 Runner finish/cancel 使用 Barrier；生产一致的 `autoflush=False` 下完整套件连续执行 10 轮无失败。
- Runner reliability：`2 passed`，证明已知失租不会启动等待中的下一题，并证明 shutdown 会取消且等待全部在途题协程。
- 真实 PostgreSQL：空库 `upgrade head`/`alembic check`、`downgrade 0001`/`upgrade head`/check 通过；旧普通/取消 running 分别聚合为 failed/cancelled，非空 latency/token/cost 保持；active downgrade 退出 1 且 revision 保持 `0002`，排空后往返通过。
- 真实 PostgreSQL integration：`2 passed`，覆盖双连接并发 claim、过期接管、旧 heartbeat/Response fencing、重复 Response 幂等及 claim/cancel 竞态。
- 本地用户 SQLite：升级前 head `0001`、1 Run/15 Responses；自动备份后 head `0002`，计数、FK/integrity 与逐 Run 核心事实不变；重复 migrate 未新增备份。
- Worker/队列阶段 `make lint`：Ruff check/format（78 files）、ESLint、TypeScript 全部退出 0。
- Worker/队列阶段 `make test`：后端 `177 passed, 4 skipped`；4 个 skip 仅为未提供显式 PostgreSQL/Redis integration URL；前端 `13 passed`。
- Worker/队列阶段 `make smoke`：`1 passed, 4 deselected`；API 提交后明确断言 pending/attempt 0/0 Response，再由独立 WorkerService 完成 15 条离线 Mock 证据。
- 真实 Redis integration：`2 passed`；一项验证 PEL→XAUTOCLAIM→ACK，另一项验证 queue-first 真实 Runner/DB 在 ACK 前已持久化 protocol v1、15 个 Response、score/completion/accuracy 100、tokens 120/30、cost 0；模拟 ACK 结果未知后的重复投递，全部 ID/聚合/attempt 不变。
- 独立进程：Uvicorn 与 Worker 使用不同 PID；API-only Run 延迟后仍为 pending/attempt 0/0 Response，Worker 启动后 completed/attempt 1/lease token 1/15 Response；API 重启后 Response ID 集 SHA-256 为 `788612aa00ef6df499b5a26c5710c9d1cc567caafe888ba8aa11dda3b62f244c`，状态与 score 100 不变；Worker 停机期间的新 Run 在新 Worker PID 启动后完成。
- 真实 Redis stop/start：停机时 POST 在 0.047 秒返回 202/pending，并持久化 `queue_notification_unavailable`；Worker 在 Redis 完全不可用时仅靠 DB 完成 15 Response/score 100；Redis 7 恢复后新 Run `last_enqueued_at` 非空、完成且 PEL 为 0。
- 最终三路只读复审未发现 P0/P1；同步数据库驱动调用仍受数据库 driver/连接池 timeout 而非 asyncio grace 约束，列为部署支持边界。
- 故障验证阶段本地门禁：Ruff check/format 通过；后端全部非集成测试通过；前端 `13 passed`、ESLint 与生产 build 通过；`docker compose config --quiet`、`git diff --check` 通过。Python 3.14 下 FastAPI/TestClient 与 pytest-asyncio 有已知上游弃用警告，不影响退出状态。
- 故障验证阶段的首次正式全栈命令使用项目 `llmbenchlab-p2-2a59e6283eda`，2026-08-25 14:09–14:11 CST 完成 8/8；这份阶段证据没有替代导入提交后的最终重跑。
- 导入提交后的首次最终全栈门禁使用项目 `llmbenchlab-p2-1ff5a1cfd83a`，2026-08-25 14:50–14:51 CST 在前四个场景期间失败。拓扑、协议基线和 API restart 已通过，但宿主 Python 3.9 的 `datetime.fromisoformat` 无法解析 PostgreSQL 返回的 `2026-08-25T06:51:51.87456+00:00`；该次 `down -v` 和容器/卷/网络残留检查仍通过，未将其记为成功。
- 修复验收脚本对 PostgreSQL 1–6 位小数秒的兼容解析后，Python 3.9 compile/self-check 及 0/1/5/6 位变体检查通过。新项目 `llmbenchlab-p2-ad6e195965bb` 于 2026-08-25 14:53–14:55 CST 重新默认 build，八场景全部通过：拓扑/健康、protocol v1 基线、执行中 API restart、实际租约 owner SIGKILL 后自然过期接管、Redis stop/start 与 DB reconciliation、pending cancel、running cancel 加 duplicate delivery、PostgreSQL head→0001→head 往返。
- 正式协议证据：所有完成 Run 均为离线 Mock；baseline 为 15 个唯一 Response、score/completion/answered accuracy 100、tokens 120/30、cost 0；故障场景未改变 `llmbenchlab-protocol-v1` 评分含义。
- 最终队列/迁移/清理证据：Redis consumer group `pending=0`、`lag=0`；同一个 15 题 baseline Run 的协议 v1 核心字段及其 15 条 Response 在迁移前/`0001`/恢复 head 三时点 canonical hash 均为 `94aceb00a70b94b1515537409430e99e1530b2110a09490876b18c3dcb2650ed`，这不是全库快照；`down -v` 返回 0，项目容器、卷、网络均为空。最终证据位于被 Git 忽略的 `.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-ad6e195965bb/evidence.json`，失败证据也保留在相邻项目目录，敏感值扫描无命中。
- SQLite→PostgreSQL 离线专项：`14 passed`；覆盖 URL/方言、argv password 拒绝且不回显、只读源 SHA-256/mtime 保持、head/active/FK 拒绝、canonical 跨方言稳定、提交前故障回滚、五表复制，以及 CLI 对 COMMIT 未确认/已提交复核失败分别使用状态/退出码 4/3。
- SQLite→PostgreSQL 真实 PostgreSQL 16：管理库显式 upgrade head/check 后，importer 与既有 lease integration 合计 `3 passed`。Importer 用例内部创建 `llmbenchlab_import_<32hex>_test`，验证第三表前注入失败后五表全空、两个不同源并发导入恰一成功、成功导入三阶段摘要相同、protocol v1/attempt/lease/JSON/Decimal 保持、重复导入拒绝及 CLI 环境变量路径。
- 提交后故障语义：真实 PostgreSQL 分别在 postcommit snapshot 与 postcommit summary output 注入异常；二者均抛出 `SQLiteImportCommittedVerificationError`，目标数据与源摘要相同且盲目重试因非空被拒绝。snapshot 实测处于单一 `repeatable read`、read-only transaction；CLI 将此状态输出为 `committed_but_verification_failed` 并返回 3，不宣称已回滚。
- COMMIT 确认丢失语义：真实 PostgreSQL 先完成实际 commit、再注入客户端 acknowledgement loss；Importer 抛出 `SQLiteImportCommitOutcomeUnknownError`，目标此例已完整且盲目重试被空目标前置条件拒绝。CLI 将这一保守状态输出为 `commit_outcome_unknown` 并返回 4，不声称回滚或已确认提交。
- 导入阶段回归：后端非集成 `205 passed, 5 deselected`；Ruff check、Ruff format check 与 diff check 退出 0。首次组合真实测试因管理库未迁移而在 setup 失败，显式迁移后才获得上述 3 passed；没有隐藏该失败。
- 最终工作树 `make lint` 退出 0；`make test` 为后端 `205 passed, 5 skipped`、前端 `13 passed`；`make smoke` 为 `1 passed, 4 deselected`。最终隔离 SQLite `head -> 0001 -> head`/check 通过；真实 PostgreSQL 16 同一往返/check 通过，真实 PostgreSQL/Redis `integration` 恰好 `5 passed, 205 deselected, 0 skipped`，随机导入数据库与 Redis DB 15 均为空。
- 最终文档与仓库审查：指定文档相对链接和代码围栏、YAML/Compose 配置、diff whitespace、验收脚本 Python 3.9 编译/四种时间戳、自定义秘密模式及精确 Docker 项目残留检查均通过；最终只读事实审查未留下 P0/P1。

## 未运行验证

- 本切片要求的 lint/test/smoke、SQLite/真实 PostgreSQL migration、真实 PostgreSQL/Redis integration、Compose 八场景和文档一致性门禁均已运行，没有用单元测试替代基础设施证据。
- 真实模型调用按任务约束明确禁止，因此有意未运行；所有模型执行证据均来自 Mock/MockTransport/stub。
- P2-05 的限流、预算、完整背压与公平调度，P2-06 的历史 counters/延迟/完整审计，以及 P2-07 的性能/容量基线和完整 Runbook 不属于“已验证完成”；它们是 Phase 2 继续 `in_progress` 的下一任务范围。

## 安全检查

- 未读取或输出 `.env` 内容；现有 `.env`、SQLite 数据库、备份、依赖目录保持忽略。
- PostgreSQL lease/迁移测试只使用专用隔离测试库；Importer 集成只在 loopback 管理库内创建严格随机命名目标库并精确删除。测试 fixture 对非专用目标要求显式 destructive 开关；临时容器均已停止并删除。
- Redis 只使用本机临时 `redis:7-alpine` 容器和随机测试 Stream；独立进程使用 `/tmp/llmbenchlab-stage3-process.*` 隔离数据库；容器、进程与临时目录均已停止/删除。
- 正式 Compose 验收使用正则约束的唯一项目名、随机 loopback API/frontend 端口、内部 PostgreSQL/Redis 端口和隔离命名卷；脚本移除 Provider credential 环境变量。失败项目 `llmbenchlab-p2-1ff5a1cfd83a` 和最终通过项目 `llmbenchlab-p2-ad6e195965bb` 都执行精确 `down -v`，并验证项目级容器/卷/网络为空。
- 导入 CLI 的 credentialed target 只从 `--target-env` 指定环境变量（默认 `LLMBENCHLAB_DATABASE_URL`）读取；`--target` 只接受无 password URL，错误输出不含 URL、行内容或密钥。真实测试只连接 loopback 管理 DSN，创建严格随机命名目标库并在 finally 使用精确名称 FORCE DROP；外部查询确认无随机库，明确命名的 `llmbenchlab-import-root-20260825-1418`、`...-1430` 与最终确认窗口复验容器 `...-1440` 均已删除。
- 未执行 reset、覆盖用户数据库、push 或真实 Provider 调用；未读取或输出 Provider 密钥。

## 结果与下一步

可靠性 schema、租约/fencing、幂等 Response、独立 Worker、Redis 通知/降级、健康/日志/DB gauges、PostgreSQL 全栈故障恢复与显式 SQLite→PostgreSQL 导入均已通过各自真实门禁；protocol v1 的逐题唯一性与评分口径保持不变。首次最终 Compose 门禁的 Python 3.9 时间戳兼容失败已保留，修复后的隔离重跑 8/8 通过且两次均无 Docker 残留。下一任务是 `NEXT_TASK.md` 定义的并发治理、审计与性能基线；Phase 2 的限流、预算、完整背压、公平调度、历史 counters/延迟、完整审计与性能/容量基线仍未完成，因此继续保持 `in_progress`。
