# LLMBenchLab 部署与运行

## 1. 当前定位

LLMBenchLab 当前有两条单机运行路径：

- 本地开发兼容路径：SQLite、可选 Redis、FastAPI API、独立 Worker 和 Vite。它便于开发与离线 Mock 验收，但 SQLite 只支持单 Worker 低并发，不能替代 PostgreSQL 并发证据。
- Phase 2 Compose 可靠执行路径：PostgreSQL 是唯一任务事实来源，Redis Streams 是 at-least-once 通知层，API 与 Worker 是独立进程，migrate 是唯一 Alembic upgrade owner，Nginx 提供前端。

可靠执行基础已经覆盖租约、心跳、fencing、幂等 Response、取消、有限重试、数据库 reconciliation 和故障恢复；`llmbenchlab-protocol-v1` 评分含义没有改变。Phase 2 总状态仍为 `in_progress`：Provider/Model/Run 级限流、预算、完整背压与公平调度、完整历史可观测性/审计、性能基线和生产高可用尚未完成。

> Compose 是本地故障验证配置，不是生产方案。它没有认证、授权、TLS、正式 secret manager、自动备份/PITR、告警或 HA；不要直接暴露公网，也不要把示例密码当作生产秘密。

## 2. 地址、进程与数据速查

| 模式 | Web | API | 数据/队列 | 说明 |
| --- | --- | --- | --- | --- |
| 本地 Make | `http://127.0.0.1:5173` | `http://127.0.0.1:8000` | `backend/data/llmbenchlab.db`；Redis 可选 | `make dev` 启动 API、独立 Worker、frontend；默认 loopback |
| Docker Compose | `http://127.0.0.1:8080` | `http://127.0.0.1:8000` | `postgres-data`、`redis-data` named volumes | API/frontend 仅 loopback；PostgreSQL/Redis 无 host port |

API 系统端点：

- `/api/v1/live`：仅 API 进程 liveness，不访问数据库、Redis 或 Provider。
- `/api/v1/health`：兼容端点，只检查数据库连接。
- `/api/v1/ready`：检查数据库、Alembic head 与 Redis，返回组件化、脱敏状态。
- `/api/v1/tasks/metrics`：数据库派生的当前任务 gauges。
- `/docs`：OpenAPI UI。

前端容器的 `/healthz` 只表示 Nginx/静态站点可响应。

## 3. 本地开发运行

### 3.1 前置要求与初始化

- Python 3.11 或更新版本。
- `uv`。
- Node.js 22 或兼容版本与 npm。
- Git；Docker 只在 Compose 和 Phase 2 真实故障验收时需要。

从仓库根目录运行：

```bash
make setup
```

脚本只在 `.env` 不存在时复制 `.env.example`，按 lockfile 安装依赖，执行安全迁移 preflight，并将本地 SQLite 升级到 Alembic head。已有 `.env` 不会覆盖；`.env`、数据库、WAL/SHM 与自动收养备份都被 Git 忽略。

### 3.2 启动 API、Worker 与前端

```bash
make dev
```

`scripts/dev.sh` 同时管理三个进程；任一进程退出会停止另外两个。需要分开查看日志时使用三个终端：

```bash
make backend
```

```bash
make worker
```

```bash
make frontend
```

只启动 `make backend` 时，API 可以提交 Run，但没有进程内 Runner；新 Run 保持 `pending`，直到独立 Worker 启动。默认 `REDIS_URL` 为空时 Worker 仍会扫描数据库并执行到期 Run，Redis 只是可选低延迟通知层。

SQLite 本地路径只允许单 Worker。不要启动多个 SQLite Worker，也不要用 SQLite kill/restart 代替真实 PostgreSQL 并发验收。

### 3.3 直接运行子项目

统一 Make 命令应是首选。排障时可显式运行：

