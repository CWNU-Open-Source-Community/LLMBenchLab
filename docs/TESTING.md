# LLMBenchLab 测试指南

## 1. 测试原则

LLMBenchLab 的默认验收路径必须完全离线、可重复且不产生模型费用。自动化测试和 CI 只能使用 `MockModelAdapter`，或使用进程内 `httpx.MockTransport` 验证 OpenAI-compatible 协议；不得访问真实 Provider，不要求 API Key，不把“本机碰巧可用”当作通过证据。

每次报告测试结果时必须写明实际命令、通过/失败、测试数量、失败原因和未运行项。没有执行的 Docker、浏览器或联网验证不能写成通过。

## 2. 测试分层

| 层级 | 目的 | 当前入口/文件 | 外部网络 |
| --- | --- | --- | --- |
| 后端单元测试 | Evaluator、Adapter、Loader/Hash 的边界语义 | `backend/tests/test_evaluators.py`、`test_adapters.py`、`test_dataset_loader.py` | 禁止；Provider 用 MockTransport |
| API 与进程边界测试 | FastAPI Schema、状态码、秘密安全、Run 提交与 API/Worker 分离 | `backend/tests/test_api.py`、`test_run_dispatch.py`、`test_process_boundaries.py` | 禁止 |
| 租约与 Worker 测试 | 条件领取、fencing、心跳、取消、幂等 Response、重试/恢复、队列 ACK | `test_run_leases.py`、`test_evaluation_runner_reliability.py`、`test_worker.py`、`test_task_queue.py` | 禁止；SQLite/假队列 |
| 迁移与导入回归 | SQLite/真实 PostgreSQL migration 往返，以及 SQLite→PostgreSQL 原子导入 | `test_migrations.py`、`test_sqlite_postgres_import.py` | 导入/本地部分禁止；真实 PostgreSQL 用 `integration` marker |
| 真实基础设施集成 | PostgreSQL 并发领取/取消竞态、Redis Streams PEL/ACK/重复投递 | `backend/tests/integration/` 与 importer 的 `integration` 用例 | 只连接显式测试 PostgreSQL/Redis；禁止 Provider |
| Mock 端到端 Smoke | API 提交 pending Run → 独立 WorkerService → Responses → Leaderboard/Metrics | `backend/tests/test_smoke.py`，marker 为 `smoke` | 禁止 |
| Compose 故障验收 | 六服务拓扑、双 Worker、进程/Redis 故障、取消、重复消息、migration 往返 | `scripts/phase2_acceptance.py` / `make phase2-acceptance` | 只拉取/构建基础镜像；模型执行始终为离线 Mock |
| 前端单元/组件测试 | 格式化、状态/指标、错误/空态、主要页面交互 | `frontend/src/**/*.test.ts(x)`、`frontend/tests/` | API 必须 stub/mock |
| 静态检查 | Python lint/format、ESLint、TypeScript | Ruff、ESLint、`tsc` | 不需要 |
| 构建检查 | 确认生产前端可编译打包 | `npm run build` | 安装完成后不需要 |
| 配置检查 | 校验 Compose 插值和服务定义 | `docker compose config` | 不启动 Provider |

单元测试定位纯逻辑错误；真实 PostgreSQL/Redis 集成测试验证方言和队列语义；Smoke 证明最小离线链路；Compose 验收才覆盖真实独立进程故障。四者不能互相替代。可靠执行基础已具备证据，但 Provider 级限流、预算、完整背压/公平调度、历史 counters/延迟、完整审计和性能基线仍未完成，因此 Phase 2 保持 `in_progress`。

## 3. 环境准备

推荐从仓库根目录执行：

```bash
make setup
```

该命令安装后端开发依赖和前端依赖，执行安全迁移前置检查并升级本地数据库；重复执行应保持幂等。直接准备子项目时：

```bash
set -a
source ./.env
set +a
cd backend
uv sync --frozen --extra dev
uv run python -m app.db.prepare_migrations
uv run alembic upgrade head

cd ../frontend
npm ci
```

