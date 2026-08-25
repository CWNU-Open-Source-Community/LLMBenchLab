# LLMBenchLab 部署与运行

## 1. 当前定位

LLMBenchLab MVP 支持两种运行方式：本机 Python/Node 开发模式，以及可选的 Docker Compose 单机模式。两者都使用 SQLite 和进程内后台任务，只适合个人、本地、低并发使用。

> 当前没有认证、授权、限流、生产 Worker 或 SSRF 完整防护。不要把 MVP 直接暴露到公网，也不要以多副本/多主机方式运行。

## 2. 端口与数据位置速查

| 模式 | 前端 | API | SQLite | 说明 |
| --- | --- | --- | --- | --- |
| 本地 Make | `http://127.0.0.1:5173` | `http://127.0.0.1:8000` | `backend/data/llmbenchlab.db` | 默认只监听 loopback |
| Docker Compose | `http://localhost:8080` | `http://localhost:8000` | volume 内 `/data/llmbenchlab.db` | 前端 Nginx 将 `/api/` 代理到 backend |

API 健康检查为 `/api/v1/health`；OpenAPI UI 为 API 地址下的 `/docs`。Compose 前端容器另有仅供容器健康检查使用的 `/healthz`。

## 3. 本地运行

### 3.1 前置要求

- Python 3.11 或更新版本。
- `uv`。
- Node.js 22 或兼容版本与 npm。
- Git；Docker 仅 Compose 模式需要。

确认版本：

```bash
python3 --version
uv --version
node --version
npm --version
```

### 3.2 一次性初始化

从仓库根目录运行：

```bash
make setup
```

脚本会：

1. 检查 Python、uv、Node 和 npm。
2. 仅在 `.env` 不存在时，从 `.env.example` 创建它；已有 `.env` 永不覆盖。
3. 用锁文件安装后端开发依赖与前端依赖。
4. 创建 `backend/data/`，执行安全迁移前置检查，再升级到 Alembic head。

`.env` 已被 Git 忽略。创建后应人工检查，不要把真实 Key 加入 `.env.example`。

### 3.3 启动

一个终端同时启动前后端：

```bash
make dev
```

脚本会监控两个子进程；任一服务退出时会停止另一服务，`Ctrl-C` 可整体关闭。需要分别查看日志或调试时，在两个终端运行：

```bash
make backend
```

```bash
make frontend
```

默认地址：

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:8000`
- OpenAPI：`http://127.0.0.1:8000/docs`

启动后检查：

```bash
curl -sS http://127.0.0.1:8000/api/v1/health
```

Mock Demo 不需要 API Key。载入 Demo 和创建离线 Run 的完整步骤见 [TESTING.md](TESTING.md)。

### 3.4 直接运行子项目

统一 Make 命令应是首选。排障时可直接运行：

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

另一个终端：

```bash
cd frontend
npm ci
VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1 npm run dev -- --host 127.0.0.1
```

相对 SQLite URL 根据当前工作目录解析。仓库脚本先进入 `backend/`，因此
`sqlite:///./data/llmbenchlab.db` 指向 `backend/data/llmbenchlab.db`；从其他目录直接启动时必须相应调整 URL。

## 4. 环境变量

配置优先从进程环境读取；根目录脚本会用 shell 载入根 `.env`。Pydantic 设置支持
`LLMBENCHLAB_` 前缀，并为常用变量提供短别名。

| 变量 | 示例/默认值 | 使用方 | 说明 |
| --- | --- | --- | --- |
| `DATABASE_URL` / `LLMBENCHLAB_DATABASE_URL` | `sqlite:///./data/llmbenchlab.db` | 后端/Alembic | SQLAlchemy URL；容器使用绝对 `/data` 路径 |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | 后端 | 单个允许的 CORS Origin；仅在未设置多 Origin 变量时生效；Compose 使用此项 |
| `CORS_ORIGINS` / `LLMBENCHLAB_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | 后端 | 优先于 `FRONTEND_ORIGIN`；本地示例同时允许两种 loopback 拼写，改端口时须同步，`*` 会被拒绝 |
| `LOG_LEVEL` / `LLMBENCHLAB_LOG_LEVEL` | `INFO` | 后端 | `CRITICAL/ERROR/WARNING/INFO/DEBUG` |
| `LLMBENCHLAB_ENVIRONMENT` | `development` | 后端 | `/info` 中的环境标签 |
| `LLMBENCHLAB_DEBUG` | `false` | 后端 | 本地调试；有秘密时不要开启 |
| `API_HOST` | `127.0.0.1` | Make/脚本 | Uvicorn 监听地址，不是 Pydantic 应用设置 |
| `API_PORT` | `8000` | Make/Compose | 本地监听或 Compose host port |
| `FRONTEND_HOST` | `127.0.0.1` | Make/脚本 | Vite 监听地址 |
| `FRONTEND_PORT` | `8080` | Compose | Nginx 的 host port |
| `VITE_API_BASE_URL` | `http://localhost:8000/api/v1` | Vite build/dev | 编译期公开配置，不得放秘密 |
| `DEMO_API_KEY_ENV` | `LLMBENCHLAB_DEMO_API_KEY` | 示例/CI 元数据 | 只是“环境变量名称”的示例，Mock 不读取它 |