```bash
set -a
source ./.env
set +a
cd backend
uv sync --frozen --extra dev
uv run python -m app.db.prepare_migrations
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另一个终端载入同一非秘密配置后启动 Worker：

```bash
set -a
source ./.env
set +a
cd backend
uv run python -m app.worker
```

前端：

```bash
cd frontend
npm ci
VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1 npm run dev -- --host 127.0.0.1
```

相对 SQLite URL 根据当前工作目录解析；仓库脚本先进入 `backend/`，所以 `sqlite:///./data/llmbenchlab.db` 指向 `backend/data/llmbenchlab.db`。

## 4. 环境变量

Pydantic 应用设置优先读取 `LLMBENCHLAB_*`，并为数据库、Redis、CORS 和日志保留短别名。根脚本会载入未提交的 `.env`。

### 4.1 数据库、队列与 Worker

| 变量 | 默认/示例 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` / `LLMBENCHLAB_DATABASE_URL` | `sqlite:///./data/llmbenchlab.db` | SQLAlchemy URL；Compose 改为内部 PostgreSQL |
| `REDIS_URL` / `LLMBENCHLAB_REDIS_URL` | 空 | 空表示关闭队列并只用 DB reconciliation；Compose 使用内部 Redis |
| `LLMBENCHLAB_DATABASE_POOL_SIZE` | `5` | 数据库 pool size |
| `LLMBENCHLAB_DATABASE_MAX_OVERFLOW` | `5` | 数据库 pool overflow |
| `LLMBENCHLAB_DATABASE_POOL_TIMEOUT_SECONDS` | `30` | 连接池等待上限；Compose 默认压缩为 2 秒 |
| `LLMBENCHLAB_READINESS_DATABASE_TIMEOUT_SECONDS` | `5` | `/ready` 等待 DB/head 线程结果的异步上限；不是驱动强制取消 |
| `LLMBENCHLAB_TASK_STREAM` | `llmbenchlab:runs:v1` | Redis Stream 名称 |
| `LLMBENCHLAB_TASK_CONSUMER_GROUP` | `llmbenchlab-workers-v1` | Consumer Group 名称 |
| `LLMBENCHLAB_TASK_STREAM_MAX_LENGTH` | `10000` | Stream 近似裁剪上限；不能作为恢复正确性前提 |
| `LLMBENCHLAB_REDIS_MAX_CONNECTIONS` | `10` | Redis 连接池上限 |
| `LLMBENCHLAB_REDIS_PUBLISH_TIMEOUT_SECONDS` | `1` | API XADD 等待上限 |
| `LLMBENCHLAB_REDIS_OPERATION_TIMEOUT_SECONDS` | `2` | ping/read/ACK 等操作上限；Compose 默认 1 秒 |
| `LLMBENCHLAB_REDIS_BLOCK_MILLISECONDS` | `1000` | Worker 阻塞读取上限 |
| `LLMBENCHLAB_WORKER_LEASE_SECONDS` | `30` | Run 租约时长 |
| `LLMBENCHLAB_WORKER_HEARTBEAT_SECONDS` | `10` | 心跳周期；必须不大于 lease 的一半 |
| `LLMBENCHLAB_WORKER_POLL_SECONDS` | `1` | DB reconciliation 周期 |
| `LLMBENCHLAB_WORKER_MAX_ATTEMPTS` | `3` | 新 Run 的最大执行 attempt |
| `LLMBENCHLAB_WORKER_RETRY_BACKOFF_BASE_SECONDS` | `1` | 重试退避基数 |
| `LLMBENCHLAB_WORKER_RETRY_BACKOFF_CAP_SECONDS` | `30` | 重试退避上限 |
| `LLMBENCHLAB_WORKER_SHUTDOWN_GRACE_SECONDS` | `30` | SIGTERM 后等待活动 Run 的应用 grace |
| `LLMBENCHLAB_MOCK_GENERATION_DELAY_SECONDS` | `0` | 只用于确定性 Mock 故障测试；不改变报告 latency 或协议评分 |

配置校验要求 `heartbeat * 2 <= lease`，退避 base 不得大于 cap。不要为了让超时测试通过而把生产时间参数直接套到验收脚本；`phase2_acceptance.py` 使用隔离的短租约配置。