要求 Python `>=3.11`、`uv`、Node.js/npm。若 lockfile 尚未生成或有意更新依赖，只能在依赖变更任务中使用非 frozen 安装，并把新的 lockfile 与原因一并 Review；日常 CI 不应静默改写 lockfile。

## 4. 统一命令

在仓库根目录：

```bash
make test       # 后端 pytest + 前端 Vitest
make lint       # Ruff + ESLint + TypeScript 类型检查
make smoke      # 只跑完全离线的后端垂直切片
make phase2-acceptance  # 隔离的真实 Compose 八场景可靠性验收
make format     # 运行项目约定的格式化器
```

格式化会修改文件；仅检查时使用下面的直接命令。提交 PR 前还应执行前端 production build。

### 4.1 后端

```bash
cd backend
uv run pytest -m "not integration"
uv run pytest -m smoke
uv run ruff check .
uv run ruff format --check .
```

`integration` marker 必须连接专用 PostgreSQL 与 Redis；缺少显式测试 DSN 时可以在普通本地套件中 skip，但基础设施 CI 和 Phase 2 验收禁止 skip：

```bash
cd backend
export LLMBENCHLAB_TEST_POSTGRES_URL='<dedicated-loopback-postgresql-dsn>'
export LLMBENCHLAB_TEST_REDIS_URL='redis://127.0.0.1:6379/15'
export LLMBENCHLAB_DATABASE_URL="$LLMBENCHLAB_TEST_POSTGRES_URL"
uv run python -m app.db.prepare_migrations
uv run alembic upgrade head
uv run alembic check
uv run pytest -m integration tests -ra
```

不要把真实 Provider Key 放入上述环境。这里的 PostgreSQL 必须是可破坏的专用测试库：lease integration fixture 会清空该 management database 的五张核心表。Importer 集成测试另在同一 loopback 服务器上创建正则约束的随机专用数据库并在结束时精确删除，不会替你保护或清空 management database。绝不能把开发、共享或生产数据库 DSN 传给这组命令。

常用的目标化命令：

```bash
cd backend
uv run pytest tests/test_evaluators.py
uv run pytest tests/test_dataset_loader.py
uv run pytest tests/test_api.py tests/test_smoke.py
uv run pytest -k 'multiple_choice or numeric'
```

需要本地查看覆盖率时可运行：

```bash
cd backend
uv run pytest --cov=app --cov-report=term-missing
```

MVP 暂未把单一覆盖率百分比当作质量替代品；关键错误路径和协议边界必须有明确断言。

### 4.2 前端

```bash
cd frontend
npm test
npm run lint
npm run typecheck
npm run build
```

交互开发可使用 `npm run test:watch`。组件测试运行在 jsdom；API 层应 stub/mock，不应依赖已启动后端或真实 Provider。生产构建输出到 `frontend/dist/`，该目录不应提交。

### 4.3 Compose 配置

```bash
docker compose config --quiet
```

这只验证配置渲染，不证明镜像已构建、migration 已完成或服务健康。普通启动验证使用 `make docker-up`、检查 `/api/v1/live`、`/health`、`/ready` 和 Worker probe，最后执行 `make docker-down`。真实故障验收使用 `make phase2-acceptance`；该脚本创建唯一 Compose project、随机 loopback 端口和隔离卷，失败路径也执行精确 `down -v` 并检查无项目残留。不得对日常项目名手工套用其清理命令。

## 5. 后端测试覆盖

### 5.1 Evaluator 单元测试

`test_evaluators.py` 至少覆盖：

- Exact Match：换行/Unicode 空白规范化、大小写配置、拒绝包含式或模糊匹配、空回答。
- Multiple Choice：`A`、`A.`、`(A)`、中英文最终答案表达；明确最终答案优先；冲突答案、自然语言随机字母和不在 choices 内的键不能误判。
- Numeric：整数、浮点、负数、科学计数法、`\boxed{}`、中英文最终答案；绝对/相对 tolerance；推理中多数字歧义；拒绝表达式、NaN 和 Infinity。
- Evaluator registry：数据集映射名称能解析为正确实现。

