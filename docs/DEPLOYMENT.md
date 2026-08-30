# LLMBenchLab 部署与运行

## 1. 当前定位

LLMBenchLab 当前有三条本地运行路径：

- 本地开发兼容路径：SQLite、可选 Redis、FastAPI API、独立 Worker 和 Vite。它便于开发与离线 Mock 验收，但 SQLite 只支持单 Worker 低并发，不能替代 PostgreSQL 并发证据。
- Phase 2 Compose 可靠执行路径：PostgreSQL 是唯一任务事实来源，Redis Streams 是 at-least-once 通知层，API 与多个可复制 Worker 是独立进程，migrate 是唯一 Alembic upgrade owner，Nginx 提供前端。标准 Make 入口默认两个 Worker。
- 可信本地正式评测 CLI：直接连接已迁移数据库，固定下载/转换 MMLU-Pro 或 GPQA-Diamond，在同一进程做 Provider preflight、运行有界 Runner、恢复缺失题并导出全量报告；不要求浏览器、API、Redis 或常驻 Worker。

可靠执行基础已经覆盖租约、心跳、fencing、幂等 Response、取消、有限重试、数据库 reconciliation 和故障恢复；数据库权威的 global/provider/model/run 治理、逐 HTTP attempt ledger、背压、公平 slice、typed audit/archive、Worker progress、固定 exporter/规则、历史延迟及 Mock 容量基线也已进入 Phase 2 工作树，`llmbenchlab-protocol-v1` 评分含义没有改变。部署仍不等于生产高可用；容量边界见 [`PERFORMANCE.md`](PERFORMANCE.md)，告警、故障、retention 与 0005/0004 安全回滚见 [`OPERATIONS.md`](OPERATIONS.md)。

> Compose 是本地故障验证配置，不是生产方案。它没有认证、授权、TLS、正式 secret manager、自动备份/PITR、告警或 HA；不要直接暴露公网，也不要把示例密码当作生产秘密。

## 2. 地址、进程与数据速查

| 模式 | Web | API | 数据/队列 | 说明 |
| --- | --- | --- | --- | --- |
| 本地 Make | `http://127.0.0.1:5173` | `http://127.0.0.1:8000` | `backend/data/llmbenchlab.db`；Redis 可选 | `make dev` 启动 API、独立 Worker、frontend；默认 loopback |
| 可信本地 CLI | 不需要 | 不需要 | 当前 `DATABASE_URL`；`artifacts/` 中缓存、Benchmark ZIP 与报告 | `llmbenchlab-evaluate` 自己运行 Runner；同一数据库不得有竞争 Worker |
| Docker Compose | `http://127.0.0.1:8080` | `http://127.0.0.1:8000` | `postgres-data`、`redis-data` named volumes | `make dev-multi` 默认两个 Worker；API/frontend 仅 loopback；PostgreSQL/Redis 无 host port |

API 系统端点：

- `/api/v1/live`：仅 API 进程 liveness，不访问数据库、Redis 或 Provider。
- `/api/v1/health`：兼容端点，只检查数据库连接。
- `/api/v1/ready`：检查数据库、Alembic head 与 Redis，返回组件化、脱敏状态。
- `/api/v1/tasks/metrics`：数据库派生的当前任务 gauges。
- `/api/v1/tasks/history`：有界 UTC 窗口的 typed event counters 和 Run 延迟分位数。
- `/api/v1/governance/policy`：可信 loopback 上读取/原子应用完整版本化治理 policy。
- `/api/v1/runs/{run_id}/audit`：按稳定顺序分页读取保留期内的 typed Run audit。
- `/docs`：OpenAPI UI。

前端容器的 `/healthz` 只表示 Nginx/静态站点可响应。

## 3. 本地开发运行

### 3.1 前置要求与初始化

- `uv`；setup/dev 及 API/Worker 的 keyring bootstrap 会由它选择满足后端约束的 CPython 3.11+，不依赖 `PATH` 中的裸 `python3`。
- Node.js 22 或兼容版本与 npm。
- Git；Docker 只在 Compose 和 Phase 2 真实故障验收时需要。

从仓库根目录运行：

```bash
make setup
```

脚本只在 `.env` 不存在时复制 `.env.example`，让 `uv` 显式选择 CPython 并按 lockfile 安装依赖，创建或严格校验 `.secrets/credential-keys.json`，执行安全迁移 preflight，并将本地 SQLite 升级到 Alembic head。已有 `.env` 不会覆盖；即使旧 `.env` 没有新变量，API/Worker 也使用仓库根目录的绝对默认 keyring 路径。`.env`、keyring、数据库、WAL/SHM 与自动收养备份都被 Git 忽略。

若旧版本在首次运行时报 `Credential keyring could not be initialized safely`，常见原因是 macOS 上裸 `python3` 命中了 PyPy，而该实现对安全原子链接参数返回 `EINVAL`。更新代码后直接重跑 `make setup && make dev`；原始 `EINVAL` 路径会正常清理且不生成目标 keyring。新版只有在确认临时文件已清理后才会重试；若操作系统拒绝清理，则立即停止并给出符号 errno。排障时请保留该错误码，但不要发送 `.env`、keyring 或残留临时文件的内容。

### 3.2 启动 API、Worker 与前端

```bash
make dev
```

`scripts/dev.sh` 同时管理三个进程；任一进程退出会停止另外两个并传播其退出状态。组合启动的控制台只显示 Web/API 地址和日志位置，API、Worker、Vite 的 stdout/stderr 分别追加到 `artifacts/dev-logs/api.log`、`worker.log`、`frontend.log`，每次启动都有 UTC session marker。目录和日志在创建/复用时分别收紧为 `0700`/`0600`，且都被 Git 忽略；它们仍是敏感本地运维证据，不应上传。

跟踪详细输出可运行：

```bash
tail -f artifacts/dev-logs/api.log \
  artifacts/dev-logs/worker.log \
  artifacts/dev-logs/frontend.log
```

需要把日志放到其他受保护目录时，仅为本地启动器设置 `LLMBENCHLAB_DEV_LOG_DIR`。需要让某个服务直接在前台输出时使用三个独立入口：

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

当 `.env` 的有效 `DATABASE_URL` 已指向迁移到 head 的 PostgreSQL 时，同一 Vite 开发入口可以管理多个 Worker 进程：

```bash
make dev DEV_WORKERS=2
```

`DEV_WORKERS` 映射到 launcher-only 的 `LLMBENCHLAB_DEV_WORKER_PROCESSES`，范围为 1–32。单 Worker继续写 `worker.log`；多 Worker分别写 `worker-1.log`、`worker-2.log` 等，并向 API 注入同值的 `LLMBENCHLAB_WORKER_EXPECTED_PROCESSES`。任一 Worker、API 或 frontend 退出时，启动器向同一会话所有剩余进程发送 TERM、逐一等待并传播原退出码。请求 `N>1` 而有效 DSN 不是 PostgreSQL 时，会在创建日志或启动子进程前失败，错误不会回显 DSN。