`VITE_*` 会进入浏览器产物，永远不能存放 Key。`DEMO_API_KEY_ENV` 不是 Provider Key，也不会自动注册模型。

### 4.1 OpenAI-compatible Key

为某个模型选择一个环境变量名，例如 `LOCAL_COMPAT_API_KEY`：

1. 在启动 backend 的同一进程环境或安全的秘密注入系统中设置它。
2. Model API 的 `api_key_env` 只填写字符串 `LOCAL_COMPAT_API_KEY`。
3. 不把真实值放进 API JSON、数据库、截图或 Git。

Adapter 在每次请求时读取该变量；缺失会将单题记为 `missing_api_key`。配置真实 Provider 前必须审查目标 `base_url` 和 Benchmark 外发许可，详见 [SECURITY.md](SECURITY.md)。

## 5. 数据库与迁移

### 5.1 本地路径

默认数据库：

```text
backend/data/llmbenchlab.db
```

文件、`*.db-wal`、`*.db-shm` 及迁移收养生成的 `*.bak` 均被 Git 忽略。不要提交数据库或备份；它们可能包含题目、参考答案、原始回答和错误内容。

### 5.2 迁移

每次拉取包含 migration 的更新后，在启动前运行：

```bash
make migrate
```

`20260824_0000` 是可执行的 ORM-era legacy-schema revision，会为全新空库创建五张旧结构表；`20260824_0001` 将其保留数据地升级为当前 schema/head。受支持的未版本化 legacy 库由 preflight 验证后 stamp 到 `0000`，未版本化 current-schema 库则 stamp 到 head；当前应用只能在 head 启动。检查状态：

```bash
cd backend
uv run alembic current
uv run alembic heads
```

Alembic 是普通运行环境唯一的 schema owner；应用启动不执行 `create_all`，并会在 revision 不是 head 时快速失败，提示先执行 setup/migrate。`make setup`、`make migrate` 与 backend 容器入口均先运行迁移前置检查，再执行 `alembic upgrade head`：

- 空库直接进入正常 upgrade；已有 `0000` 或 head revision 的库先验证其对应结构，head 漂移也会拒绝。
- 五张业务表已经存在但没有有效 revision：只接受与当前 schema 完全一致，或与已知 legacy schema 的六项差异完全一致的 SQLite。
- 受支持的未版本化库：先执行 SQLite backup API，生成 `llmbenchlab.db.pre-alembic-<UTC>.bak`，再用 Alembic stamp 到经验证的 revision 并继续 upgrade。
- 部分表、未知列/约束漂移、完整性错误、外键错误或不满足新约束的数据：在 stamp 前退出并给出错误，不猜测、不删库。

直接手工操作时也必须先运行前置检查：

```bash
set -a
source ./.env
set +a
cd backend
uv run python -m app.db.prepare_migrations
uv run alembic upgrade head
```

迁移失败时不要删除数据库、不要盲目 `stamp head`，也不要直接修改 `alembic_version`。保留日志和 `.bak`，先评估失败位置再恢复或重试。任何 downgrade 都要先确认数据丢失语义；`downgrade base` 会删除全部业务表，只应在临时验证库或已确认可丢弃且有备份的库执行。

## 6. Docker Compose

### 6.1 启动和停止

```bash
make docker-up
```

该命令执行前台 `docker compose up --build`。Compose 包含：

- `backend`：Python 3.12、非 root 用户，启动时先做迁移前置检查再升级，SQLite volume 挂载到 `/data`。
- `frontend`：Node 22 build 后由 Nginx 提供静态文件；`/api/` 同源代理到 `backend:8000`。
- named volume `sqlite-data`：持久保存 `/data/llmbenchlab.db`。

默认访问：

- Web（推荐）：`http://localhost:8080`
- 直接 API：`http://localhost:8000/api/v1/health`

停止并保留数据：

```bash
make docker-down
```

该命令等价于 `docker compose down`，不会删除 named volume。`docker compose down -v` 会删除数据库 volume，是破坏性操作；只有明确要丢弃数据且已有所需备份时才能运行。