解析失败应断言 `score=0` 和稳定的 `parse_error`，不能通过放宽断言掩盖歧义。

### 5.2 Adapter 单元测试

`test_adapters.py` 覆盖：

- Mock 输出、Token、延迟、request ID 可预测且不进行 I/O。
- Mock 可注入分类错误，用于验证单题故障隔离。
- OpenAI-compatible 的 Chat Completions URL、messages 与 `temperature/top_p/max_tokens/seed`。
- usage 缺失时 Token 字段为 `null`。
- 429、选定 5xx、网络超时的有限指数退避，以及普通 4xx 不重试。
- Key 环境变量缺失、空 Provider 回答、非法配置与错误脱敏。

OpenAI-compatible 测试只给进程内 transport 使用虚构 token；不得把测试地址改为真实域名。

### 5.3 Dataset Loader 单元测试

`test_dataset_loader.py` 覆盖 manifest/JSONL Schema、错误行列和 JSON Pointer、UTF-8、重复键/ID、题数、Evaluator 兼容、NaN/Infinity、稳定 SHA-256，以及：

- ZIP 路径穿越、绝对/嵌套路径、重复/额外成员。
- symlink/非普通文件、加密或不支持压缩。
- archive/member/单行/题数上限与高压缩比。
- 内置 Demo 为 15 道原创双语题，含三种题型并带非正式标识。

新增数据格式字段、Hash 规则或限制时，必须先更新协议/Schema 文档，再增加兼容与拒绝测试；不能无版本变化地改变既有 Hash。

### 5.4 可靠执行单元与仓储测试

可靠执行测试必须把 PostgreSQL 中的 Run/Response 当作唯一事实，Redis 消息、日志和内存状态都不能覆盖它。当前离线测试覆盖：

- 两个领取者竞争同一 Run 时只有一个有效 owner/token；旧 token 的 heartbeat、逐题写入和 finalize 都被 fencing 拒绝。
- 每题 `(run_id, question_id)` 唯一；重复投递、ACK 结果未知、Worker 接管和重跑不会双写 Response 或重复增加进度。
- pending/running 取消、自然租约过期、完整证据直接聚合、有限退避与 attempt 耗尽 dead-letter。
- API 只在数据库 commit 后 best-effort 发布通知；commit 失败不 publish，Redis 发布失败仍保留可由数据库 reconciliation 找回的 pending Run。
- Worker 每次只执行一个 Run，按配置心跳；优雅停止在 grace 内等待，超时后由租约过期恢复，而不是伪造成功 ACK。
- API 进程不持有 Runner/task manager；只启动 API 时 Run 保持 `pending`，启动独立 Worker 后才执行。

SQLite 测试适合快速验证状态机和兼容路径；跨连接并发保证、锁和数据库时钟必须由真实 PostgreSQL 用例补充，不能用 SQLite 通过代替。

## 6. API、迁移与真实基础设施测试

### 6.1 API

`backend/tests/conftest.py` 在导入应用前设置一个临时目录中的 SQLite URL。每个 API client fixture 会重建表，因此测试不会读写开发数据库 `backend/data/llmbenchlab.db`。

当前 API 集成路径验证：

- `/live` 不访问数据库、Redis 或 Provider；`/health` 只检查数据库；`/ready` 分别报告数据库、Alembic head 与队列状态；`/info` 保持 `llmbenchlab-protocol-v1`。
- Redis 不可用时 `/ready` 返回脱敏的 `503 degraded`，但数据库正常时 `accepting_runs=true`、database reconciliation 可用；数据库或 schema 不可用时返回 `not_ready` 并停止接受新 Run。
- `X-Request-ID` 校验、回传与错误关联；未知路径只记录 `<unmatched>`，不把用户路径或请求正文写入应用日志。
- `/tasks/metrics` 只从数据库派生 pending/due/running/expired/cancel/retry/dead-letter/queue-notification-error/attempt gauges。
- Model CRUD、分页、Provider 必需字段和名称冲突。
- API 只返回 `api_key_env` 名称，不返回环境变量值。
- SQLAlchemy 基本 CRUD 与外键/Schema 基线。
- Run 创建 `202`、取消、轮询、逐题证据、汇总和排行榜；API 提交不在进程内执行 Adapter。