已有 SQLite 数据不能仅靠改 DSN 出现在 PostgreSQL。需要保留 Model、Benchmark 和 Run 时，必须先让 `pending/running`、active reservation 与 live Worker generation 通过受支持的取消/租约/对账流程收敛，随后停写并使用第 7 节的 SQLite→空 PostgreSQL importer；启动器不会自动迁移或双写。

### 3.3 直接运行子项目

统一 Make 命令应是首选。排障时可显式运行：

```bash
set -a
source ./.env
set +a
cd backend
uv sync --python cpython --frozen --extra dev
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

### 3.4 可信本地正式评测 runbook

这条流程会向第三方发送题目并可能产生费用，只能由受信任操作者主动执行。先从仓库根目录完成依赖与 migration：

```bash
make setup
```

停止连接同一数据库的常规 API 和常驻 Worker（使用 `make dev` 时结束整个开发进程组），让 CLI 独占数据库。CLI 会拒绝在已有 `running` Run 时创建新 Run，但不能把这个检查当作多执行者锁；它无法发现仍空闲的 Worker，后者可能抢走刚创建的 `pending` Run，使 Key 继承和费用边界变得不可预测。SQLite 始终只允许一个执行者。

先准备少量正式题。该命令会联网下载固定数据源、校验预期大小/SHA、写入缓存并生成 dataset-v1 ZIP，但不会初始化 Provider 或读取 Key：

```bash
cd backend
uv run llmbenchlab-evaluate prepare \
  --dataset mmlu-pro \
  --profile official_cot \
  --limit 20
```

`--dataset` 可选 `mmlu-pro` 或 `gpqa-diamond`。`--groups` 是逗号分隔的 MMLU category 或 GPQA high-level domain；MMLU `--profile` 可选 `official_cot`/`direct`；GPQA 可用 `--shuffle-seed`，默认 `42`。缓存和 ZIP 默认写到仓库根目录的 `artifacts/dataset-cache/`、`artifacts/benchmarks/`，两者均被 Git 忽略。

真实运行的 Key 有两种安全输入方式：

1. 由操作系统 Keychain、secret manager 或受控父进程设置 `LLMBENCHLAB_REAL_API_KEY`；或用 `--api-key-env NAME` 选择另一个已设置变量。
2. 不设置该变量，在交互终端等待隐藏输入。

不要在 shell 命令中拼接真实值。CLI 没有 `--api-key` 参数，非交互且变量为空时会停止。

使用兼容根地址的限题评测；`--provider-type` 必须与目标模型实际使用的协议一致：

```bash
cd backend
uv run llmbenchlab-evaluate run \
  --dataset mmlu-pro \
  --profile official_cot \
  --limit 20 \
  --provider-type openai_responses \
  --base-url https://provider.example.invalid/v1 \
  --model replace-with-provider-model-id \
  --concurrency 1
```

Base URL 支持协议根地址或与显式协议匹配的完整 endpoint：

- `--provider-type openai_compatible`：生成 endpoint 为 `/chat/completions`；
- `--provider-type openai_responses`：生成 endpoint 为 `/responses`；
- `--provider-type anthropic_messages`：生成 endpoint 为 `/messages`。

例如根地址 `https://host/v1` 会把发现请求发到 `GET https://host/v1/models`，再按所选协议生成对应 endpoint；完整地址 `https://host/v1/chat/completions`、`https://host/v1/responses` 或 `https://host/v1/messages` 则原样用于生成，发现请求仍推导为同级 `/models`。完整 endpoint 与 `--provider-type` 不匹配时会在发送 Key 前拒绝；不会根据模型名/URL 猜测协议，也不会在失败后跨协议 fallback。

远端 Provider 必须使用 HTTPS；明文 HTTP 仅允许 `localhost` 或字面量 loopback IP，用于操作者控制的本地推理服务。模型发现按 `--provider-type` 鉴权：Chat/Responses 使用 `Authorization: Bearer`，Messages 使用 `x-api-key` 与 `anthropic-version`；Messages 的 `has_more/last_id` 通过 `after_id` 分页，并受累计 100 页、60 秒 wall-clock、2 MiB、10,000 项与缺失/重复 cursor 门禁保护。默认模型发现只在聚合后返回唯一模型时自动选择；返回多个模型必须提供 `--model`。任何模型 ID 若包含当前 Key，预检立即失败且错误不会回显该值。如果 `/models` 返回 404/405，只有显式提供模型名才继续。已知 Provider 不实现发现时可用 `--no-model-discovery --model ...`，但付费 canary 仍会执行。

CLI 在发出 canary 前显示 Provider host、协议、模型、计分题数、剩余 failed-attempt 预算和最多 Provider HTTP 尝试数，交互要求精确输入 `RUN`。剩余预算严格为 `max_attempts - failed_attempt_count`，不把 cooperative yield 算作失败。canary 必须可解析为预期答案；若成功体明确返回不同于目标的模型名也会失败。上界包含每个逻辑调用最多 3 次 HTTP attempts：`(缺失计分题数 × 剩余 failed-attempt 预算 + 1 个 canary) × 3`。`--yes` 仅用于操作者明确批准的非交互环境。

MMLU `official_cot` 的输出上限默认 `max_tokens=4000`，其他配置默认 `max_tokens=1024`，共同默认 `concurrency=1`。Chat Completions 另默认 `temperature=0/top_p=1/seed=42`；Responses 与 Messages 在未显式传入或由 Model 默认提供时省略 `temperature`、`top_p` 和 `seed`，避免向不支持采样字段的模型发送它们。Responses/Messages 不接受非空 seed；Messages 的 `temperature` 上限为 `1`，且 `max_tokens` 必须始终为有限正整数。允许的命令参数会冻结到 Run 快照。

模型发现与三类远程请求固定声明 `Accept-Encoding: identity` 并拒绝其他响应编码；discovery 聚合最多 2 MiB/10,000 个模型 ID，Messages 分页另限制累计 100 页/60 秒 wall-clock，并拒绝 cursor 循环或 `has_more=true` 时缺失 `last_id`。Chat、Responses、Messages 的 SSE 必须分别消费到 `[DONE]`、`response.completed`、`message_stop`；普通 JSON 成功体上限 4 MiB，SSE wire/单事件/聚合 content 上限分别为 64 MiB/1 MiB/4 MiB，非 2xx 错误体上限 64 KiB。Provider 返回证据会递归检查成功内容、raw usage 的对象键/全部 JSON 标量、request ID、返回模型名、system fingerprint 和 finish reason；SSE content 先完整聚合，当前 Key 的精确回显再于进入 Runner/快照/Response 边界前替换为 `[REDACTED]`。这些控制不扫描无关 Benchmark/Question 内容，也不替代 Provider 账单检查、内容访问控制或通用 DLP。