### 6.2 Compose 配置校验与状态

```bash
docker compose config
docker compose ps
docker compose logs backend
docker compose logs frontend
```

`docker compose config` 仅证明 YAML 和插值有效。完整验收还需构建成功、两个容器健康，以及 Mock Smoke/API 手工链路通过。

### 6.3 Compose 安全注意

当前 `ports` 短语法通常绑定宿主机所有接口，而不只是 loopback。它适合隔离的开发机，但在公共 Wi-Fi、云主机或共享服务器上可能被同网段访问。公开部署前必须改成明确的 loopback binding 或受保护的反向代理，并加入鉴权；CORS 不能阻止非浏览器访问。

Compose 默认只适合 Mock，不向 backend 传递任意 Provider Key。若用户主动做真实 Provider 手工验证，应使用未提交的 Compose override 或部署 secret 注入，把**选定变量**传给 backend；不要把值写进 `compose.yaml`。示意 override：

```yaml
services:
  backend:
    environment:
      LOCAL_COMPAT_API_KEY: ${LOCAL_COMPAT_API_KEY:?set_in_shell_before_start}
```

先在受控 shell/secret store 中提供值，再启动 Compose。不要提交包含值的 override，也不要把它打印到 `docker compose config` 的共享日志。

### 6.4 容器数据位置

应用内数据库 URL 是：

```text
sqlite:////data/llmbenchlab.db
```

Compose 项目名固定为 `llmbenchlab`，Docker 通常将 volume 显示为
`llmbenchlab_sqlite-data`；应以 `docker volume ls` 与 `docker compose config` 的实际输出为准，不要在脚本中猜测名称。

## 7. SQLite 备份与恢复

备份是用户责任。变更 Schema、升级应用、移动 volume 或清理环境前必须备份，并定期做恢复演练。

### 7.1 本地一致性备份

若系统安装了 `sqlite3`，可在应用运行时使用 SQLite backup API：

```bash
mkdir -p backups
sqlite3 backend/data/llmbenchlab.db ".backup 'backups/llmbenchlab-20260824.db'"
sqlite3 backups/llmbenchlab-20260824.db 'PRAGMA integrity_check;'
```

预期完整性检查输出 `ok`。备份文件名应包含 UTC 时间和可选 commit SHA；把它放到受控、加密且不被 Git 跟踪的位置。不要只复制一个正在写入的 `.db` 而忽略可能存在的 WAL/SHM。

没有 `sqlite3` CLI 时，先停止 backend，再复制主数据库文件：

```bash
cp backend/data/llmbenchlab.db backups/llmbenchlab-20260824.db
```

### 7.2 本地恢复

1. 停止 backend，确认没有进程持有数据库。
2. 先保留当前文件，再复制经过验证的备份：

   ```bash
   cp backend/data/llmbenchlab.db backend/data/llmbenchlab.pre-restore.db
   cp backups/llmbenchlab-20260824.db backend/data/llmbenchlab.db
   sqlite3 backend/data/llmbenchlab.db 'PRAGMA integrity_check;'
   make migrate
   ```

3. 启动服务，检查 `/api/v1/health`、模型/Benchmark/Run 数量和一条逐题记录。

不要在数据库打开时覆盖文件。恢复不会恢复 `.env` 或 Provider Key；秘密应独立管理。

### 7.3 Compose 备份

最简单可靠的 MVP 做法是短暂停止 backend，前端可保持运行但 API 暂不可用：

```bash
mkdir -p backups
docker compose stop backend
docker compose cp backend:/data/llmbenchlab.db ./backups/llmbenchlab-compose-20260824.db
docker compose start backend
```

验证复制出的文件：

```bash
sqlite3 backups/llmbenchlab-compose-20260824.db 'PRAGMA integrity_check;'
```

恢复时停止 backend，先从容器复制一份现状备份，再把目标备份复制到一个临时文件名；由一次性 backend 容器完成替换和迁移，避免主服务同时写入：

```bash
docker compose stop backend
docker compose cp backend:/data/llmbenchlab.db ./backups/llmbenchlab-before-restore.db
docker compose cp ./backups/llmbenchlab-compose-20260824.db backend:/data/llmbenchlab.restore.db
docker compose run --rm --no-deps backend sh -c 'cp /data/llmbenchlab.restore.db /data/llmbenchlab.db && python -m app.db.prepare_migrations && alembic upgrade head'
docker compose start backend
```

随后检查健康和关键记录。恢复命令会覆盖 volume 中的主数据库，执行前必须再次核对备份路径与目标环境。

### 7.4 备份范围与验证

一个可恢复快照至少记录：