### 4.2 API、前端与日志

| 变量 | 默认/示例 | 说明 |
| --- | --- | --- |
| `CORS_ORIGINS` / `LLMBENCHLAB_CORS_ORIGINS` | 两种 localhost `:5173` | 显式 allowlist；拒绝 `*` |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | 单 Origin 兼容别名；`CORS_ORIGINS` 优先 |
| `LOG_LEVEL` / `LLMBENCHLAB_LOG_LEVEL` | `INFO` | `CRITICAL/ERROR/WARNING/INFO/DEBUG` |
| `LLMBENCHLAB_ENVIRONMENT` | `development` | `/info` 环境标签 |
| `LLMBENCHLAB_DEBUG` | `false` | 只用于受控本地调试 |
| `API_HOST` / `API_PORT` | `127.0.0.1` / `8000` | Make/脚本监听；Compose 只消费 host port |
| `FRONTEND_HOST` / `FRONTEND_PORT` | `127.0.0.1` / `8080` | Vite host / Compose Nginx host port |
| `VITE_API_BASE_URL` | `http://localhost:8000/api/v1` | 浏览器公开的编译期值，绝不能放秘密 |

### 4.3 Compose 插值

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLMBENCHLAB_IMAGE_TAG` | `local` | backend/migrate/worker 共用镜像标签 |
| `LLMBENCHLAB_COMPOSE_DATABASE_URL` | 内部 `postgres:5432/llmbenchlab` | 覆盖应用 DSN；默认密码只用于本地隔离 Compose |
| `LLMBENCHLAB_COMPOSE_REDIS_URL` | `redis://redis:6379/0` | 内部队列地址 |
| `LLMBENCHLAB_COMPOSE_WORKER_LEASE_SECONDS` | `30` | 映射到容器 Worker lease |
| `LLMBENCHLAB_COMPOSE_WORKER_HEARTBEAT_SECONDS` | `10` | 映射到 heartbeat |
| `LLMBENCHLAB_COMPOSE_WORKER_POLL_SECONDS` | `1` | 映射到 reconciliation |
| `LLMBENCHLAB_COMPOSE_WORKER_SHUTDOWN_GRACE_SECONDS` | `30` | 应用 grace；容器另有 45 秒 stop grace |
| `LLMBENCHLAB_COMPOSE_REDIS_OPERATION_TIMEOUT_SECONDS` | `1` | 容器 Redis 操作 timeout |
| `LLMBENCHLAB_COMPOSE_DATABASE_POOL_TIMEOUT_SECONDS` | `2` | 容器连接池 timeout |
| `LLMBENCHLAB_COMPOSE_READINESS_DATABASE_TIMEOUT_SECONDS` | `2` | 容器 readiness 等待上限 |
| `LLMBENCHLAB_COMPOSE_MOCK_GENERATION_DELAY_SECONDS` | `0` | 可靠性测试专用 Mock delay |

### 4.4 OpenAI-compatible Key

模型表只保存环境变量名，例如 `LOCAL_COMPAT_API_KEY`。真正调用 Adapter 的是 Worker，因此秘密必须注入 Worker 进程；只给 API 注入不会生效。`make dev` 会让三个本地进程继承同一 `.env`，但真实部署应按进程最小权限拆分。

Compose 默认不注入任何 Provider Key。用户主动手工验证时使用未提交 override 或 secret 系统，只给 Worker 注入选定变量：

```yaml
services:
  worker:
    environment:
      LOCAL_COMPAT_API_KEY: ${LOCAL_COMPAT_API_KEY:?set_in_controlled_shell}
```

不要提交 override，不要把展开后的 `docker compose config` 发到公共日志，也不要把 Key 写入 `VITE_*`、Model API JSON、数据库或命令行。自动化、CI、Smoke 与 Phase 2 验收禁止调用真实 Provider。

## 5. 数据库、队列与 Alembic

### 5.1 事实来源与恢复边界