增加或修改路由时至少断言：成功状态码与 Schema、一项校验错误、404/409 等业务错误、分页/筛选（若适用），以及响应中不出现秘密值。API 行为改变必须同步更新 [API.md](API.md)。

### 6.2 Alembic 与遗留 SQLite

`backend/tests/test_migrations.py` 使用独立临时 SQLite 和 Alembic 子进程验证：

- 空库 upgrade/check/downgrade/upgrade 往返，以及 `20260825_0002` 最终 revision、可靠性字段、约束和索引。
- 有模型、Benchmark、题目、Run 与 Response 的 legacy schema 被一致性备份、严格识别并无损升级；题目按原插入顺序回填 0-based `position`。
- 与当前 metadata 一致但没有版本标记的库可安全收养，已有 head 重复 preflight 不生成多余备份。
- 部分表、server default/CHECK 内容或重名、PK/UNIQUE/FK/index/partial index、trigger、SQLite conflict policy/generated column、`STRICT`/`WITHOUT ROWID` 等未知 drift 在创建版本标记和备份前被拒绝；已在 head 的库同样验证。
- versioned legacy 的非法 Provider 配置数据会在任何 SQLite batch DDL 前失败，不残留临时重建表。
- `0001 -> 0002` 会按冻结的 Phase 1 语义收敛旧 `running` Run；存在 active Run 时可靠性 downgrade 被拒绝，不能静默删除租约元数据。
- 应用启动 revision 门禁拒绝未迁移库；测试夹具中的 `create_all` 仅用于隔离临时库，并显式 stamp 到与 metadata 对应的 head，不是运行时建表路径。

目标化运行：

```bash
cd backend
uv run pytest tests/test_migrations.py
```

真实 PostgreSQL `backend-integration` job 在空的专用 management database 上执行 `head -> 20260824_0001 -> head` 与 `alembic check`，验证 revision/DDL 可往返；它不提供业务数据保持证据。带数据的证据来自 Compose 验收：脚本先完成一个 15 题 Mock baseline Run，停止 API/Worker，再对该 Run 的协议 v1 核心字段及其 15 条 Response 在 head/`0001`/head 三个时点生成 canonical hash。父级 Model/Benchmark/Question 的存在由外键与迁移成功间接约束，但该 hash 不是全库快照。这个 schema downgrade 也不等于 PostgreSQL→SQLite 平台回迁。

### 6.3 SQLite→PostgreSQL 导入

`backend/tests/test_sqlite_postgres_import.py` 的离线路径验证 canonical hash、只读 SQLite、head/integrity/FK/active-Run 拒绝、五表复制及提交前回滚。标记为 `integration` 的真实 PostgreSQL 用例还验证：

- 随机专用空库成功导入后，五表行数、主键集合和 canonical row hash 与源一致，JSON、Decimal、UTC、协议快照和逐题证据保持。
- 中途复制失败整体回滚；两个不同源并发导入时恰好一个成功，另一个在目标非空检查处拒绝。
- `COMMIT` 确认丢失使用专用 `commit_outcome_unknown` 语义；已确认提交后的 snapshot 或输出失败使用 `committed_but_verification_failed`，两者都禁止盲目重试。
- 源 SQLite 主文件 hash 不变；输出只有阶段、表名、行数与 SHA-256 摘要，不打印题目、回答或连接 URL。
- 测试只在 loopback PostgreSQL 创建随机数据库，验证实际 `current_database()` 后才执行 truncate，并在 `finally` 精确 `DROP DATABASE ... WITH (FORCE)`。