- SQLite 文件及 `PRAGMA integrity_check` 结果。
- 应用 commit/tag、Alembic revision、创建时间（UTC）。
- 与结果解释有关的 Benchmark 源文件或 dataset hash。
- 非秘密运行配置。秘密和 Key 只在独立秘密系统备份。

定期抽样恢复到隔离目录/临时 volume，运行迁移、健康检查和只读数据核对。只有“文件存在”不等于备份可恢复。

## 8. 升级、回滚和运行行为

### 8.1 升级顺序

1. 阅读 Changelog、migration 和协议变化。
2. 停止创建新 Run，等待现有 Run 终态。
3. 创建并验证 SQLite 备份。
4. 安装锁定依赖/构建新镜像。
5. 执行 `make migrate` 或让 Compose backend 启动命令迁移。
6. 启动 API，检查 health/info 和 Alembic revision。
7. 运行完全离线 Mock Smoke，再恢复正常使用。

### 8.2 回滚

代码回滚必须与数据库、API、前端和 `protocol_version` 兼容。优先从升级前备份恢复整个 SQLite，而不是盲目执行 Alembic downgrade。不同协议或 dataset hash 的结果不能因回滚被无提示混排。

### 8.3 进程重启

Runner 是 API 进程内 asyncio task。正常关闭会取消活动任务并标记失败；异常终止后，下次启动会把遗留 `running` Run 标为 `failed`，错误为
`interrupted_by_process_restart...`。MVP 不会自动继续，也没有独立 Worker/队列。重启前应等待 Run 完成；中断后由用户显式创建新 Run。

## 9. 生产部署前必须完成的改造

| 领域 | 当前 MVP | 生产前要求 |
| --- | --- | --- |
| 身份与权限 | 无鉴权，所有端点可读写 | 登录/API Token、RBAC、对象级授权、管理员 Model/导入权限、审计 |
| 网络 | 开发端口直出 | TLS 反向代理、可信 Host/代理、仅所需端口、网络分区和安全 headers |
| SSRF | 只做 URL 语法校验 | Provider allowlist、IP/DNS/redirect 校验、出站代理/网络策略、元数据阻断 |
| 数据库 | 单文件 SQLite | PostgreSQL、连接池、迁移演练、加密、备份/PITR 与恢复目标 |
| 任务执行 | 单进程 task，不恢复 | Redis/可靠队列、独立 Worker、租约、幂等、心跳、重试/死信、优雅排空 |
| 扩展 | 单实例低并发 | 全局并发/Provider rate limit、背压；完成协调前不得简单增加 API replicas |
| 费用 | 人工价格估算，无预算 | 每用户/Provider 预算、最大题量/Token、成本预检、熔断和告警 |
| 上传 | 应用层 ZIP 限制 | 代理请求体/超时限制、每用户配额、存储隔离、内容/许可证审批 |
| 私有数据 | SQLite 明文、API 返回答案/raw | 静态加密、访问控制、DLP、保留/删除、导出审计、Provider 外发确认 |
| Secrets | 本地环境变量 | Secret manager、短期凭据、轮换、最小权限、无日志注入 |
| 可观测性 | 基础日志/health | 结构化关联日志、metrics、traces、ready/live、告警和审计事件 |
| 供应链 | lockfile 与基础 CI | Action SHA/镜像 digest、漏洞门禁、SBOM、签名和 provenance |
| Web 安全 | 显式 CORS、部分 Nginx headers | CSP、认证后 CSRF/session 设计、速率限制、渗透测试 |
| 代码评测 | 不支持 | 专用无网络沙箱与资源限制；不得在 API 主机直接执行不可信代码 |

具体安全要求见 [SECURITY.md](SECURITY.md)，可靠任务架构属于 Phase 2，代码沙箱属于 Phase 3。

## 10. 当前部署限制

- 仅单机 SQLite；并发写入和容量有限，未做性能/容量 SLA。
- 进程内任务无法跨重启恢复，不能安全地用常规滚动发布承载活动 Run。
- 没有多用户、认证、授权、速率限制或费用硬上限。
- 任意受信任用户可配置 `base_url`，SSRF 未在应用层解决。
- Benchmark 与 Responses API 暴露题目、参考答案、metadata 和原始回答。
- SQLite/volume/备份默认未加密，无自动备份、保留或灾难恢复服务。
- Compose 是开发便利配置：端口可能对局域网开放，未配置 TLS、生产代理或集中日志。
- OpenAI-compatible 兼容性取决于目标服务对 Chat Completions 的实现；真实 Provider 不属于自动验收。
- 上游不返回 usage 时 Token 和费用为未知；估算价格由用户配置，不是账单真值。
- Demo 数据只验证链路，不代表正式模型能力。