- PostgreSQL 中的 Run 状态、取消意图、attempt、租约、Response、聚合、错误和 dead-letter 是唯一权威事实。
- Redis Stream 消息只包含版本、`run_id` 和 correlation ID。通知丢失、重复或 ACK 不确定不能删除或改变数据库事实。
- API 先提交数据库，再 best-effort XADD。Redis 失败时仍返回已持久化 Run；Worker 定期扫描数据库并恢复。
- 每个执行写入校验 owner 与单调 `lease_token`；终态清除 owner/expiry/heartbeat，旧 token 永久失效。

Redis 开启 AOF (`appendfsync everysec`) 只改善通知持久性，不是备份，也不能取代 PostgreSQL。

### 5.2 Migration chain

- `20260824_0000`：可执行 legacy schema。
- `20260824_0001`：Phase 1 schema、模型约束与题目 position。
- `20260825_0002`：attempt、租约、心跳、backoff、queue audit 与 dead-letter 字段/约束/索引。

本地 SQLite 更新：

```bash
make migrate
```

命令先执行 `app.db.prepare_migrations`，再 `alembic upgrade head`。受支持的未版本化 SQLite 会在严格结构/integrity/FK 检查和一致性备份后 stamp；未知 drift 在写 revision 前拒绝。普通 API/Worker 启动只检查 head，不运行 `create_all`、preflight 或 upgrade。

Compose 中只有一次性 `migrate` 服务执行：

```text
python -m app.db.prepare_migrations && alembic upgrade head && alembic check
```

`api` 与 `worker` 必须等待 migrate exit 0，然后仅执行 head check。不要把 Alembic 命令加回 API/Worker entrypoint，也不要同时运行多个 migration owner。

`0002 -> 0001` downgrade 在发现 `pending` 或 `running` Run 时拒绝；它会删除可靠性元数据但保留五类核心实体与协议证据。它不是 PostgreSQL→SQLite 反向同步，也不恢复 Phase 1 进程内 Runner。

### 5.3 备份与恢复证据边界

仓库当前没有自动 PostgreSQL 备份、PITR、跨主机灾难恢复或经过记录的生产恢复演练。升级、导入、truncate、volume 删除或 schema downgrade 前，操作方必须按自己的 PostgreSQL/SQLite 平台创建并验证备份；不能把“volume 存在”或本地导入测试写成恢复演练通过。

SQLite 自动收养生成的 `.bak` 只保护该 preflight 窗口，不是长期备份策略。Redis volume/AOF 也不是任务事实备份。

## 6. Docker Compose 六服务拓扑

Compose 定义六个 service，其中五个常驻，`migrate` 为一次性任务：

| Service | 角色 | 启动/健康语义 |
| --- | --- | --- |
| `postgres` | PostgreSQL 16，任务和评测唯一事实来源 | `pg_isready`；`postgres-data`；无 host port |
| `redis` | Redis 7 Streams 通知层，AOF everysec | `redis-cli ping`；`redis-data`；无 host port |
| `migrate` | 唯一 Alembic preflight/upgrade/check owner | 等 PostgreSQL healthy；成功后 exit 0，不常驻 |
| `api` | FastAPI CRUD、Run commit 与 best-effort publish | 等 migrate 成功；启动只 head check；`/ready` 为容器 health；loopback API port |
| `worker` | 独立租约 Worker、DB reconciliation、Redis consume/ACK | 等 migrate 成功；启动只 head check；dependency probe；容器 stop grace 45 秒 |
| `frontend` | Nginx 静态站与 `/api/` 同源代理 | 等 API healthy；loopback frontend port |

### 6.1 启动、检查和停止

```bash
make docker-up
```

该命令执行 `docker compose up --build --wait --wait-timeout 180 --remove-orphans`。检查：

```bash
docker compose ps -a
docker compose logs migrate
docker compose logs api
docker compose logs worker
curl -sS http://127.0.0.1:8000/api/v1/live
curl -sS http://127.0.0.1:8000/api/v1/health
curl -sS http://127.0.0.1:8000/api/v1/ready
curl -sS http://127.0.0.1:8000/api/v1/tasks/metrics
curl -sS http://127.0.0.1:8080/healthz
```