完整运维步骤和 exit code 处理见 [DEPLOYMENT.md](DEPLOYMENT.md)。

### 6.4 健康、日志和指标的测试边界

- `/ready` 把同步数据库/head 检查放入 `asyncio.to_thread` 并设置异步等待上限，Redis ping 也有独立 timeout；测试证明半开依赖不会阻塞 `/live`。取消 `to_thread` 的等待不会终止底层数据库驱动调用，因此最终上界仍依赖 driver、连接池和 `connect_timeout`，不能把 asyncio timeout 当作强制中止。
- Worker `app.worker_probe` 是依赖能力探针：数据库/head 失败 exit 1；Redis 不可用但数据库 reconciliation 可用时输出 `degraded` 且 exit 0；配置错误 exit 1。它不观察 Worker 主事件循环或当前 heartbeat，不能证明进程没有卡死。
- LLMBenchLab 应用 logger 输出脱敏 JSON、request/correlation ID 与 allowlist 字段；异常只记录类型。Uvicorn 自身及 access logger 仍使用其原生 handler，不在“全部日志统一 JSON”的保证内。
- `/tasks/metrics` 是当下数据库 gauges，不是 Prometheus exporter，也不提供历史 counters、claim/heartbeat 延迟、trace、告警或完整审计。缺少的可观测能力仍是 Phase 2 后续工作。

## 7. 完全离线 Smoke Test

运行：

```bash
make smoke
```

等价目标命令：

```bash
cd backend
uv run pytest -m smoke
```

Smoke Test 实际执行以下链路：

1. 在临时 SQLite 中启动 FastAPI TestClient。
2. 注册 `provider_type=mock` 的模型。
3. 幂等载入 `demo-general`，断言 `question_count=15`、`is_demo=true`。
4. 创建 Run 并取得 `202` 与 Run ID；先断言 API-only 状态仍为 `pending`、attempt 0、0 Response，且应用没有进程内 task manager。
5. 显式运行独立 `WorkerService.run_once()`；该 Smoke 关闭 Redis，只通过数据库 reconciliation 领取。
6. 在有限期限内轮询到终态，不使用无限等待。
7. 断言 15 道题均有唯一 Response，三种题型都出现。
8. 断言严格总分、完成率和 answered accuracy 都在合法范围；当前确定性 Demo 预期为 100。
9. 断言排行榜出现该 Run 且保留 Demo 标记，Dashboard 汇总增加 completed Run。

另一个故障隔离测试给单题注入 Mock Adapter 错误，断言 Run 仍完成、失败题计 0、错误被持久化、其余题继续执行，严格总分为 `14/15*100`。

Smoke Test 证明 API 与 Worker 责任边界以及数据库驱动的最小离线链路；它仍在同一 pytest 进程内调用 WorkerService，不证明真实 PostgreSQL/Redis、操作系统进程重启、Docker 网络或生产高可用。后者必须由 infrastructure 与 Compose 验收补充。

## 8. 不调用真实 API 的机制

自动化安全依赖多层约束：

1. 所有端到端测试注册的 Provider 都是 `mock`；`MockModelAdapter.generate` 不执行网络 I/O。
2. OpenAI-compatible 协议测试向 Adapter 注入 `httpx.MockTransport`，响应在进程内生成。
3. 测试数据库和日志级别在应用导入前通过 fixture 环境变量设置，不读取开发 `.env`。
4. CI 不配置任何 Provider Key；即使误走 OpenAI-compatible 路径，缺少 `api_key_env` 对应值也会在 HTTP 请求前失败。
5. 测试数据中的 Key 与域名必须是明显无效占位符，不从开发者环境复制。