API URL 与 Key 已足以在 `/models` 只返回一个可用 ID 时选择模型；若返回多个模型，仍需提供 `--model`，避免猜测付费目标。`--input-price-per-million`/`--output-price-per-million` 是可选成本估算输入；不提供价格或 Provider usage 时报告成本为 unknown，而不是虚假 `0`。

GPQA-Diamond 固定 Prompt profile `zero-shot-cot-answer-line-v1`，要求逐步思考并在末行写 `Answer: X`。MMLU/GPQA 标准 manifest 的 system 都为空，Runner 会省略空 system message，而不是向 Provider 发送空内容 system role。

限题成功并核对 Provider 账单后，才考虑全量。以下示例会评测 GPQA-Diamond 全部 198 题：

```bash
cd backend
uv run llmbenchlab-evaluate run \
  --dataset gpqa-diamond \
  --full \
  --provider-type openai_compatible \
  --base-url https://provider.example.invalid/v1/chat/completions \
  --model replace-with-provider-model-id \
  --api-key-env MY_PROVIDER_API_KEY \
  --concurrency 1
```

`run` 强制在 `--limit` 和 `--full` 中二选一，防止无意启动全量；如果同时给 `--groups`，`--full` 仅表示选定 groups 内的全量。MMLU-Pro 全量为 12,032 题，尤其是 `official_cot` 会产生显著 Token、时间和费用。该可信本地直连 CLI 仍创建 `legacy_unmanaged` Run，不经过 Web/API admission policy，因此没有全局 RPM/TPM 或金额预算硬上限；它和受治理 Web Run 都不保证 Provider exactly-once。

`run`/`resume` 到达任何终态后，默认把报告写入 `artifacts/evaluations/<RUN_ID>/`；`completed` 可用于正式比较，`failed/cancelled` 报告只用于诊断部分证据。也可用 `--report-dir` 指定一个尚不存在的目录。收到信号、进程崩溃或 Run 留在非终态后，使用同一数据库恢复缺失 Response：

```bash
cd backend
uv run llmbenchlab-evaluate resume <RUN_ID>
```

恢复会读取 Run 中冻结的 Base URL、模型和生成配置，再次做模型发现/确认/canary。若原 Run 记录的变量名与当前 secret manager 不同，可用 `--api-key-env SOURCE_ENV` 临时把来源值映射到冻结变量名。未过期的旧租约必须先自然到期；CLI 会等待数据库裁决，不会覆盖有效 owner。已经过期且证据不全的旧租约会由本地 Runner fenced reclaim，不再等待已停止的外部 Worker。新 Run 的初次 canary 证据会进入快照，但 resume canary 当前不会追加为独立事件；逐题 Response 已保存经过安全字符/长度校验的 request ID、返回 model、system fingerprint、finish reason 和 HTTP attempt count。

任何终态 Run 都可离线导出，不读取 Key 或调用 Provider：

```bash
cd backend
uv run llmbenchlab-evaluate report <RUN_ID> \
  --output-dir ../artifacts/evaluations/<NEW_REPORT_DIRECTORY> \
  --group-by domain
```

报告目录不得已存在，防止静默覆盖证据。`summary.json` 保存协议/数据/模型/生成/执行/preflight 快照，并从计划题与实际 Responses 派生唯一主指标；`metrics_provenance` 标出持久化 Run 字段是否一致及漂移字段。`groups.csv` 使用同一证据口径按一个白名单 metadata 字段形成完整分区，`responses.jsonl` 分页导出所有已持久化逐题证据。文件不含 Key，但含题目、参考答案、raw response 和错误，必须按数据库敏感级别保护。

### 3.5 上游 SSE 与反向代理

`read_timeout_seconds` 是 HTTPX 等待下一批响应字节的空闲读取上限，不是整个生成的总墙钟时限。正常 SSE token 或 comment ping 可以持续刷新这个窗口；但 Worker 到 Provider 之间每一层 CDN/Gateway 的响应头超时、读取空闲、绝对总时长和 buffering 都需要独立核对。

- Provider 必须尽早返回 `Content-Type: text/event-stream`，并在 token/心跳后实际 flush。[Caddy 官方文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming) 说明识别到该 Content-Type 时会立即 flush；应避免覆盖 Content-Type 或引入 `response_buffers`。不必为此无条件设置 `flush_interval -1`，因为它还会改变客户端断开后的上游取消行为。
- [Cloudflare 524 官方说明](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/) 当前把 proxied 请求的默认 Proxy Read Timeout 记为 125 秒，并说明实际 524 可出现约 1 秒偏差。近 126 秒的 524 与这一已知边界吻合，但不能只凭状态码断定所有 524 都是同一原因。
- 无法在当前 Cloudflare 套餐上放宽必要限制时，可考虑为模型 API 使用 DNS-only/直连子域。这会暴露 origin，必须同时补齐有效 TLS、访问控制、防火墙/源站限制和日志审计，不得当作无风险开关。

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
| `LLMBENCHLAB_WORKER_PROGRESS_FLUSH_SECONDS` | `5` | 真实 scan/claim/progress/heartbeat bit 合并写入 DB 的最大间隔；timer 不生成 keepalive |
| `LLMBENCHLAB_WORKER_PROGRESS_STALE_SECONDS` | `60` | DB UTC 下 active generation 的 stale cutoff |
| `LLMBENCHLAB_DEV_WORKER_PROCESSES` | `1` | 仅 `scripts/dev.sh` 使用的本地 Worker 进程数，范围 1–32；大于 1 要求 PostgreSQL |
| `LLMBENCHLAB_WORKER_EXPECTED_PROCESSES` | `1` | 部署明确声明的最小 live Worker 数；扩缩容必须同步修改 |
| `LLMBENCHLAB_WORKER_RECOVERY_ALERT_SECONDS` | `60` | expired lease age 告警比较阈值 |
| `LLMBENCHLAB_MOCK_GENERATION_DELAY_SECONDS` | `0` | 只用于确定性 Mock 故障测试；不改变报告 latency 或协议评分 |
| `LLMBENCHLAB_CREDENTIAL_KEYS_FILE` | 仓库根 `.secrets/credential-keys.json` | API 加密、Worker 解密 Web Key 的共享 keyring；空值会关闭 stored 模式并 fail closed |