`migrate` 应显示成功退出；它不是故障容器。停止并保留 PostgreSQL/Redis volumes：

```bash
make docker-down
```

`docker compose down -v` 会删除 `postgres-data` 与 `redis-data`，属于破坏性操作。除隔离验收脚本管理的唯一项目外，不要自动执行；仓库没有可据此宣称安全恢复的备份演练。

### 6.2 网络与安全

API 与 frontend 明确绑定 `127.0.0.1`；PostgreSQL/Redis 只在 Compose 内部网络，无宿主端口。默认 PostgreSQL 密码 `llmbenchlab-local-only` 与 CI 密码都只是隔离测试固定值，不满足生产 secret 管理。

Loopback 绑定不能提供用户隔离；宿主机上的其他进程仍可访问。Compose 没有 TLS、鉴权、网络策略、容器只读文件系统、正式证书或多租户权限，不能直接部署到共享服务器/公网。

### 6.3 Worker 停止与故障

Worker 收到 SIGTERM 后在应用 `LLMBENCHLAB_WORKER_SHUTDOWN_GRACE_SECONDS` 内等待活动 Run；Compose 给容器 45 秒 stop grace。若应用 grace 先耗尽，它取消本地 task，不 ACK 未安全收敛的消息，数据库租约保留到自然过期，由 peer 以新 token 接管。

强制 SIGKILL 不会立即转移 owner，也不允许 peer 提前覆盖。Phase 2 验收精确杀死实际 lease owner，并证明 peer 在数据库 expiry 之后才接管。生产环境仍需滚动排空、Pod disruption、告警与容量策略。

### 6.4 Redis 故障

Redis 不可用时：

- `/ready` 返回 `503 degraded` 和 `queue_unavailable`，但数据库/head 正常时 `accepting_runs=true`。
- `POST /runs` 仍先提交 PostgreSQL 并返回 `202`；Run 记录稳定的 queue notification error。
- Worker 保持 DB reconciliation，可完成到期 Run；Redis 恢复后重新初始化 consumer group/消费。

因此不能用 `/ready=503` 推断所有 Run 创建都应被拒绝，也不能把 Redis 当作结果数据库。

## 7. SQLite→PostgreSQL 单向导入 runbook

导入器是显式、一次性的五表复制，不会自动在应用启动时运行，也不会反向同步。

### 7.1 前置条件

1. 停止源 SQLite 的 API、Worker 和所有写进程；同时停止目标 API/Worker 和用户入口。导入器只能保证自身连接只读，不能证明外部写进程已停止。
2. 按组织要求创建并独立验证源与目标备份。仓库没有生产备份/恢复演练，不得把本 runbook 描述为已验证灾难恢复。
3. 源必须是文件型 SQLite、处于当前 Alembic head；不能有 `pending` 或 `running` Run。
4. 目标必须是当前 head 的 PostgreSQL，五张核心表必须为空。先由唯一 migrate owner 完成 schema，再保持 API/Worker 停止。
5. 在受信环境运行；源可能包含题目、参考答案、原始模型输出和错误内容。保护源文件、终端输出和摘要日志。

导入器以 SQLite URI `mode=ro` 打开源、设置 `PRAGMA query_only=ON`，在显式读事务内执行 `integrity_check`、`foreign_key_check`、head/no-active 检查与 snapshot。任何一项失败都会在接触目标数据前停止。

### 7.2 准备空的 Compose 目标

在尚未启动 API/Worker 的 Compose 项目中：

```bash
docker compose up -d postgres redis
docker compose build migrate
docker compose run --rm migrate
```

如果目标栈已经运行，先停止入口和执行进程，再确认没有 active Run：

```bash
docker compose stop frontend api worker
docker compose run --rm migrate
```

第二条命令只负责把 schema 确认到 head，不会清空已有业务表；非空目标会由 importer 拒绝。不要为通过 preflight 而执行 truncate。

### 7.3 运行导入