当前测试套件没有操作系统级的“禁止所有出站网络”沙箱，因此最后一层仍是代码 Review：任何新增测试若构造真实 `httpx` client、读取开发 Key 或依赖在线服务，都必须被拒绝。公开 CI 加固可在后续增加 egress-disabled runner 或网络拦截 fixture；不能因为 CI runner 通常没有 Key 就认定任意网络访问安全。

真实 OpenAI-compatible Provider 只允许作为用户主动执行、明确知晓费用和数据政策的可选手工验证；它不是 PR、CI、Smoke 或 Phase 2 可靠执行基础的完成条件。

## 9. 前端测试要求

当前有 4 个 Vitest 文件、13 个用例，实际自动化覆盖：

- 分数、百分比、Token 合计/未知值、费用、UTC 时间、Hash 与答案的格式化。
- `pending/running/completed/failed/cancelled` 五种状态标签。
- Dashboard 主页面的 API 加载、严格总分、完成率、Run/模型/Benchmark 汇总与最近运行。
- 后端不可达时的结构化、可重试错误状态。

所有 fetch 与 Recharts 均在进程内 stub，没有真实网络。这满足 MVP 的最低前端测试门槛，但下列交互目前仍由 production build、后端 Smoke 和手工验收覆盖，应在后续迭代补为组件测试：Models CRUD 表单、Benchmark Demo 标识、新建 Run 跳转、Run Detail 的 raw/parsed/reference/score/error 与终态停止轮询，以及 Leaderboard 分区筛选。不得把这份待补清单描述为已有自动化覆盖。

测试应优先按可见文本、label 和 role 查询 DOM，不依赖内部 class 或实现细节。时间、ID 和 API 返回应固定；不要用长 sleep 消除竞态。

## 10. CI

GitHub Actions 对 `main` push 和 Pull Request 触发四类 job：

| Job | 必需检查 | 隔离与失败规则 |
| --- | --- | --- |
| `backend` | Ruff lint/format；临时 SQLite `upgrade -> 0001 -> head`/check；`pytest -m "not integration"` | 临时 SQLite；不启动 PostgreSQL/Redis；离线 Mock/MockTransport |
| `backend-integration` | 真实 PostgreSQL migration 往返；PostgreSQL/Redis/importer 的 5 个 `integration` 用例 | Actions service 容器；JUnit 必须收集非零用例且零 skip，否则 job 失败 |
| `full-stack-reliability` | `python3 scripts/phase2_acceptance.py` 的隔离 Compose 八场景 | 唯一项目/卷、随机 loopback 端口、Mock-only；总是上传已脱敏 evidence，脚本总是精确清理 |
| `frontend` | ESLint、13 个 Vitest、production build（`tsc -b` + Vite） | `npm ci` 锁定依赖；fetch/Recharts stub；`VITE_API_BASE_URL=/api/v1` |

CI 不配置 Provider Key、不调用真实模型。PostgreSQL/Redis 是测试依赖，不是 Provider 网络。所有必需 job 通过后才能合并；跳过用例、降低断言或使用 `continue-on-error` 都不算修复。具体分支和 Review 门槛见 [GITHUB_WORKFLOW.md](GITHUB_WORKFLOW.md)。

本次 Phase 2 工作日志的阶段性证据与最终门禁预期如下；在最终提交前必须再次执行并核对，若数字或结果变化，应更新本表而不是沿用旧记录：

| 验证 | 当前记录/最终预期 |
| --- | --- |
| 后端本地非基础设施 | `205 passed`，命令 `uv run pytest -m "not integration"` |
| 真实 PostgreSQL/Redis infrastructure | `5 passed, 0 skipped` |
| 前端 | `4 files, 13 passed`；lint/typecheck/build 分别通过 |
| 离线 Smoke | `1 passed`（其余非 smoke 用例 deselected） |
| Compose 可靠性 | `8/8 passed`，最终 Redis consumer group `pending=0`、`lag=0`，清理后无项目容器/卷/网络 |

这些数字证明“可靠任务执行基础”垂直切片，不代表 Phase 2 全部完成；限流、预算、完整背压、公平调度、完整历史可观测性/审计和性能基线仍未验收。