配置校验要求 `heartbeat * 2 <= lease`，退避 base 不得大于 cap。不要为了让超时测试通过而把生产时间参数直接套到验收脚本；`phase2_acceptance.py` 使用隔离的短租约配置。

### 4.2 API、前端与日志

| 变量 | 默认/示例 | 说明 |
| --- | --- | --- |
| `CORS_ORIGINS` / `LLMBENCHLAB_CORS_ORIGINS` | 两种 localhost `:5173` | 显式 allowlist；拒绝 `*` |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | 单 Origin 兼容别名；`CORS_ORIGINS` 优先 |
| `LOG_LEVEL` / `LLMBENCHLAB_LOG_LEVEL` | `INFO` | `CRITICAL/ERROR/WARNING/INFO/DEBUG` |
| `LLMBENCHLAB_DEV_LOG_DIR` | `artifacts/dev-logs` | 仅供 `make dev` 使用的本地详细日志目录；不是应用 Settings 或生产日志后端 |
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
| `LLMBENCHLAB_COMPOSE_WORKER_MAX_ATTEMPTS` | `3` | 映射到新 Run 的最大 Worker attempt |
| `LLMBENCHLAB_COMPOSE_WORKER_RETRY_BACKOFF_BASE_SECONDS` | `1` | 映射到 Worker retry backoff base |
| `LLMBENCHLAB_COMPOSE_WORKER_RETRY_BACKOFF_CAP_SECONDS` | `30` | 映射到 Worker retry backoff cap |
| `LLMBENCHLAB_COMPOSE_WORKER_SHUTDOWN_GRACE_SECONDS` | `30` | 应用 grace；容器另有 45 秒 stop grace |
| `LLMBENCHLAB_COMPOSE_WORKER_PROGRESS_FLUSH_SECONDS` | `5` | 映射到 Worker progress flush |
| `LLMBENCHLAB_COMPOSE_WORKER_PROGRESS_STALE_SECONDS` | `60` | 映射到 Worker stale cutoff |
| `LLMBENCHLAB_COMPOSE_WORKER_PROCESSES` | `2` | 标准 Compose 包装器的 Worker replica 数，范围 1–32；当前只有 1–2 经过容量资格 |
| `LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES` | `1`（直接 Compose） | API 的低层 expected 声明；标准包装器从 Worker 数自动设置，直接 `docker compose` 时仍须自行同步 |
| `LLMBENCHLAB_COMPOSE_WORKER_RECOVERY_ALERT_SECONDS` | `60` | exporter lease recovery 告警阈值 |
| `LLMBENCHLAB_COMPOSE_REDIS_BLOCK_MILLISECONDS` | `1000` | 映射到 Worker Redis blocking read 上限 |
| `LLMBENCHLAB_COMPOSE_REDIS_OPERATION_TIMEOUT_SECONDS` | `1` | 容器 Redis 操作 timeout |
| `LLMBENCHLAB_COMPOSE_DATABASE_POOL_SIZE` | `5` | 映射到 API/Worker 数据库 pool size |
| `LLMBENCHLAB_COMPOSE_DATABASE_MAX_OVERFLOW` | `5` | 映射到 API/Worker 数据库 pool overflow |
| `LLMBENCHLAB_COMPOSE_DATABASE_POOL_TIMEOUT_SECONDS` | `2` | 容器连接池 timeout |
| `LLMBENCHLAB_COMPOSE_READINESS_DATABASE_TIMEOUT_SECONDS` | `2` | 容器 readiness 等待上限 |
| `LLMBENCHLAB_COMPOSE_MOCK_GENERATION_DELAY_SECONDS` | `0` | 可靠性测试专用 Mock delay |
| `LLMBENCHLAB_COMPOSE_CREDENTIAL_KEYS_FILE` | `.secrets/credential-keys.json` | Compose 只读挂载给 API/Worker 的宿主 keyring |

### 4.4 远程 Provider Key

Web 服务路径让用户在 Models 表单直接输入真实 Provider Key。API 将它作为 write-only 字段接收，用共享 keyring 做 AES-256-GCM 加密，并只把认证密文写入 `model_credentials`；Worker 读取同一 keyring 解密。API 不把凭据流中的 Key 或 Provider 回显复制进 Model GET/list、Run 的 model snapshot、队列和报告证据，也不返回密文、nonce 或 key id；这不排除无关用户数据的独立字面巧合。keyring 必须与数据库分开备份；数据库与 keyring 同时泄漏时，Provider Key 仍可被解密。

Compose 不把 Provider Key 放入环境变量；它只把同一个 keyring secret 挂载给 API 与 Worker。`make setup`/`make docker-up` 会创建或验证宿主 keyring，既有普通文件会收紧到 `0600`，符号链接、目录、超大或无效 JSON 会被拒绝。切勿提交、打印或把 keyring 与数据库备份放在同一无保护位置。

旧 `api_key_env` Model 与可信本地 CLI 仍兼容。只有使用旧模式时，才应通过未提交 override 或 secret 系统把选定环境变量只注入 Worker：

```yaml
services:
  worker:
    environment:
      LOCAL_COMPAT_API_KEY: ${LOCAL_COMPAT_API_KEY:?set_in_controlled_shell}
```

不要提交 override，不要把展开后的 `docker compose config` 发到公共日志，也不要把 Key 写入 `VITE_*`、URL、命令行或日志。Web Key 会有意进入一次本机 Model POST/PATCH 请求体，但不会以明文落库。自动化、CI、Smoke 与 Phase 2 验收禁止调用真实 Provider。

可信本地 CLI 默认读取 `LLMBENCHLAB_REAL_API_KEY`，或读取 `--api-key-env` 指定的变量/隐藏输入；这条 legacy CLI 路径仍只在数据库保存变量名。环境变量和进程内存可被同权限进程读取，因此这不是 OS 级 secret isolation。`prepare` 与 `report` 不需要 Key，CI 也不得给它们注入真实凭据。

## 5. 数据库、队列与 Alembic

### 5.1 事实来源与恢复边界

- PostgreSQL 中的 Run 状态、取消意图、attempt、租约、Response、聚合、错误、dead-letter、治理 scope/minute counter、Provider attempt ledger 和 audit 是唯一权威事实。
- Redis Stream 消息只包含版本、`run_id` 和 correlation ID。通知丢失、重复或 ACK 不确定不能删除或改变数据库事实。
- API 先提交数据库，再 best-effort XADD。Redis 失败时仍返回已持久化 Run；Worker 定期扫描数据库并恢复。
- 每个执行写入校验 owner 与单调 `lease_token`；终态清除 owner/expiry/heartbeat，旧 token 永久失效。
- Worker 取得租约并启动心跳后，用工作线程读取和物化 Run/Question 快照，避免大题集同步数据库工作阻塞事件循环；completed/cancelled 以及 attempt 耗尽 dead-letter 前都从 Responses 聚合 Run 指标。