通用本地命令应从受控环境变量读取 credentialed DSN，避免密码进入 argv：

```bash
cd backend
export LLMBENCHLAB_IMPORT_TARGET_URL='<credentialed-postgresql-dsn-from-secret-store>'
uv run python -m app.db.import_sqlite \
  --source /absolute/path/to/llmbenchlab.db \
  --target-env LLMBENCHLAB_IMPORT_TARGET_URL
```

不要把示例占位符替换后提交到 shell history、文档或日志。`--target` 仅允许 passwordless PostgreSQL URL；若 userinfo 或 query 含 password，CLI 在连接前拒绝。也可用受控 `PGPASSFILE` 或 libpq service，让 argv 仍不含秘密。

对内部 Compose PostgreSQL，可用 migrate 镜像作为一次性维护容器，并把源文件只读挂载：

```bash
docker compose run --rm \
  --volume /absolute/path/to/llmbenchlab.db:/import/source.db:ro \
  migrate python -m app.db.import_sqlite \
  --source /import/source.db \
  --target-env DATABASE_URL
```

执行时仍必须保持 API/Worker 停止。不要把整个源目录可写挂载到容器。

### 7.4 原子性、锁和对账

目标流程在一个 PostgreSQL 事务内：

1. 获取固定 transaction advisory lock，串行化两个 importer。
2. 检查 head，随后对 `alembic_version` 与五张核心表获取 `ACCESS EXCLUSIVE` lock，再次检查 head 与空表。
3. 按依赖顺序复制 `models`、`benchmarks`、`questions`、`evaluation_runs`、`evaluation_responses`。
4. 提交前比较行数、主键集合 SHA-256 与 canonical row SHA-256；失败整体 rollback。
5. COMMIT 成功确认后，在单个 `REPEATABLE READ`、`READ ONLY` 事务中做 post-commit snapshot。

成功输出三组、每组五行的 content-free 摘要：`phase=source`、`phase=precommit_target`、`phase=postcommit_target`。每张表的 `row_count`、`pk_set_digest` 和 `canonical_row_digest` 必须三阶段一致。摘要不打印行内容或 URL，但行数/hash 仍是敏感运维元数据。

### 7.5 Exit code 与恢复动作

| Exit | 状态 | 数据语义 | 必需动作 |
| --- | --- | --- | --- |
| `0` | completed and reconciled | COMMIT 已确认，post-commit 摘要匹配 | 保存脱敏摘要；检查 head/ready 后再启动 API/Worker |
| `2` | pre-commit failure | preflight/copy/提交前对账失败；目标事务若已开始会 rollback | 保留错误与源；确认目标仍为空并修复原因后，才考虑重新执行 |
| `4` | `commit_outcome_unknown` | PostgreSQL 未确认 COMMIT；原子事务意味着目标可能为空，也可能已完整提交 | 立即停止；保持应用停机，检查目标五表和摘要；禁止盲目重试、truncate 或覆盖 |
| `3` | `committed_but_verification_failed` | COMMIT 已确认，但 post-commit snapshot/比较或摘要输出未完成 | 将目标视为已提交；保持只读检查并补做对账；禁止重新导入或清空 |

exit 4 时，只有在独立检查证明五表仍为空后才可按新变更重新运行；若非空，按“可能已完整提交”保护现场。exit 3 已明确提交，不得把它当成 rollback。任何不确定状态都应升级给数据库负责人，而不是靠重复命令猜测。

### 7.6 导入后与回退

exit 0 后确认三阶段摘要、Alembic head 和**实际导入的目标环境**。如果目标就是第 7.2 节已停止的同一 Compose project，使用与导入时相同的 project 选择、环境插值和 `LLMBENCHLAB_COMPOSE_DATABASE_URL` 恢复该栈；默认 project 可运行：

```bash
make docker-up
curl -sS http://127.0.0.1:8000/api/v1/ready
```