## 11. Mock 手工验收

这套验收不需要网络模型。推荐运行：

```bash
make dev
```

它同时启动 API、独立 Worker 和 Vite。若拆分终端，必须分别运行 `make backend`、`make worker` 与 `make frontend`；只启动 API 时新 Run 保持 `pending` 是预期行为。

### 11.1 API 验收

1. 访问健康、就绪、任务 gauges 与能力信息：

   ```bash
   curl -sS http://127.0.0.1:8000/api/v1/live
   curl -sS http://127.0.0.1:8000/api/v1/health
   curl -sS http://127.0.0.1:8000/api/v1/ready
   curl -sS http://127.0.0.1:8000/api/v1/tasks/metrics
   curl -sS http://127.0.0.1:8000/api/v1/info
   ```

   默认本地 `REDIS_URL` 为空时 queue 显示 `disabled`，上述请求均为 `200`，且不出现 Provider 请求。若显式配置的 Redis 不可用，`ready` 为脱敏 `503 degraded`；数据库正常时仍显示 `accepting_runs=true`。

2. 注册 Mock：

   ```bash
   curl -sS -X POST http://127.0.0.1:8000/api/v1/models \
     -H 'Content-Type: application/json' \
     -d '{"name":"Manual Offline Mock","provider_type":"mock","enabled":true}'
   ```

   保存返回的 `id`，确认响应无 Key 值。

3. 载入 Demo：

   ```bash
   curl -sS -X POST http://127.0.0.1:8000/api/v1/benchmarks/reload-demo
   ```

   保存 Benchmark `id`，确认 `question_count=15`、`is_demo=true`，描述含“Demo 数据，不代表正式模型能力”。

4. 把下面两个占位 ID 替换为上两步返回值并创建 Run：

   ```bash
   curl -sS -X POST http://127.0.0.1:8000/api/v1/runs \
     -H 'Content-Type: application/json' \
     -d '{"model_id":"<MODEL_ID>","benchmark_id":"<BENCHMARK_ID>","temperature":0,"top_p":1,"max_tokens":64,"seed":42,"concurrency":1}'
   ```

   预期 `202`；保存 Run `id`。

5. 轮询直到 `completed`：

   ```bash
   curl -sS 'http://127.0.0.1:8000/api/v1/runs/<RUN_ID>'
   ```

   预期 15/15 完成、无错误、严格总分/完成率/回答准确率均为 100。若未完成，检查 `status` 和 `error_message`，不要无限轮询。

6. 检查逐题、排行榜和汇总：

   ```bash
   curl -sS 'http://127.0.0.1:8000/api/v1/runs/<RUN_ID>/responses?limit=100'
   curl -sS 'http://127.0.0.1:8000/api/v1/leaderboard?benchmark_id=<BENCHMARK_ID>&order=score_desc'
   curl -sS http://127.0.0.1:8000/api/v1/metrics/summary
   ```

   预期 Responses 共 15 条，排行榜包含该 Run 且 `is_demo=true`，汇总包含 1 个已完成 Run。

### 11.2 前端验收

打开 `http://127.0.0.1:5173`，按顺序检查：

- Dashboard 的模型、Benchmark、Run、得分/延迟/Token 汇总与最近运行来自 API，而非固定假数据。
- Models 能新增、编辑、删除 Mock；表单不出现明文 Key 输入框，OpenAI-compatible 只要求环境变量名。
- Benchmarks 能重载 Demo、显示版本/题数/Hash/许可证与醒目的 Demo 警告。
- New Run 能选择 Mock 与 Demo，生成参数约束正确，提交后跳转详情。
- Run Detail 轮询进度并在终态停止；配置快照和 15 条逐题证据可查看。
- Leaderboard 可按模型/Benchmark 筛选、按得分排序，协议、Hash、完成率和 Demo 标识可见。
- 刷新页面、空数据库、API 关闭和常见移动宽度下都有明确可操作状态。