Redis 开启 AOF (`appendfsync everysec`) 只改善通知持久性，不是备份，也不能取代 PostgreSQL。

### 5.2 Migration chain

- `20260824_0000`：可执行 legacy schema。
- `20260824_0001`：Phase 1 schema、模型约束与题目 position。
- `20260825_0002`：attempt、租约、心跳、backoff、queue audit 与 dead-letter 字段/约束/索引。
- `20260827_0003`：Model credential source、AES-GCM `model_credentials` 与 Web write-only Key。
- `20260827_0004`：版本化治理 policy、四层 scope/minute counter、question execution、Provider attempt ledger、typed audit、Run fairness/backpressure 字段和 Response Provider metadata。
- `20260828_0005`：Worker progress facts 与 audit retention/exporter 有界扫描索引。
- `20260829_0006`：不改变逻辑 schema 的兼容修复；仅为早期 `0004` 历史变体补齐三个 canonical governance 索引。
- `20260830_0007`：不改变 schema、ledger 或 Provider actual usage 的数据修复；仅按显式 input/output hard reservation 与由完整上界和价格派生的 reserved cost 语义重算 `governance_scopes.overdrawn`。
- `20260830_0008`：将 `models.provider_type` 从 `VARCHAR(17)` 扩为 `VARCHAR(18)`，同时替换 Provider 类型 check 与远程配置 check，加入显式 `openai_responses` / `anthropic_messages` Adapter；旧 Model 不改写，存在新类型 Model 时 downgrade 先拒绝。

本地 SQLite 更新：

```bash
make migrate
```

命令先执行 `app.db.prepare_migrations`，再 `alembic upgrade head`。受支持的未版本化 SQLite 会在严格结构/integrity/FK 检查和一致性备份后 stamp；即使结构已与 current metadata 一致，也只 stamp 到 `0006`，确保 data-only `0007` 仍实际执行，然后再由 `0008` 扩展 `provider_type` 列宽并替换两个 Provider check。已知早期 `0004/0005` 变体只有在 revision fingerprint 为 canonical，或仅缺这三个已知索引的非空子集且最多一条 active policy 时，才会备份并交给 `0006` 修复，因此修复 DDL 中断后可安全重入。新近成为 historical 的 PostgreSQL `0005/0006/0007` 按各自规则先做 metadata drift 校验；未知 drift 在写 revision 或 repair DDL 前拒绝。`0007` 在更新 materialized flag 前拒绝任何 `reserved/send_started` reservation。普通 API/Worker 启动只检查 head，不运行 `create_all`、preflight 或 upgrade。

Compose 中只有一次性 `migrate` 服务执行：

```text
python -m app.db.prepare_migrations && alembic upgrade head && alembic check
```

`api` 与 `worker` 必须等待 migrate exit 0，然后仅执行 head check。不要把 Alembic 命令加回 API/Worker entrypoint，也不要同时运行多个 migration owner。

`0008 -> 0007` 会把 `models.provider_type` 从 `VARCHAR(18)` 收回 `VARCHAR(17)`，并恢复旧 Provider 类型 check 与远程配置 check；若存在 `openai_responses` 或 `anthropic_messages` Model，它会在第一条 DDL 前拒绝。`0007 -> 0006` 只按旧语义重算 `governance_scopes.overdrawn`，保留 reservation、actual usage、Response、audit 与 Run 终态；若有 active reservation 会在更新前拒绝。`0006 -> 0005` 是 no-op downgrade，因为三个索引本来就属于 canonical `0004`；`0005 -> 0004` 在 `worker_processes` 有任意 generation fact 时于第一条 DDL 前拒绝；`0004 -> 0003` 在任何 policy/scope/bucket/question-execution/ledger/audit 行或新 Run/Response 证据存在时同样拒绝。正常使用后的数据库不能把它们当普通代码回滚。`0003 -> 0002` 只要 `model_credentials` 存在任意行也会拒绝，避免静默丢失 Provider Key。`0002 -> 0001` 在发现 `pending` 或 `running` Run 时拒绝；它会删除可靠性元数据但保留核心实体与协议证据。完整 0008/0007/0006/0005/0004 回滚流程见 [OPERATIONS.md](OPERATIONS.md)；schema downgrade 不是 PostgreSQL→SQLite 反向同步，也不恢复 Phase 1 进程内 Runner。

### 5.3 备份与恢复证据边界

仓库当前没有自动 PostgreSQL 备份、PITR、跨主机灾难恢复或经过记录的生产恢复演练。升级、导入、truncate、volume 删除或 schema downgrade 前，操作方必须按自己的 PostgreSQL/SQLite 平台创建并验证备份；不能把“volume 存在”或本地导入测试写成恢复演练通过。

SQLite 自动收养生成的 `.bak` 只保护该 preflight 窗口，不是长期备份策略。Redis volume/AOF 也不是任务事实备份。备份含 `model_credentials` 的数据库时必须同时安排独立 keyring 备份与恢复验证；只有数据库没有可用 keyring 时，密文不可恢复，只有 keyring 没有数据库也无凭据记录。二者应分开存放并使用不同访问控制。

## 6. Docker Compose 六服务拓扑

Compose 定义六个 service，其中五个常驻，`migrate` 为一次性任务：

| Service | 角色 | 启动/健康语义 |
| --- | --- | --- |
| `postgres` | PostgreSQL 16，任务和评测唯一事实来源 | `pg_isready`；`postgres-data`；无 host port |
| `redis` | Redis 7 Streams 通知层，AOF everysec | `redis-cli ping`；`redis-data`；无 host port |
| `migrate` | 唯一 Alembic preflight/upgrade/check owner | 等 PostgreSQL healthy；成功后 exit 0，不常驻 |
| `api` | FastAPI CRUD、write-only Key 加密、Run commit 与 best-effort publish | 等 migrate 成功；只读挂载 keyring；启动只 head check；`/ready` 为容器 health；loopback API port |
| `worker` | 可横向复制的独立租约 Worker、stored Key 解密、DB reconciliation、Redis consume/ACK、DB-time process progress | 等 migrate 成功；只读挂载同一 keyring；启动只 head check；dependency-only probe；容器 stop grace 45 秒 |
| `frontend` | Nginx 静态站与 `/api/` 同源代理 | 等 API healthy；loopback frontend port；关闭 API request buffering，避免 Key body 被 Nginx 临时落盘 |

### 6.1 启动、检查和停止

```bash
make docker-up
# 等价的产品化名称
make dev-multi
# 显式副本数
make docker-up WORKERS=2
```