如果 `LLMBENCHLAB_IMPORT_TARGET_URL` 指向外部/托管 PostgreSQL，不要运行上述命令来“恢复”它：Compose 不读取这个 importer 专用变量，默认会启动另一套本地 PostgreSQL。应通过该外部环境自己的部署流程，把 API、Worker 和唯一 migration owner 配置到刚核验过的同一 DSN，再启动服务并检查其 `/ready`、Alembic head 与数据库身份。启动前应保留导入摘要和只读核验结果，避免仅凭主机名或环境变量名称判断目标一致。

PostgreSQL 上后续产生的数据不会自动写回 SQLite。平台回退只能使用迁移前冻结的 SQLite 源/经独立验证的备份，或另行设计并验证导出工具；Alembic schema downgrade 不是反向数据迁移。Redis 可以重建，因为它不保存权威事实，但这不等于 PostgreSQL 可丢弃。

## 8. Health、日志、指标与 probe 边界

### 8.1 API liveness/readiness

| 端点 | 检查 | 失败语义 |
| --- | --- | --- |
| `/live` | API 进程可响应；返回应用版本与 UTC 时间 | 不探测外部依赖；数据库/Redis 全断仍可 200 |
| `/health` | 数据库 `SELECT 1` | 数据库失败 503；不检查 Alembic head 或 Redis |
| `/ready` | DB `SELECT 1`、Alembic head、Redis ping（若配置） | DB/schema 失败为 `not_ready`/不接受 Run；仅 Redis 失败为 `degraded`/仍接受 Run |

`/ready` 用 `asyncio.to_thread` 执行同步数据库/head 检查，并以 `LLMBENCHLAB_READINESS_DATABASE_TIMEOUT_SECONDS` 限制等待；Redis 有独立 operation timeout。asyncio timeout 只能停止等待，不能杀死已经运行的数据库 driver 线程，因此实际资源占用还受 PostgreSQL `connect_timeout`、SQLAlchemy pool timeout 和驱动行为约束。不要据此宣称硬实时 timeout。

Compose API healthcheck 使用 `/ready`，所以 Redis 停止时容器会显示 unhealthy，即使数据库 reconciliation 仍可接受和完成 Run；运维告警必须读取组件字段而不是只看一个颜色。

### 8.2 Worker probe

```bash
docker compose exec worker python -m app.worker_probe
```

probe 检查数据库连接、Alembic head 与队列能力：DB/head 或队列配置错误 exit 1；Redis 运行时不可用但 DB reconciliation 可用时输出 `degraded` 且 exit 0。它是依赖 capability/readiness probe，不观察 Worker 主循环、当前 lease heartbeat、事件循环卡死或执行吞吐，不能称为完整 Worker liveness。

### 8.3 结构化日志

LLMBenchLab 应用 logger 输出单行脱敏 JSON，包含 allowlist event、request/correlation ID、run/question、worker、attempt、lease token、message ID、结果和异常类型。它不应记录 Authorization、DSN/Redis URL、请求正文、Provider 请求/响应正文、完整题目或原始模型输出。API 接受安全的 `X-Request-ID`；Run correlation 默认稳定使用 Run ID。

这个保证只覆盖 LLMBenchLab 配置的应用 logger。Uvicorn/error/access handler 仍可能使用原生日志格式；不得把当前实现描述为所有容器日志统一 JSON。秘密也不能放入 URL，因为反向代理/access logger 可能记录 URL。

### 8.4 Task metrics

`/api/v1/tasks/metrics` 从 PostgreSQL 当前行派生：pending、due pending、running、expired running、active cancellation、retry scheduled、dead-lettered、queue notification error 和 total attempts。它是只读 gauges，不参与调度、不覆盖数据库状态。

当前没有 Prometheus exporter、持久历史 counter、claim conflict/heartbeat/queue error 时序、恢复延迟 histogram、trace、告警或完整审计流。生产监控和 Phase 2 P2-06 仍需补齐。

## 9. 升级、重启与回滚

### 9.1 升级顺序