### 11.3 取消与重启限制

可额外创建一个 Run 并立即调用 `POST /runs/{id}/cancel`，确认 pending 会直接取消、running 会在题目/心跳边界收敛，重复取消不改变终态。Mock 很快，可能在取消前已经完成，这也是合法竞态。

API 重启不拥有或取消 Worker 的租约。Worker 正常停止会在配置的 grace 内等待活动 Run；超时或异常退出后不会让旧进程继续写入，peer Worker 只能在数据库租约自然过期后用新 token 接管。不要在本地 SQLite/单 Worker 手工 kill 一次就宣称恢复通过；可复核证据必须来自下一节的 PostgreSQL 双 Worker 脚本。

## 12. Phase 2 真实 Compose 故障验收

从仓库根目录运行：

```bash
make phase2-acceptance
```

脚本使用标准库编排真实 PostgreSQL 16、Redis 7、一次性 migrate、API、两个 Worker replica 与 frontend（六个 service 定义）；只运行确定性 Mock，不读取 Provider Key。它创建正则约束的唯一 Compose project、随机 `127.0.0.1` API/frontend 端口和隔离 named volumes，证据写入 Git 已忽略的 `.pytest_cache/artifacts/phase2-acceptance/<project>/evidence.json`。无论成功或失败都执行该项目的精确 `down -v`，并验证容器、卷、网络没有残留。

八个必需场景是：

1. 六服务 Compose 拓扑、migrate exit 0、API/frontend/依赖健康与仅 loopback 暴露。
2. `llmbenchlab-protocol-v1` 基线：15 个唯一 Response，score/completion/answered accuracy 为 100，Token 120/30、cost 0。
3. Run 执行中重启 API；Worker owner/token 和最终协议证据不变。
4. 精确定位实际 lease owner Worker 并发送 SIGKILL；数据库保留旧租约和已提交 Response，peer 只在自然过期后以 token +1 接管，不覆盖旧证据。
5. Redis 完全 stop/start；`live`/`health` 保持可用、`ready` 降级，API 仍以 `202` 提交数据库事实，Worker 仅靠 DB reconciliation 完成；Redis 恢复后新消息正常 ACK。
6. Worker 停止时取消 pending Run；Worker 恢复消费旧通知后终态和 0 Response 不漂移。
7. 运行中取消并再次 XADD 同一 Run；Response 数在取消后冻结，重复投递被 ACK 且 canonical snapshot 不变。
8. 停止 API/Worker 后执行 PostgreSQL `head -> 20260824_0001 -> head`；同一个 15 题 baseline Run 的协议 v1 核心字段及其 15 条 Response 在三个时点的 canonical hash 相同。该 hash 不是全库快照。

任何一个场景失败、未运行、使用真实 Provider、最终 PEL/lag 非零或清理不完整，都不能把可靠执行基础写成通过。`--self-check-only` 只验证 Docker/Compose、隔离和清理 guard，不执行八场景，不能替代正式命令。

## 13. 失败排查与完成证据

- 先单独复现最小失败测试，再运行完整层级；保留第一个有因果信息的 traceback。
- SQLite lock、PostgreSQL/Redis 测试 DSN、端口占用或残留环境变量是测试隔离问题，不能通过重试掩盖。
- 快照变化需人工确认是有意 API/协议变化；不得无条件更新 expected output。
- 前端测试通过但 build 失败时任务仍未完成；lint、typecheck、test、build 是不同门槛。
- Docker 不可用时记录 `docker compose config`/启动未运行及原因；不能因此把 Compose 场景写成通过。
- 最终证据应列出工作目录、命令、测试数、耗时和失败数；CI 链接只能补充，不能替代本地实际结果说明。
- evidence 与日志必须先做敏感值检查；不得记录 DSN 密码、Redis URL、Authorization、题目、原始回答或 Provider 正文。Hash 和行数仍属于运维元数据，应按评测数据保护。