标准包装器校验副本数后，用同一个值导出 `LLMBENCHLAB_COMPOSE_WORKER_EXPECTED_PROCESSES` 并分阶段执行 build、依赖/migrate、Worker、API、frontend。它以全部 Compose replica（包括 exited）判断是否缩容，并另算 running replica 数；扩容/重启时先增加 Worker，要求恰好 `N` 个 fresh active generation 已有真实数据库 scan，且至少 `N-running` 个 generation 在本轮 DB-time watermark 之后启动并完成 scan，再强制重建 API 提高 expected。缩容时先重建 API 降低 expected，再 graceful scale Worker。默认 `N=2`。最后从 API container 有界轮询 `/api/v1/tasks/metrics`，只有 `worker_expected_processes=N`、`worker_registered_processes=N`、`worker_live_processes=N`、`worker_stalled_processes=0`、`worker_shortfall_processes=0` 才返回成功；scan 或 gauges 超时会保留当前 stack 供诊断，不自动删除 volume 或容器。检查：

```bash
docker compose ps -a
docker compose logs migrate
docker compose logs api
docker compose logs worker
curl -sS http://127.0.0.1:8000/api/v1/live
curl -sS http://127.0.0.1:8000/api/v1/health
curl -sS http://127.0.0.1:8000/api/v1/ready
curl -sS http://127.0.0.1:8000/api/v1/tasks/metrics
curl -sS http://127.0.0.1:8000/api/v1/metrics/prometheus
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

### 6.5 `P2-local-control-plane-v2` 资格拓扑

正式单机资格入口是：

```bash
make phase2-slo
```

它只允许精确 clean commit，并为每个 trial 创建唯一 Compose project、隔离 PostgreSQL/Redis volume 和随机 loopback 端口。默认串行执行 1 次 warm-up 与恰好 5 次 measured trial；每轮使用一个 API、PostgreSQL 16、Redis 7、两个 Worker、Demo 15 题 Mock，并固定 `lease/heartbeat/poll=30/10/1s`、Worker `max_attempts=3`、retry `base/cap=1/30s`、pool/overflow `5/5`、Run concurrency 1、backlog 4、question quantum 5、Mock delay 80 ms、input reservation 256 与 output limit 64。脚本从容器内 Settings 回读这些值，要求 PostgreSQL `max_connections >= 100`，并把只过滤 Compose project/service labels 后的 image content SHA、Host/Docker 资源、配置与数据指纹跨轮锁定；raw image ID 仅保留在 child evidence。

每个 v2 child 固定四个 measurement：seed-balanced 的 `single_worker_reference`/`configured_multi_worker_baseline`，随后固定 `warmed_pause_burst_and_drain`、`cold_start_burst_and_drain`。warmed 与 cold 的 queue/execution/E2E p95 门槛分别为 `3/5/8s` 与 `6/8/10s`；两者都要求吞吐 one-sided 95% LCB `>=6 q/s`、CV `<=20%`、drain 每轮 `<=10s`，并精确验证 `4×202 + 2×typed 429`、两个 distinct validated claim Worker 与分段 timing。每个 child 最终必须精确对账 22 completed Run、330 Response、330 QuestionExecution 和 331 reservation，并把容器、volume、network 以及本项目唯一 backend build image 清理到零；该镜像操作不允许扩展到共享 tag 或其他 Docker cache。

该 profile 的最低 Host/Docker 资源分别是 8 logical CPU + 8,000,000,000 bytes RAM 和 8 CPU + 4,000,000,000 bytes memory。它描述的是一台主机、一个故障域的 Mock 控制面，不是生产部署模板；不要通过修改 profile、降低断言或在共享 GitHub-hosted runner 上追求绝对数值。Hosted CI 只验证 validator、统计和失败路径。

raw child 与 aggregate evidence 都保留在 Git 忽略的 `.pytest_cache/artifacts/phase2-slo/`。v2 aggregate schema 是 `llmbenchlab-phase2-slo-evidence-v2`，只复制 commit/hash、稳定指纹、匿名参与计数、SLI/统计/判定、ledger projection 和 cleanup 摘要等 allowlist，不复制 raw identity、stdout/log、DSN/URL、环境变量、题目、Prompt/Response、keyring 或 Provider 数据。child 使用独立进程组；超时/中断后允许 scoped cleanup 最多 420 秒。运行结束仍须核对 evidence 中容器、volume、network 和项目镜像零残留，并把 artifact 当内部运维数据保护。公开记录可以给出 Git 忽略的外层 aggregate 相对路径和内容 SHA；raw child、aggregate 内嵌 trial child 路径、环境/配置明细不得原样发布，只能摘录人工复核后的 commit、匿名统计与支持边界。

历史 `P2-local-control-plane-v1` 在 clean `dfa67abb1a9a0418a7e3337c179f816e3c69f121` 上只通过 15/18 项 SLO，保持 `unqualified`，不能用 v2 追认。当前 v2 在 clean `b6a35fef1dd069ebb54b69955058915c722aa34d` 上从全新 warm-up 开始完成恰好 5 个 measured trial，discarded trial 为 0，23/23 项 SLO、每轮 hard invariant 和 cleanup 均通过，容量模型为 `qualified`；每个 child 的项目镜像 cleanup 都精确为 candidate/removed/retained/remaining `1/1/0/0`，容器、volume 和 network 也为 0。aggregate 内容 SHA-256 为 `a76d167bb664e2ee3ee7514c39ac738b76cef37776d7b66e1175a8596329d0d9`。同一实现 SHA 的 [GitHub Actions run 33146681285](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33146681285) 4/4 必需 job 成功。这只记录该精确单机 Mock profile 的资格，不是生产部署、真实 Provider 性能、HA/SLA 或 Phase 2 整体完成。

## 7. SQLite→PostgreSQL 单向导入 runbook

导入器是显式、一次性的 13 表复制，不会自动在应用启动时运行，也不会反向同步。`model_credentials` 含认证密文，ledger/audit/worker progress 又含运行、费用和故障时间元数据；工具输出仍只包含行数与摘要，但源、目标和数据库备份必须作为敏感数据保护。

### 7.1 前置条件

1. 停止源 SQLite 的 API、Worker 和所有写进程；同时停止目标 API/Worker 和用户入口。导入器只能保证自身连接只读，不能证明外部写进程已停止。
2. 按组织要求创建并独立验证源与目标备份。仓库没有生产备份/恢复演练，不得把本 runbook 描述为已验证灾难恢复。
3. 源必须是文件型 SQLite、处于当前 Alembic head；不能有 `pending` 或 `running` Run、`reserved/send_started` attempt，或按同一 DB UTC cutoff 仍 live 的 Worker generation。stopped/stale generation 会精确复制。导入器会在打开目标前用全部 never-delete ledger 重算所有 scope 与 minute-bucket 的 active/reserved/consumed/overdrawn 事实；物化值任何高、低漂移、scope 类型错配或缺 bucket 都在接触目标前拒绝。
4. 目标必须是当前 head 的 PostgreSQL，13 张核心/治理表必须为空。先由唯一 migrate owner 完成 schema，再保持 API/Worker 停止。
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
2. 检查 head，随后对 `alembic_version` 与 13 张核心/治理表获取 `ACCESS EXCLUSIVE` lock，再次检查 head 与空表。
3. 按依赖顺序复制 `governance_policies`、`models`、`model_credentials`、`benchmarks`、`questions`、`governance_scopes`、`evaluation_runs`、`evaluation_responses`、`governance_minute_buckets`、`question_executions`、`provider_call_reservations`、`audit_events`、`worker_processes`。
4. 提交前比较行数、主键集合 SHA-256 与 canonical row SHA-256；失败整体 rollback。
5. COMMIT 成功确认后，在单个 `REPEATABLE READ`、`READ ONLY` 事务中做 post-commit snapshot。

成功输出三组、每组 13 行的 content-free 摘要：`phase=source`、`phase=precommit_target`、`phase=postcommit_target`。每张表的 `row_count`、`pk_set_digest` 和 `canonical_row_digest` 必须三阶段一致。摘要不打印行内容、密文或 URL，但行数/hash 仍是敏感运维元数据。导入后还必须把与源库匹配的 keyring 通过目标环境的 secret 流程单独交付给 API/Worker；导入器不会复制 keyring 文件。

### 7.5 Exit code 与恢复动作

| Exit | 状态 | 数据语义 | 必需动作 |
| --- | --- | --- | --- |
| `0` | completed and reconciled | COMMIT 已确认，post-commit 摘要匹配 | 保存脱敏摘要；检查 head/ready 后再启动 API/Worker |
| `2` | pre-commit failure | preflight/copy/提交前对账失败；目标事务若已开始会 rollback | 保留错误与源；确认目标仍为空并修复原因后，才考虑重新执行 |
| `4` | `commit_outcome_unknown` | PostgreSQL 未确认 COMMIT；原子事务意味着目标可能为空，也可能已完整提交 | 立即停止；保持应用停机，检查目标 13 表和摘要；禁止盲目重试、truncate 或覆盖 |
| `3` | `committed_but_verification_failed` | COMMIT 已确认，但 post-commit snapshot/比较或摘要输出未完成 | 将目标视为已提交；保持只读检查并补做对账；禁止重新导入或清空 |

exit 4 时，只有在独立检查证明 13 表仍为空后才可按新变更重新运行；若非空，按“可能已完整提交”保护现场。exit 3 已明确提交，不得把它当成 rollback。任何不确定状态都应升级给数据库负责人，而不是靠重复命令猜测。

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

probe 检查数据库连接、Alembic head 与队列能力：DB/head 或队列配置错误 exit 1；Redis 运行时不可用但 DB reconciliation 可用时输出 `degraded` 且 exit 0。输出明确包含 `probe_scope=dependencies_only`、`main_loop_progress=not_checked`。它不写 progress，也不观察当前 Worker 主循环、lease heartbeat、事件循环卡死或执行吞吐；主循环 liveness 必须使用数据库 `worker_processes` 聚合与 shortfall 告警。

### 8.3 结构化日志

LLMBenchLab 应用 logger 输出单行脱敏 JSON，包含 allowlist event、request/correlation ID、run/question、worker、attempt、lease token、message ID、结果和固定的异常存在标记 `exception_type="suppressed"`；它不反射具体异常类名。它不应记录 Authorization、DSN/Redis URL、请求正文、Provider 请求/响应正文、完整题目或原始模型输出。API 始终生成服务端 UUID request ID，不信任或回显客户端 `X-Request-ID`；Run correlation 默认稳定使用 Run ID。

应用配置同时治理 Uvicorn、SQLAlchemy 等已知进程内 Python logger：消息/异常只通过固定 allowlist 投影，Uvicorn access log 默认关闭，未知 extra 不进入 JSON。Alembic migration CLI 使用独立 `fileConfig`/console 输出，不经过应用 sanitizer；该边界也不能约束反向代理、PostgreSQL、Redis、Docker daemon、驱动原生日志或崩溃转储，因此秘密绝不能放入 URL、argv、迁移输出或环境诊断。新增 logger 调用必须使用 literal message，不能把 exception/响应/请求对象插值进 message。

### 8.4 Task metrics

`/api/v1/tasks/metrics` 从 PostgreSQL 当前行派生：pending/due/running/expired/cancel/retry/dead-letter/queue error，managed backlog/governance/attempt gauges，以及 Worker expected/registered/live/stalled/shortfall 与最近聚合进展时间。它是只读 gauges，不参与调度、不覆盖数据库状态。

`/api/v1/tasks/history?window_hours=1..2160` 在同一 PostgreSQL `REPEATABLE READ READ ONLY`/显式 SQLite 读快照中确定 DB UTC 窗口，逐条验证 retained audit 的 contract/hash/identity/retention 后聚合 counter，再从同一快照的 Run 时间字段计算 queue/execution/end-to-end p50/p95/p99。损坏 audit 使整个请求返回 `500 audit_event_integrity_error`，不提供部分数据；每类延迟最多 10,000 个样本并显式报告 `truncated`。`/api/v1/runs/{run_id}/audit` 提供稳定分页的应用 append-only audit；它不是 WORM 或数据库管理员防篡改证明。

`/api/v1/metrics/prometheus` 提供固定低基数、全部 gauge 的 text exposition；每进程单次 collection、15 分钟 audit hard cap 和 1 小时 latency cap 用于约束抓取压力。示例 scrape/八条规则在 `deploy/observability/`，仓库不部署 Prometheus、Alertmanager、通知发送器或生产 SLO。具体阈值与逐条响应见 [OPERATIONS.md](OPERATIONS.md)。

### 8.5 Audit retention maintenance

operational/security audit 分别至少保留 90/365 天，清理不在请求链路执行。使用 `llmbenchlab-audit-retention` 的 `archive/verify/reconcile/restore/delete`；输出目录必须由当前维护用户拥有且不可 group/other 写，建议 `0700`，archive 文件固定 `0600`。`verify` 完全离线；mutation 必须在 API/Worker/audit writers 停止的维护窗口使用 archive 精确 SHA-256，并在 exit `3/4` 时先只读 reconcile、禁止盲目重试。完整命令与失败语义见 [OPERATIONS.md](OPERATIONS.md)。

## 9. 升级、重启与回滚

### 9.1 升级顺序

1. 阅读 Changelog、ADR 和 migration；确认协议版本没有被无提示改变。
2. 停止创建新 Run，等待 active Run 完成或显式取消。
3. 停止 API/Worker；按平台流程创建并独立验证备份。仓库当前没有可引用的生产恢复演练。
4. 安装锁定依赖或构建镜像。
5. 只由 `make migrate` 或 Compose `migrate` 服务执行 preflight/upgrade/check。
6. 启动 API/Worker/frontend，检查 head、`live/health/ready`、Worker probe 和 task gauges。
7. 运行 `make smoke`；涉及可靠性/Compose 变更时运行 `make phase2-acceptance`，涉及治理、Worker 数或快速性能回归时运行 `make phase2-capacity`。需要发布资格时，在最终 clean commit 上另运行固定的 `make phase2-slo`，保留 aggregate/child evidence SHA，并关联同一精确 SHA 的 required CI。

### 9.2 API、Worker 与 Redis 重启

- API 重启不会拥有、取消或重新创建 Worker 租约；Run/Response 保持在数据库。
- Worker 优雅停止先使用 grace；异常退出则等待 lease 自然过期。新 Worker 以递增 token 恢复缺失 Response，旧 token 写入被拒绝。
- 失效 token 的 `reserved` Provider attempt 做 pre-send release，`send_started` 做一次 conservative settlement；旧 owner 可 CAS 结算已发生消费，但不能继续发请求或写 Response。普通明确 pre-send release 会保留终态 ledger、开启新 ledger generation 并重试当前未发送 ordinal，不重置已经发送的较小 HTTP retry；租约接管已单独推进 generation，reconciler 不二次推进。
- Run 终态/defer/exhaust 转换先在短事务提交，再对该 lease 做 post-commit ledger reconcile；完整性失败时保留已提交 Run 状态，向 Worker 报错并用独立短事务尽力写固定 integrity event。过期 lease takeover 在新 owner 提交后若旧 ledger 校验失败，会撤销新 lease 并使 Run fail closed，不允许接管 Worker 外发。
- Redis 重启、清空或 ACK 丢失可能造成延迟/重复通知，但 DB reconciliation 和幂等唯一约束维持正确性。
- 最后一题已提交但 finalize 前崩溃时，reconciliation 从完整 Response 重新聚合，不再次调用 Provider。
- attempt 耗尽或过期租约进入 failed/dead-letter 时也先聚合已有 Responses，使 API 的诊断性部分指标与证据一致；报告仍会防御性重算并用 `metrics_provenance` 标记旧字段漂移。

这些语义已在隔离双 Worker Compose 验收中覆盖，但不构成多主机 HA、容量或恢复时间 SLA。

### 9.3 Schema/code 回滚

代码回滚必须与当前 schema、API 和 `protocol_version` 兼容。回退 `0005` 前必须停止 Worker并处理/保存 `worker_processes` facts；任一 generation 行都会在 DDL 前拒绝。回退 `0004` 前还必须停止 admission/API/Worker、对账 active reservation，并归档/核验全部治理、ledger、audit 和 Provider metadata；任一证据存在时同样拒绝。优先向前修复或恢复经验证的旧备份，同时单独保留新 schema 证据。回退 `0003` 前还必须确认 `model_credentials` 为空；回退 `0002` 前必须确认没有 `pending/running`。详见 [OPERATIONS.md](OPERATIONS.md)。

不同 protocol version、Benchmark version 或 dataset hash 不能因回滚无提示混排。优先使用经过平台验证的完整备份恢复，而不是盲目 downgrade；本仓库当前没有 PostgreSQL→SQLite 自动回退或生产恢复演练。

## 10. 当前限制与生产前工作

| 领域 | 当前可靠执行基础 | 生产前仍需 |
| --- | --- | --- |
| 身份与权限 | 无鉴权，所有端点可读写 | 登录/API Token、RBAC、对象授权、管理员导入/Model 权限、审计 |
| 网络 | API/frontend loopback；PG/Redis 内部 Compose 网络；Provider Chat/Responses/Messages 真 SSE 客户端 | TLS 反向代理、可信 Host/代理、Worker→Provider 全链路 SSE flush/buffering/timeout 核对、网络策略、认证与安全 headers |
| PostgreSQL | 单实例、named volume、迁移/故障测试 | 托管/HA、TLS、最小权限角色、加密、备份/PITR、RPO/RTO 与真实恢复演练 |
| Redis | 单实例 AOF、非权威通知层 | 认证/TLS、HA/容量/保留策略、监控；继续保持 DB 事实来源 |
| Worker | 租约/心跳/fencing/重试/取消；数据库权威四层治理、attempt ledger、背压/公平 slice；DB-time scan/claim/progress/heartbeat aggregate；标准 Compose 默认双 Worker且按方向扩缩并校验 expected/registered/live/stalled/shortfall；真实双 Worker Mock 基线 | 滚动排空自动化、三个以上 Worker/真实 Provider 的独立容量与成本规划 |
| Secrets | Web write-only；AES-GCM 密文入库；API/Worker 共享独立 keyring；legacy env 可用 | 身份/对象授权、KMS/HSM、短期凭据、批量重加密、轮换审计与每进程最小权限 |
| SSRF/数据外发 | 远端 HTTPS、HTTP 仅 loopback、禁重定向与有界正文 | allowlist、DNS/IP 验证、出站代理、元数据阻断、外发审批 |
| 可观测性 | 受控 JSON 日志源、组件健康、DB gauges、typed audit/history、固定低基数 Prometheus exporter、8 条规则、Worker progress、逐题安全 Provider metadata | 受认证 Dashboard、统一 traces、告警发送/值班集成、生产 SLO、数据库管理员级不可篡改审计 |
| 数据保护 | 13 表单向 importer/hash 对账；canonical audit archive 的 offline verify/精确 restore-delete | 静态加密、签名/WORM、备份/PITR、灾备演练、合规删除与受控导出 |
| 供应链 | lockfile、基础 CI、版本标签镜像 | Action SHA/镜像 digest、漏洞门禁、SBOM、签名与 provenance |
| 性能/HA | 真实故障正确性验收、指定硬件/commit 的 PostgreSQL16/Redis7/双 Worker Mock 基线，以及 clean `b6a35fe…` 的固定单机 v2 1+5 资格 evidence | 另做真实 Provider 基线、多主机故障、滚动升级和恢复时间验证；现有结果不是生产 SLA/HA |

Compose 可靠性验收只证明当前最小垂直切片在指定故障下保持数据库事实、逐题唯一性和协议 v1 评分；它不授权公网发布，也不把 Phase 2 标记为 completed。详细测试命令见 [TESTING.md](TESTING.md)，安全边界见 [SECURITY.md](SECURITY.md)，架构决定见 [ADR-0005](decisions/ADR-0005-durable-task-execution.md)。