1. 阅读 Changelog、ADR 和 migration；确认协议版本没有被无提示改变。
2. 停止创建新 Run，等待 active Run 完成或显式取消。
3. 停止 API/Worker；按平台流程创建并独立验证备份。仓库当前没有可引用的生产恢复演练。
4. 安装锁定依赖或构建镜像。
5. 只由 `make migrate` 或 Compose `migrate` 服务执行 preflight/upgrade/check。
6. 启动 API/Worker/frontend，检查 head、`live/health/ready`、Worker probe 和 task gauges。
7. 运行 `make smoke`；涉及可靠性/Compose 变更时运行 `make phase2-acceptance`。

### 9.2 API、Worker 与 Redis 重启

- API 重启不会拥有、取消或重新创建 Worker 租约；Run/Response 保持在数据库。
- Worker 优雅停止先使用 grace；异常退出则等待 lease 自然过期。新 Worker 以递增 token 恢复缺失 Response，旧 token 写入被拒绝。
- Redis 重启、清空或 ACK 丢失可能造成延迟/重复通知，但 DB reconciliation 和幂等唯一约束维持正确性。
- 最后一题已提交但 finalize 前崩溃时，reconciliation 从完整 Response 重新聚合，不再次调用 Provider。

这些语义已在隔离双 Worker Compose 验收中覆盖，但不构成多主机 HA、容量或恢复时间 SLA。

### 9.3 Schema/code 回滚

代码回滚必须与当前 schema、API 和 `protocol_version` 兼容。回退 `0002` 前必须停止 API/Worker 并确认没有 `pending/running`；downgrade 删除的租约/attempt 元数据不可逆，必须先接受其数据损失语义。

不同 protocol version、Benchmark version 或 dataset hash 不能因回滚无提示混排。优先使用经过平台验证的完整备份恢复，而不是盲目 downgrade；本仓库当前没有 PostgreSQL→SQLite 自动回退或生产恢复演练。

## 10. 当前限制与生产前工作

| 领域 | 当前可靠执行基础 | 生产前仍需 |
| --- | --- | --- |
| 身份与权限 | 无鉴权，所有端点可读写 | 登录/API Token、RBAC、对象授权、管理员导入/Model 权限、审计 |
| 网络 | API/frontend loopback；PG/Redis 内部 Compose 网络 | TLS 反向代理、可信 Host/代理、网络策略、认证与安全 headers |
| PostgreSQL | 单实例、named volume、迁移/故障测试 | 托管/HA、TLS、最小权限角色、加密、备份/PITR、RPO/RTO 与真实恢复演练 |
| Redis | 单实例 AOF、非权威通知层 | 认证/TLS、HA/容量/保留策略、监控；继续保持 DB 事实来源 |
| Worker | 租约/心跳/fencing/重试/取消，默认单 Worker | 全局/Provider 限流、预算、完整背压、公平调度、滚动排空与容量规划 |
| Secrets | 环境变量名入库，值只在 Worker 运行时读取 | Secret manager、短期凭据、轮换、每进程最小注入 |
| SSRF/数据外发 | `base_url` 基本校验 | allowlist、DNS/IP/redirect 验证、出站代理、元数据阻断、外发审批 |
| 可观测性 | 应用 JSON 日志、组件健康、DB gauges | 统一运行时日志、历史 metrics/traces、告警、完整审计、SLO |
| 数据保护 | 显式单向 importer 与 hash 对账 | 保留/删除策略、静态加密、备份/PITR、灾备演练、受控导出 |
| 供应链 | lockfile、基础 CI、版本标签镜像 | Action SHA/镜像 digest、漏洞门禁、SBOM、签名与 provenance |
| 性能/HA | 真实故障正确性验收 | 压测、容量/成本基线、多主机故障、滚动升级和恢复时间验证 |

Compose 可靠性验收只证明当前最小垂直切片在指定故障下保持数据库事实、逐题唯一性和协议 v1 评分；它不授权公网发布，也不把 Phase 2 标记为 completed。详细测试命令见 [TESTING.md](TESTING.md)，安全边界见 [SECURITY.md](SECURITY.md)，架构决定见 [ADR-0005](decisions/ADR-0005-durable-task-execution.md)。
