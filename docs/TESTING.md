# LLMBenchLab 测试指南

## 1. 测试原则

LLMBenchLab 的默认验收路径必须完全离线、可重复且不产生模型费用。自动化测试和 CI 只能使用 `MockModelAdapter`，或使用进程内 `httpx.MockTransport` 验证 OpenAI-compatible 协议；不得访问真实 Provider，不要求 API Key，不把“本机碰巧可用”当作通过证据。

每次报告测试结果时必须写明实际命令、通过/失败、测试数量、失败原因和未运行项。没有执行的 Docker、浏览器或联网验证不能写成通过。

## 2. 测试分层

| 层级 | 目的 | 当前入口/文件 | 外部网络 |
| --- | --- | --- | --- |
| 后端单元测试 | Evaluator、Adapter、Loader/Hash、固定标准数据转换的边界语义 | `backend/tests/test_evaluators.py`、`test_adapters.py`、`test_dataset_loader.py`、`test_standard_datasets.py` | 禁止；Provider 用 MockTransport，下载用注入 fixture fetcher |
| 正式流程组件测试 | CLI 秘密/确认/编排、模型发现/canary、完整报告和大题集有界 task | `test_evaluation_cli.py`、`test_provider_preflight.py`、`test_run_report.py`、`test_evaluation_runner_reliability.py` | 禁止；只用 fixture、MockTransport、Mock Adapter、临时数据库 |
| API 与进程边界测试 | FastAPI Schema、状态码、秘密安全、Run 提交与 API/Worker 分离 | `backend/tests/test_api.py`、`test_run_dispatch.py`、`test_process_boundaries.py` | 禁止 |
| Governance / audit 测试 | full-document policy/hash/Run overrides、四层 admission/ledger 派生物完整性、fail-closed Token/cost、pre-send generation、公平 slice、typed history/audit、Provider metadata 与 credential audit | `test_governance.py`、`test_governance_api.py`、`test_audit_api.py`、`test_task_history_api.py`、`test_response_metadata_api.py`、`test_credential_audit.py` | 禁止；SQLite/Mock/MockTransport |
| 租约与 Worker 测试 | 条件领取、fencing、心跳、取消、幂等 Response、重试/恢复、队列 ACK | `test_run_leases.py`、`test_evaluation_runner_reliability.py`、`test_worker.py`、`test_task_queue.py` | 禁止；SQLite/假队列 |
| 迁移与导入回归 | SQLite/真实 PostgreSQL migration、0004 populated downgrade refusal/空库往返，以及 12 表 SQLite→PostgreSQL 原子导入 | `test_migrations.py`、`test_sqlite_postgres_import.py` | 导入/本地部分禁止；真实 PostgreSQL 用 `integration` marker |
| 真实基础设施集成 | PostgreSQL 并发领取/取消竞态、Redis Streams PEL/ACK/重复投递 | `backend/tests/integration/` 与 importer 的 `integration` 用例 | 只连接显式测试 PostgreSQL/Redis；禁止 Provider |
| Mock 端到端 Smoke | API 提交 pending Run → 独立 WorkerService → Responses → Leaderboard/Metrics | `backend/tests/test_smoke.py`，marker 为 `smoke` | 禁止 |
| Compose 故障验收 | 六服务拓扑、双 Worker、进程/Redis/lease 故障、治理/ledger、取消、重复消息与 0004 安全回滚 | `scripts/phase2_acceptance.py` / `make phase2-acceptance` | 只拉取/构建基础镜像；模型执行始终为离线 Mock |
| Mock 容量基线 | 真实 PostgreSQL 16/Redis 7、全有限 policy、1/2 Worker、精确 `202/429` backlog、cooperative quantum、跨 Model 公平性、lease/Redis/重复通知故障及 DB/queue/ledger/audit 对账 | `scripts/phase2_capacity.py` / `make phase2-capacity` | 只拉取/构建基础镜像；模型执行始终为离线 Mock |
| 前端单元/组件测试 | 格式化、状态/指标、错误/空态、主要页面交互 | `frontend/src/**/*.test.ts(x)`、`frontend/tests/` | API 必须 stub/mock |
| 静态检查 | Python lint/format、ESLint、TypeScript | Ruff、ESLint、`tsc` | 不需要 |
| 构建检查 | 确认生产前端可编译打包 | `npm run build` | 安装完成后不需要 |
| 配置检查 | 校验 Compose 插值和服务定义 | `docker compose config` | 不启动 Provider |

单元测试定位纯逻辑错误；真实 PostgreSQL/Redis 集成测试验证方言和队列语义；Smoke 证明最小离线链路；Compose 验收覆盖真实独立进程故障；capacity harness 记录指定环境的有界 Mock 吞吐与延迟。五者不能互相替代。四层治理、逐 attempt ledger、背压/公平调度、typed audit/history 和 Provider metadata 已有自动化覆盖；增强 capacity harness 的代码合同也已写入，但新的完整真实基础设施 evidence 仍待运行。Phase 2 仍为 `in_progress`，因为本地结果和历史 dirty evidence 都不能替代阶段 commit 的精确 SHA 远程必需检查与正式阶段收尾。

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
uv sync --python cpython --frozen --extra dev
uv run python -m app.db.prepare_migrations
uv run alembic upgrade head

cd ../frontend
npm ci
```

要求 `uv`、Node.js/npm；本地后端命令由 `uv` 选择 Python `>=3.11` 的 CPython。若 lockfile 尚未生成或有意更新依赖，只能在依赖变更任务中使用非 frozen 安装，并把新的 lockfile 与原因一并 Review；日常 CI 不应静默改写 lockfile。

## 4. 统一命令

在仓库根目录：

```bash
make test       # 后端 pytest + 前端 Vitest
make lint       # Ruff + ESLint + TypeScript 类型检查
make smoke      # 只跑完全离线的后端垂直切片
make phase2-acceptance  # 隔离的真实 Compose 八场景可靠性验收
make phase2-capacity    # PostgreSQL 16/Redis 7/1→2 Worker 的 Mock 容量基线
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

不要把真实 Provider Key 放入上述环境。这里的 PostgreSQL 必须是可破坏的专用测试库：integration fixture 会级联清空该 management database 的核心/治理表。Importer 集成测试另在同一 loopback 服务器上创建正则约束的随机专用数据库并在结束时精确删除，不会替你保护或清空 management database。绝不能把开发、共享或生产数据库 DSN 传给这组命令。

常用的目标化命令：

```bash
cd backend
uv run pytest tests/test_evaluators.py
uv run pytest tests/test_dataset_loader.py
uv run pytest \
  tests/test_evaluation_cli.py \
  tests/test_standard_datasets.py \
  tests/test_provider_preflight.py \
  tests/test_run_report.py \
  tests/test_evaluation_runner_reliability.py
uv run pytest tests/test_api.py tests/test_smoke.py
uv run pytest -q \
  tests/test_governance.py \
  tests/test_governance_api.py \
  tests/test_audit_api.py \
  tests/test_task_history_api.py \
  tests/test_response_metadata_api.py \
  tests/test_credential_audit.py \
  tests/test_phase2_capacity_script.py \
  tests/test_adapters.py
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

这只验证配置渲染，不证明镜像已构建、migration 已完成或服务健康。普通启动验证使用 `make docker-up`、检查 `/api/v1/live`、`/health`、`/ready` 和 Worker probe，最后执行 `make docker-down`。真实故障验收使用 `make phase2-acceptance`，容量基线使用 `make phase2-capacity`；两者都创建唯一 Compose project、随机 loopback 端口和隔离卷，失败路径也执行精确 `down -v` 并检查无项目残留。不得对日常项目名手工套用其清理命令。

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
- Mock 可注入分类错误，用于验证单题故障隔离；latency/Token/usage shape 等确定性本地配置在 reserve 前全部验证，非法配置断言治理 hook 零调用，成功与模拟 Provider error 断言 reserve→mark→actual/conservative finish 顺序。
- OpenAI-compatible 的 Chat Completions URL、messages、`Accept: text/event-stream`、`stream:true`、`stream_options.include_usage:true` 与 `temperature/top_p/max_tokens/seed`；数字 `max_tokens` 原样发送，`null` 时请求体完全省略该字段。
- usage 缺失时 Token 字段为 `null`。
- 429、选定 5xx、网络超时的有限指数退避，以及普通 4xx 不重试。
- 每个 OpenAI-compatible HTTP retry 都经过 request-local reserve→mark-send-started→finish hook；pre-send 失败 release，usage 完整 actual，transport/usage 缺失 conservative，settlement unknown 停止后续外发。
- 远端 HTTPS 强制、loopback HTTP 例外，以及在发送 Key 前拒绝远端明文 HTTP。
- 真 SSE 的任意网络/UTF-8 拆包与合包、LF/CRLF/独立 CR、BOM、comment ping、多 `data` 行、role/null delta、reasoning/timings 扩展忽略、finish 后继续读取可选 usage-only 块至 `[DONE]`，以及普通 JSON fallback。
- 非法 UTF-8/JSON/字段、SSE 内 Provider error、缺失 `[DONE]`、transport 中断的有限重试，以及同一 Adapter 并发请求的 request-local 状态隔离。
- 模型发现与 Chat 的 `Accept-Encoding: identity`、读取前拒绝压缩，以及发现 2 MiB、Chat JSON 4 MiB/错误 64 KiB、SSE wire 64 MiB/单事件 1 MiB/聚合 content 4 MiB 的上限。
- legacy Key 环境变量缺失、write-only direct `SecretStr`、空 Provider 回答、非法配置与错误脱敏；`finish_reason="length"` 的空输出分类为 `output_truncated`，普通空输出仍为 `empty_response`；成功内容、raw usage 键/字符串值、request ID、返回模型名、fingerprint 和 finish reason 的当前 Key 精确替换，包括 Key 横跨 SSE delta 的聚合后替换。
- 最终 `httpx.TransportError` 的安全 `AdapterError` 同时断言 `__cause__ is None`、`__context__ is None`，格式化 traceback 不含带 Authorization request 中的 canary Key。

OpenAI-compatible 测试只给进程内 transport 使用虚构 token；不得把测试地址改为真实域名。

### 5.3 Dataset Loader 单元测试

`test_dataset_loader.py` 覆盖 manifest/JSONL Schema、错误行列和 JSON Pointer、UTF-8、重复键/ID、题数、Evaluator 兼容、NaN/Infinity、稳定 SHA-256，以及：

- ZIP 路径穿越、绝对/嵌套路径、重复/额外成员。
- symlink/非普通文件、加密或不支持压缩。
- archive/member/单行/题数上限与高压缩比。
- 内置 Demo 为 15 道原创双语题，含三种题型并带非正式标识。

新增数据格式字段、Hash 规则或限制时，必须先更新协议/Schema 文档，再增加兼容与拒绝测试；不能无版本变化地改变既有 Hash。

### 5.4 标准数据、Provider preflight 与报告测试

`test_standard_datasets.py` 不访问 Hugging Face 或 GitHub。测试在内存构造小型 Parquet/CSV/ZIP，用注入 fetcher 替代下载，并覆盖：

- 固定源 SHA 不匹配拒绝、已验证缓存复用、确定性 ZIP 与 Dataset Hash；
- MMLU-Pro `direct` 与 `official_cot`、同 category 5-shot、group/limit 和不同转换配置身份；
- GPQA-Diamond 内层 CSV Hash、198 行约束、逐 Record ID 确定性选项重排、seed/domain 筛选，以及不携带作者/解释字段；
- 输出 ZIP 再由普通 dataset-v1 Loader round-trip 校验。

`test_provider_preflight.py` 只使用 `httpx.MockTransport`，覆盖 `/v1` 与完整 `/chat/completions` 的 `/models` 推导、远端 HTTP 拒绝、identity-only/2 MiB 发现响应、压缩体读取前拒绝、认证错误脱敏、发现模型 ID 反射当前 Key 时失败、唯一/多模型选择、最小 Chat canary 使用同一流式 payload/响应 Adapter、finish reason 脱敏，以及 canary 明确返回不同模型时失败。它不能证明任何真实 Provider 兼容，也不应改为读取开发者环境 Key。

`test_evaluation_cli.py` 只做离线编排，覆盖无 `--api-key`、环境/隐藏输入生命周期、确认口令、含 HTTP retries 与剩余 Run attempts 的请求上界、profile 默认值、active Run 早拒绝、Run 创建/恢复/报告顺序、过期 incomplete lease 的 fenced reclaim 和 Key 值不持久化。它验证的是本地控制流，不证明真实 Provider 或操作系统级独占；人工 runbook 仍必须先停常规 API/Worker。

`test_run_report.py` 使用临时数据库覆盖分页导出全部 Response、非重叠分组、三文件内容、目标拒绝覆盖、文件权限与秘密脱敏。回归还构造 failed Run 的陈旧汇总字段，验证 `summary.metrics` 从计划题与 Responses 派生、`metrics_provenance` 标出漂移，并与 groups/responses 保持同一口径。报告含题目和 raw response，因此测试 fixture 必须完全虚构。

### 5.5 可靠执行单元与仓储测试

可靠执行测试必须把 PostgreSQL 中的 Run/Response 当作唯一事实，Redis 消息、日志和内存状态都不能覆盖它。当前离线测试覆盖：

- 两个领取者竞争同一 Run 时只有一个有效 owner/token；旧 token 的 heartbeat、逐题写入和 finalize 都被 fencing 拒绝。
- 每题 `(run_id, question_id)` 唯一；重复投递、ACK 结果未知、Worker 接管和重跑不会双写 Response 或重复增加进度。
- pending/running 取消、自然租约过期、完整证据直接聚合、有限退避，以及 attempt 耗尽 dead-letter 前聚合已有部分 Responses。
- API 只在数据库 commit 后 best-effort 发布通知；commit 失败不 publish，Redis 发布失败仍保留可由数据库 reconciliation 找回的 pending Run。
- policy GET 初始化前返回无副作用 `404 governance_policy_not_initialized`；PUT 要求全部 20 个字段，相同内容幂等、历史内容重激活原版本。Run admission 原子检查 backlog，并冻结 policy ID/version、完整 policy hash、opaque provider scope、question quantum，以及 `input_token_reservation` 和 lifetime request/Token/USD 四项 override；active attempt 必须使用这份 Run 快照，不能切换到后来激活的 policy。
- global/provider/model/run 四层按固定锁序共同裁决 concurrency/fixed-minute request+Token/lifetime budget；hard Token/TPM 缺 input reservation 或有限 `max_tokens`、hard cost 缺价格时在 Provider 外发前 fail closed。每次 reserve/send-start/finish/reconcile/lease renew 都从完整 ledger 重算四层 scope 与 minute bucket，任何高报、低报或缺 bucket 漂移都回滚并另写最小 `governance_integrity_error`，失败事务不留下部分 reservation/counter。
- attempt ledger 覆盖 `reserved → send_started → settled_actual|settled_conservative` 与 retained `released_pre_send`。pre-send 重试递增 QuestionExecution generation 并保留当前尚未外发的 provider ordinal；已经到达 `send_started` 的较小 ordinal 仍被消费，不能因接管重置。HTTP retry、lease takeover、取消、重复投递和 commit-unknown 重放不 double-count，失租 `send_started` 只保守结算一次。
- Run 终态/defer/exhaust 等状态先提交，再 reconcile active ledger；reconcile 完整性失败必须保留已提交 Run 状态、向调用方失败并写独立 integrity event。过期租约接管若旧 ledger reconcile 失败，必须撤销新 lease 并把 Run 收敛到 failed/exhausted（有取消意图时为 cancelled），且不得外发 Provider 请求。
- backlog 满稳定 429；rate/concurrency defer 不生成 0 分 Response；每个 lease 只新增 frozen question quantum，cooperative yield 不增加 `failed_attempt_count`，最久未获服务 Run 优先再调度。
- typed audit 的唯一 event key/payload hash、90/365-day retention、稳定 Run audit 分页，以及 history window/p50/p95/p99 截断语义；history 必须在单一一致性快照中校验窗口内每个 event 的 contract、payload hash、identity、retention 和数值边界，任一损坏使整个响应以 `audit_event_integrity_error` fail closed，不能返回局部统计或反射损坏 payload。credential change/reject/decrypt failure 不含 Key/origin/envelope，Provider metadata 经过安全归一化进入 Response/API/report。
- Worker 每次只执行一个 Run，按配置心跳；优雅停止在 grace 内等待，超时后由租约过期恢复，而不是伪造成功 ACK。
- Runner 对大题集只创建至多 `concurrency` 个消费者 task；当前可靠性用例以 2,000 题验证并发 4 的固定 task 集，并验证取消/失租停止后续取题和 Adapter 只关闭一次。另有阻塞快照物化用例验证同步加载被移出事件循环，租约心跳可在加载期间继续运行。
- API 进程不持有 Runner/task manager；只启动 API 时 Run 保持 `pending`，启动独立 Worker 后才执行。

SQLite 测试适合快速验证状态机和兼容路径；跨连接并发保证、锁和数据库时钟必须由真实 PostgreSQL 用例补充，不能用 SQLite 通过代替。

## 6. API、迁移与真实基础设施测试

### 6.1 API

`backend/tests/conftest.py` 在导入应用前设置一个临时目录中的 SQLite URL。每个 API client fixture 会重建表，因此测试不会读写开发数据库 `backend/data/llmbenchlab.db`。

当前 API 集成路径验证：

- `/live` 不访问数据库、Redis 或 Provider；`/health` 只检查数据库；`/ready` 分别报告数据库、Alembic head 与队列状态；`/info` 保持 `llmbenchlab-protocol-v1`。
- Redis 不可用时 `/ready` 返回脱敏的 `503 degraded`，但数据库正常时 `accepting_runs=true`、database reconciliation 可用；数据库或 schema 不可用时返回 `not_ready` 并停止接受新 Run。
- 服务端生成并回传 UUID `X-Request-ID`，忽略客户端同名值，防止调用方把 write-only Key 复制到 header 后迫使日志/响应反射；未知路径只记录 `<unmatched>`，不把用户路径或请求正文写入应用日志。
- `/tasks/metrics` 从数据库派生任务、governance backlog/delay/exhaustion、active Provider attempt、overdrawn scope 与 attempt/failure/dispatch gauges；`/tasks/history?window_hours=1..2160` 从 retained typed audit/Run 时间聚合 counters 与 queue/execution/end-to-end p50/p95/p99，并验证 10,000 样本上限/`truncated`。history 的 DB 时钟、窗口、事件和三组 latency 查询必须共享同一事务快照（PostgreSQL `REPEATABLE READ READ ONLY`；SQLite 显式 `BEGIN`）；任一 event contract/hash/identity/retention/数值损坏都使整个响应以 500 `audit_event_integrity_error` fail closed。pending/running cancel 都贡献 `run_cancel_requested`，dead-letter 使用专用 `run_dead_lettered`，不会与一般 terminal counter 混淆。
- governance policy GET 在初始化前无副作用返回 `404 governance_policy_not_initialized`，PUT 必须提交全部 20 个字段；相同内容幂等、历史内容重激活原 ID/version。整数限制覆盖各字段准确边界；所有 USD 限制公开上限均为 `10000000.00000000`，请求接受 Decimal-compatible JSON number 或 decimal string，响应固定为 JSON string。SQLite 回归验证该上限内 binary64 spacing 小于半个 `0.00000001` 量子，八位小数可按相同 Decimal round-trip；PostgreSQL 仍以 `NUMERIC(20,8)` 为并发生产门禁。Run audit 端点按 `(occurred_at,id)` 稳定分页并对 retained event identity/payload/retention 做读取期完整性检查。
- Model CRUD、分页、Provider 必需字段、远端 HTTP 拒绝/loopback 例外和名称冲突。
- Model POST/PATCH 接受 8–8192-byte visible-ASCII write-only `api_key`，并拒绝 7-byte、空白、非 ASCII 与过长输入；GET/list 的凭据相关字段只返回非秘密状态，不返回 Key、密文、nonce 或 key id。marker 测试覆盖 201、422、409、503、500、Host 与 request-ID 反射路径。
- create/PATCH 会对新 Key 或保存旧 Key 执行精确 `ModelRead` 全字段及 Run snapshot `model` 子投影重复检查，包括生成 ID/时间戳、数值和默认参数；测试不把该保证扩大到无关 Benchmark/Question 字面巧合。缺行、未知/旧 `key_id` 或损坏 envelope 可在 active keyring 可用时通过隔离的新 Key PATCH 修复，或只切换 Mock/legacy env 清理；夹带无关公开修改返回 422，无新 Key 保留 stored 则稳定 503，且两种失败均保持事务不变。
- stored credential 用 AES-GCM 随机 nonce 和 Model/origin AAD；Worker 的合法三态、错误 keyring、缺行、跨模型/跨 origin/篡改 snapshot 均在 Adapter 构造前 fail closed，legacy env 路径继续通过。
- credential change/reject/decrypt failure 使用数据库 UTC 写入 security-retention typed audit；拒绝业务事务回滚后仍可另行持久化，payload 明确不含 Key、origin 或 envelope。Run `created_at`/`finished_at` 与相应 lifecycle audit 也以数据库 UTC 对齐，跨进程历史不依赖主机墙钟。
- SQLite 并发测试断言 Model PATCH 与 Run create 在读取 Model 前以 `BEGIN IMMEDIATE` 串行化；PostgreSQL integration 断言两条路径共用 Model row `FOR UPDATE` 锁。SQLite 竞争期间允许请求短暂等待，这仍只是低并发本地模式；生产/并发评测门禁使用 PostgreSQL。
- SQLAlchemy 基本 CRUD 与外键/Schema 基线。
- Run 创建 `202`、取消、轮询、逐题证据、汇总和排行榜；API 提交不在进程内执行 Adapter。生成边界测试覆盖兼容默认 `max_tokens=256`、显式 `null`、数字上限 `131072`、读取超时默认 `60s`/上限 `1800s`，并断言最终 generation 与 `execution.timeouts_seconds.read` 快照。
- Web stored Key 纵向用例通过模拟 SSE 验证 API 写入→Worker 解密→Adapter 聚合→逐题/Run Token 持久化→报告脱敏；另一用例保留 JSON fallback，并断言 Run 的空闲读取超时和 stream payload 到达 Provider request。
- Response/API/report 纵向用例验证安全归一化的 provider request ID、returned model、system fingerprint、finish reason 和 HTTP attempt count；过长/控制字符/非标值 fail closed 为 `null`，raw usage object 不作为任意持久化字段暴露。
- Runner 诊断测试覆盖非空但无法解析且 `finish_reason="length"` 时的 `output_truncated`，并确认普通解析失败仍保持 `parse_error`；两者都不改变严格计零语义。

增加或修改路由时至少断言：成功状态码与 Schema、一项校验错误、404/409 等业务错误、分页/筛选（若适用），以及响应中不出现秘密值。API 行为改变必须同步更新 [API.md](API.md)。

### 6.2 Alembic 与遗留 SQLite

`backend/tests/test_migrations.py` 使用独立临时 SQLite 和 Alembic 子进程验证：

- 空库 upgrade/check/downgrade/upgrade 往返，以及 `20260827_0004` 最终 revision、可靠性/凭据/治理/ledger/audit/Provider metadata 字段、约束和索引。
- 有模型、Benchmark、题目、Run 与 Response 的 legacy schema 被一致性备份、严格识别并无损升级；题目按原插入顺序回填 0-based `position`。
- 与当前 metadata 一致但没有版本标记的库可安全收养，已有 head 重复 preflight 不生成多余备份。
- 部分表、server default/CHECK 内容或重名、PK/UNIQUE/FK/index/partial index、trigger、SQLite conflict policy/generated column、`STRICT`/`WITHOUT ROWID` 等未知 drift 在创建版本标记和备份前被拒绝；已在 head 的库同样验证。
- versioned legacy 的非法 Provider 配置数据会在任何 SQLite batch DDL 前失败，不残留临时重建表。
- `0001 -> 0002` 会按冻结的 Phase 1 语义收敛旧 `running` Run；存在 active Run 时可靠性 downgrade 被拒绝，不能静默删除租约元数据。
- `0002 -> 0003` 会把旧 OpenAI-compatible Model 回填为 `environment`、Mock 回填为 `none`；只要凭据表有任意行，credential downgrade 在 DDL 前拒绝并保留二进制内容。
- `0003 -> 0004` 把既有 Run 标为 `legacy_unmanaged` 并保留 protocol-v1 证据；任意 policy/scope/bucket/question execution/attempt ledger/audit、新 Run fairness/governance 字段或 Response Provider metadata 存在时，`0004 -> 0003` 必须在第一条 DDL 前拒绝。只有隔离空库用于 `0004 -> 0003 -> 0004` roundtrip。
- 应用启动 revision 门禁拒绝未迁移库；测试夹具中的 `create_all` 仅用于隔离临时库，并显式 stamp 到与 metadata 对应的 head，不是运行时建表路径。

目标化运行：

```bash
cd backend
uv run pytest tests/test_migrations.py
```

真实 PostgreSQL `backend-integration` job 在空的专用 management database 上执行 migration 往返与 `alembic check`，验证 revision/DDL；它不提供已使用数据库可安全丢弃新事实的证明。带数据证据来自 Compose 验收：脚本完成 managed Mock baseline 后停止 API/Worker，尝试 `0004 -> 0003` 并断言 guard 在任何 DDL 前拒绝，revision 和 core protocol hash 不变；另建隔离空 PostgreSQL 验证 `0004 -> 0003 -> 0004`。schema downgrade 不是 PostgreSQL→SQLite 平台回迁。

### 6.3 SQLite→PostgreSQL 导入

`backend/tests/test_sqlite_postgres_import.py` 的离线路径验证 canonical hash、只读 SQLite、head/integrity/FK/active-Run/active-reservation 拒绝、固定 12 表复制及提交前回滚；源 preflight 在打开目标前从完整 reservation ledger 重算每个 scope 和 minute bucket，拒绝高报、低报、缺 bucket 或其他派生漂移。fixture 含真实 AES-GCM nonce/ciphertext/key-id、governance/ledger/audit/Provider metadata 行并断言明文不在 SQLite。标记为 `integration` 的真实 PostgreSQL 用例还验证：

- 随机专用空库成功导入后，12 表行数、主键集合和 canonical row hash 与源一致，JSON、Decimal、UTC、协议快照、逐题证据、governance ledger/audit 及 credential 二进制保持；stdout/stderr 不打印 Key、key id、nonce 或 ciphertext。
- 中途复制失败整体回滚；两个不同源并发导入时恰好一个成功，另一个在目标非空检查处拒绝。
- `COMMIT` 确认丢失使用专用 `commit_outcome_unknown` 语义；已确认提交后的 snapshot 或输出失败使用 `committed_but_verification_failed`，两者都禁止盲目重试。
- 源 SQLite 主文件 hash 不变；输出只有阶段、表名、行数与 SHA-256 摘要，不打印题目、回答或连接 URL。
- 测试只在 loopback PostgreSQL 创建随机数据库，验证实际 `current_database()` 后才执行 truncate，并在 `finally` 精确 `DROP DATABASE ... WITH (FORCE)`。

完整运维步骤和 exit code 处理见 [DEPLOYMENT.md](DEPLOYMENT.md)。

### 6.4 健康、日志和指标的测试边界

- `/ready` 把同步数据库/head 检查放入 `asyncio.to_thread` 并设置异步等待上限，Redis ping 也有独立 timeout；测试证明半开依赖不会阻塞 `/live`。取消 `to_thread` 的等待不会终止底层数据库驱动调用，因此最终上界仍依赖 driver、连接池和 `connect_timeout`，不能把 asyncio timeout 当作强制中止。
- Worker `app.worker_probe` 是依赖能力探针：数据库/head 失败 exit 1；Redis 不可用但数据库 reconciliation 可用时输出 `degraded` 且 exit 0；配置错误 exit 1。它不观察 Worker 主事件循环或当前 heartbeat，不能证明进程没有卡死。
- LLMBenchLab 应用 logger 输出脱敏 JSON、request/correlation ID 与 allowlist 字段；异常只记录类型。Uvicorn 自身及 access logger 仍使用其原生 handler，不在“全部日志统一 JSON”的保证内。
- `/tasks/metrics` 是当下数据库 gauges；`/tasks/history` 和 `/runs/{id}/audit` 已提供 retained typed counters、有限样本延迟与稳定分页事件，包括 pending cancel 的 `run_cancel_requested` 和专用 `run_dead_lettered`。它们仍不是 Prometheus exporter、trace、自动告警、WORM/不可抵赖审计或 Worker 主循环 liveness；这些生产观测能力仍需后续工作。

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
4. CI 不配置任何真实 Provider Key；测试进程只生成独立临时 keyring 和明显虚构 canary。所有 OpenAI-compatible 路径必须注入 `MockTransport`，篡改/错误凭据路径在构造 Adapter 前失败。
5. 测试数据中的 Key 与域名必须是明显无效占位符，不从开发者环境复制。
6. 标准数据测试必须注入内存 fetcher；CI 不运行在线 `llmbenchlab-evaluate prepare`，也不依赖本机已有 `artifacts/` 缓存。
7. 报告和正式流程组件测试必须使用临时目录/临时数据库，不能读取或覆盖操作者已有正式 Run。

当前测试套件没有操作系统级的“禁止所有出站网络”沙箱，因此最后一层仍是代码 Review：任何新增测试若构造真实 `httpx` client、读取开发 Key 或依赖在线服务，都必须被拒绝。公开 CI 加固可在后续增加 egress-disabled runner 或网络拦截 fixture；不能因为 CI runner 通常没有 Key 就认定任意网络访问安全。

真实 OpenAI-compatible Provider 只允许作为用户主动执行、明确知晓费用和数据政策的可选手工验证；它不是 PR、CI、Smoke 或 Phase 2 可靠执行基础的完成条件。

真实模型验收应在隔离的本地数据库上先运行 `--limit`，人工核对模型发现、付费 canary、请求上界、逐题错误、Provider 账单和报告三文件，再决定是否 `--full`。这项手工操作不得写入自动化测试结果或 CI 通过数；如果本次没有真实 API URL/Key，就应明确记录“未运行”，不能用 MockTransport 结果替代“真实 Provider 已验证”。

当前自动化已验证 typed lifecycle/credential audit、history counters/latency、逐题 Provider request ID/returned model/system fingerprint/finish reason/HTTP attempt count 的持久化/API/报告路径，以及非法值归一化。已知观测缺口是 `resume` canary 尚未追加独立 audit event，另无 exporter/告警/WORM；不能把这些剩余边界写成 Provider exactly-once 或生产可观测性完成。

## 9. 前端测试要求

当前 Vitest 组件测试覆盖：

- 分数、百分比、Token 合计/未知值、费用、UTC 时间、Hash 与答案的格式化。
- `pending/running/completed/failed/cancelled` 五种状态标签。
- Dashboard 主页面的 API 加载、严格总分、完成率、Run/模型/Benchmark 汇总与最近运行。
- 后端不可达时的结构化、可重试错误状态。
- Models password input、创建必填、编辑留空保留、origin 变化重输、请求 pending/成功/失败/关闭/切 Mock/unmount 清空、AbortSignal、恶意错误回显脱敏，以及不写 storage/console。
- New Run 的 GPQA `8192/600s` 初始建议、MMLU-Pro official/direct 切换建议、手动预算不被覆盖、“应用建议”恢复、`max_tokens:null` Provider 托管提交，以及数字 `131072`/超时 `1800s` DOM 上限。
- Runs 主导航、20 条 offset 分页、状态筛选、active Run 定时刷新、错误/空状态和详情链接。
- Run Detail 返回 Runs 列表、终态停止轮询、逐题每页 100 条的 offset 导航，以及跨页序号/总数显示。
- Run Detail 显示 `managed`、`delayed`、`exhausted`、`legacy_unmanaged` 治理状态，稳定 closed reason 使用受控文案，`not_before` 明确按 UTC 格式化；未知 reason 只显示安全兜底，不反射服务端原值。

所有 fetch 与 Recharts 均在进程内 stub，没有真实网络；这些组件测试不会调用真实 Provider，也不会验证 Provider 实际接受某个输出长度或读取超时。Benchmark Demo 导入与完整 raw/parsed/reference/score/error 证据仍以离线后端 Smoke 和手工验收补充，不能把 DOM/API stub 结果描述成真实模型兼容性验证。

本轮还在可信 loopback 的真实浏览器中手工核对 Models 表单：Key 控件实际为 password input，页面没有 `api_key_env` 控件，保存成功后表单/卡片/网络响应均不回显测试 Key，应用日志也没有该测试 Key。该检查使用无效测试值且没有触发真实 Provider；它补充 DOM stub 自动化，但不增加 Vitest 或后端测试计数。

测试应优先按可见文本、label 和 role 查询 DOM，不依赖内部 class 或实现细节。时间、ID 和 API 返回应固定；不要用长 sleep 消除竞态。

## 10. CI

GitHub Actions 对 `main` push 和 Pull Request 触发四类 job：

| Job | 必需检查 | 隔离与失败规则 |
| --- | --- | --- |
| `backend` | Ruff lint/format；临时 SQLite `upgrade -> 0001 -> head`/check；`pytest -m "not integration"` | 临时 SQLite；不启动 PostgreSQL/Redis；离线 Mock/MockTransport |
| `backend-integration` | 真实 PostgreSQL migration 往返；PostgreSQL/Redis/importer 的 6 个 `integration` 用例 | Actions service 容器；JUnit 必须收集非零用例且零 skip，否则 job 失败 |
| `full-stack-reliability` | `python3 scripts/phase2_acceptance.py` 的隔离 Compose 八场景 | 唯一项目/卷、随机 loopback 端口、Mock-only；总是上传已脱敏 evidence，脚本总是精确清理 |
| `frontend` | ESLint、Vitest 组件测试、production build（`tsc -b` + Vite） | `npm ci` 锁定依赖；fetch/Recharts stub；`VITE_API_BASE_URL=/api/v1` |

CI 不配置 Provider Key、不调用真实模型，也不在线下载 MMLU-Pro/GPQA。PostgreSQL/Redis 是测试依赖，不是 Provider 网络；标准数据转换只使用 fixture fetcher。所有必需 job 通过后才能合并；跳过用例、降低断言或使用 `continue-on-error` 都不算修复。具体分支和 Review 门槛见 [GITHUB_WORKFLOW.md](GITHUB_WORKFLOW.md)。

下表是 2026-08-25 Phase 2 可靠性切片的历史基线，不是本次标准数据/真实评测 CLI 提交的通过声明。本次实际数量必须在当前工作日志和精确 commit 的 CI 中重新记录，不能沿用这些数字：

| 验证 | 2026-08-25 历史基线 |
| --- | --- |
| 后端本地非基础设施 | `205 passed`，命令 `uv run pytest -m "not integration"` |
| 真实 PostgreSQL/Redis infrastructure | `5 passed, 0 skipped` |
| 前端 | `4 files, 13 passed`；lint/typecheck/build 分别通过 |
| 离线 Smoke | `1 passed`（其余非 smoke 用例 deselected） |
| Compose 可靠性 | `8/8 passed`，最终 Redis consumer group `pending=0`、`lag=0`，清理后无项目容器/卷/网络 |

这些数字只证明 2026-08-25 当时的“可靠任务执行基础”垂直切片；它们早于后续 governance/audit/capacity 工作，不能用来判断当前实现，也不代表 Phase 2 全部完成。

2026-08-27 Web Provider 凭据切片的最终当前工作树本地证据如下；精确 SHA 的远程 CI 仍须在 commit/push 后独立验证：

| 验证 | 当前工作树结果 |
| --- | --- |
| 后端全量 | `make test`：`427 passed, 6 skipped`；6 个 skip 为未注入 DSN 的 infrastructure marker |
| 真实基础设施 | 临时 PostgreSQL 16/Redis 7：`6 passed, 0 skipped`，精确容器已清理 |
| 前端 | ESLint/typecheck、5 files / `21 passed`、Vite production build 均通过；保留既有 chunk warning |
| 离线 Smoke | `1 passed, 5 deselected` |
| Compose 可靠性 | `8/8 passed`；evidence `llmbenchlab-p2-60f3ccdac113/evidence.json`，清理后项目容器/卷/网络为空；每个 API 请求同时验证 client request-id 不被反射且响应为 UUIDv4 |
| 其他静态门禁 | Ruff/format、PostgreSQL Alembic upgrade/check、`uv lock --check`、Compose config、`git diff --check` 与高置信 secret scan 通过 |

keyring bootstrap 的 `24` 个定向测试覆盖所有相关本地入口强制 CPython，以及原子创建、既有文件校验、权限、symlink/路径置换、清理确认后瞬时重试、open 身份不确定与 unlink/close 清理失败停止、仅符号 errno 的错误输出。部署入口还在 `PATH` 将 macOS PyPy 放在首位时对全新临时路径执行创建/二次校验，确认 `uv` 仍选择 CPython；该手工探针不改变上表测试总数。

### 10.1 历史 evidence 与当前工作树回归

下表记录同一治理工作树演进中实际执行的本地验证。两个旧脚本 JSON 各自冻结其运行时 dirty snapshot；它们均早于当前增强 capacity harness，不是当前候选、精确提交 SHA 或远程 CI 的结论。脚本都移除了真实 Provider credential 环境变量，模型执行只用 Demo Mock；PostgreSQL/Redis 是被测基础设施，不是模型 Provider。

| 验证 | 实际结果 |
| --- | --- |
| 当前工作树全量（尚非候选 SHA/远程 CI） | `make test`：后端 `603 passed, 29 skipped`，前端 Vitest `38 passed` |
| 2026-08-28 历史定向回归 | `.venv/bin/pytest -q tests/test_governance.py tests/test_governance_api.py tests/test_audit_api.py tests/test_task_history_api.py tests/test_response_metadata_api.py tests/test_credential_audit.py tests/test_phase2_capacity_script.py tests/test_adapters.py`：`114 passed`；仅既有 Starlette/pytest-asyncio deprecation warnings |
| Compose acceptance | `8/8 passed`；`.pytest_cache/artifacts/phase2-acceptance/llmbenchlab-p2-5b2d8219bf59/evidence.json`，SHA-256 `3f7299d4f7e9b8fcb09d779ff92609535cdee71a33c3869c193aa87f35158a19`；场景 8 证明 populated 0004 downgrade 在 DDL 前拒绝且 Run/hash/revision 不漂移，并用独立空库完成 `0004→0003→0004`；最终 PEL/lag 为 0，cleanup 容器/卷/网络为空 |
| Capacity 环境与输入 | `make phase2-capacity` evidence schema v1；Darwin 25.5 arm64、8 CPU/8 GiB host，Docker Desktop 29.7.2、8 CPU/约 4.1 GB，PostgreSQL 16.14、Redis 7.4.10；`llmbenchlab-protocol-v1` Demo 15 题，dataset SHA-256 `5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe`，4 Run/阶段、concurrency 1、Mock delay 80 ms、1→2 Worker；记录 HEAD `1cd19c51ed309316047a18ed3b2a308647af495d` 且 `dirty=true` |
| 1 Worker reference | 4 Run/60 题，7.422401 s，0.538909 Run/s、8.083637 题/s；end-to-end p50/p95/p99 = 4.578112/6.953858/7.167727 s，queue p95 5.179710 s，execution p95 1.912167 s；0 question error、0 failed attempt |
| 2 Worker baseline | 4 Run/60 题，4.085257 s，0.979131 Run/s、14.686958 题/s；end-to-end p50/p95/p99 = 2.951079/3.899837/3.907275 s，queue p95 2.015659 s，execution p95 2.010238 s；0 question error、0 failed attempt |
| 增强前的历史 backlog/fault/reconciliation | bounded burst 4 Run/60 题：6.423057 s、9.341347 题/s，峰值 pending 4；lease-owner SIGKILL/expiry、Redis stop/start DB reconciliation、terminal duplicate delivery 均收敛。最终 14 completed Run/210 Response/211 ledger（210 actual、1 conservative）、900 audit event、无重复 key/operation、无 active/reserved/overdrawn 漂移，PEL/lag 0；该运行没有触达拒绝阈值，也不证明当前精确 `202/429`、yield 或公平性场景 |
| Capacity artifact/cleanup | `.pytest_cache/artifacts/phase2-capacity/llmbenchlab-p2-bb49c6069785/evidence.json`，SHA-256 `f78886422eeb1d6b54c3fe1da401fd411042a6b4421aeae3f4f5e7ef43444340`；cleanup 容器/卷/网络为空 |

这些吞吐和百分位只描述旧 evidence 中记录的机器、dirty worktree、Mock 配置和小样本，不是当前候选、Provider 兼容/费用结果、生产 SLO/SLA、无限扩展或精确 commit 结论。增强 harness 的完整真实 Compose evidence 尚待运行；不得从旧 artifact 推导新结果。完整字段解释与复现实验见 [PERFORMANCE.md](PERFORMANCE.md)，backlog/dead-letter、budget、settlement unknown、Worker 扩缩和 DB/Redis/lease 恢复见 [OPERATIONS.md](OPERATIONS.md)。

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
   curl -sS 'http://127.0.0.1:8000/api/v1/tasks/history?window_hours=24'
   curl -sS http://127.0.0.1:8000/api/v1/info
   ```

   默认本地 `REDIS_URL` 为空时 queue 显示 `disabled`，上述请求均为 `200`，且不出现 Provider 请求。若显式配置的 Redis 不可用，`ready` 为脱敏 `503 degraded`；数据库正常时仍显示 `accepting_runs=true`。

   在首次 policy apply/Run admission 前再执行 `curl -i http://127.0.0.1:8000/api/v1/governance/policy`，应得到无副作用的 `404 governance_policy_not_initialized`；查询不能偷偷 bootstrap。若要手工激活 policy，`PUT /api/v1/governance/policy` 必须按 [API.md](API.md) 一次提交全部字段，不是局部 PATCH；相同内容幂等，重新提交历史内容会重激活原 ID/version。

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
   curl -sS 'http://127.0.0.1:8000/api/v1/runs/<RUN_ID>/audit?limit=100'
   curl -sS 'http://127.0.0.1:8000/api/v1/tasks/history?window_hours=24'
   curl -sS 'http://127.0.0.1:8000/api/v1/leaderboard?benchmark_id=<BENCHMARK_ID>&order=score_desc'
   curl -sS http://127.0.0.1:8000/api/v1/metrics/summary
   ```

   预期 Responses 共 15 条，Run audit 能稳定分页看到 admission/claim/question/terminal typed event，history counters/latency 包含本次 Run；排行榜包含该 Run 且 `is_demo=true`，汇总包含 1 个已完成 Run。

### 11.2 前端验收

打开 `http://127.0.0.1:5173`，按顺序检查：

- Dashboard 的模型、Benchmark、Run、得分/延迟/Token 汇总与最近运行来自 API，而非固定假数据。
- Models 能新增、编辑、删除 Mock；选择 OpenAI-compatible 后出现 masked API Key 输入框，用户直接粘贴真实 Key，保存后输入框清空且卡片只显示“已安全保存”。编辑留空保留，改变 Provider origin 必须重输。
- Benchmarks 能重载 Demo、显示版本/题数/Hash/许可证与醒目的 Demo 警告。
- New Run 能选择 Model 与 Benchmark，区分 protocol-v1 API 默认和 Web 建议，允许数字预算或显式 Provider 托管，并把读取超时随创建请求提交。
- Runs 列表能从主导航进入，按状态/20 条分页显示并链接回详情；Run Detail 轮询进度并在终态停止，配置快照和逐题证据以每页 100 条查看，不把大型 Run 截断在第一页。
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
8. 停止 API/Worker 后在 populated PostgreSQL 上尝试 `20260827_0004 -> 0003`：policy/ledger/audit 等数据存在时必须在第一条 DDL 前拒绝，application revision、Run/Response core protocol hash 与可靠性字段不变；另建独立空 PostgreSQL 完成 `0004 -> 0003 -> 0004` 和 check，随后重启 API/Worker。schema downgrade 不是数据平台回迁。

任何一个场景失败、未运行、使用真实 Provider、最终 PEL/lag 非零或清理不完整，都不能把可靠执行基础写成通过。`--self-check-only` 只验证 Docker/Compose、隔离和清理 guard，不执行八场景，不能替代正式命令。

### 12.1 Phase 2 Mock 容量基线

从仓库根目录运行：

```bash
make phase2-capacity
```

脚本复用隔离 Compose guard，启动真实 PostgreSQL 16/Redis 7/API 与至少两个独立 Worker，但所有 Model 都是 deterministic Demo Mock，且启动前移除真实 Provider credential 环境变量。它先通过 API apply 并 read-back 全部 20 字段均非 `null` 的有限 policy；每个 Run 显式携带 input Token reservation 和 lifetime request/Token/USD budgets。默认先把 Worker 缩到 1 记录 reference，再扩到 2；每阶段提交 4 个 15 题 Run、Run concurrency 1、`question_quantum=5`、Mock generation delay 80 ms，并记录 wall time、Run/题吞吐、queue/execution/end-to-end p50/p95/p99、错误/重试、PostgreSQL counter delta、task gauges 与 Redis stream/PEL/lag。

除 steady-state 外，默认先停止 Worker，以 6 路并发提交 6 个 Run，必须精确得到 4 个 `202` 和 2 个带 `run_backlog_full`/`limit=4` 的 `429`，再排空已接纳 Run；每个测量 Run 必须有至少两次 dispatch 和 cooperative yield。跨 Model 公平性场景在单 Worker 下先压入高流量 Model 的 3 个 Run，再用最后一个 slot 接纳低流量 Run，并以 durable audit 顺序断言低流量 Run 在高流量 backlog 全部终态前获得 claim/slice。脚本还必测 lease owner SIGKILL/自然过期接管、Redis stop/start 下 DB reconciliation，以及终态重复投递 no-op；最后从 DB ledger 重算 active/reserved/consumed，检查 audit/operation key 唯一、Response/QuestionExecution 对齐、overdrawn/漂移、queue lag/PEL 和项目 cleanup。证据写入 `.pytest_cache/artifacts/phase2-capacity/<project>/evidence.json`，schema 为 `llmbenchlab-phase2-capacity-evidence-v1`，包含被测 commit/dirty 状态、脚本/Compose hash、主机/容器资源、数据 Hash、配置、测量、故障、公平性、reconciliation、secret self-review 与 cleanup。

失败、字段缺失、非 Mock Model、真实 Provider credential、非有限 policy/read-back 漂移、错误的 `202/429` 分布、缺少 cooperative yield/公平顺序、少于两个 Worker、DB/queue/ledger/audit 漂移或残留资源都会使脚本失败。`--self-check-only` 只验证工具、guard、配置和清理，不能替代基线。增强合同已有代码和离线测试，但当前候选的新一轮完整真实 Compose evidence 尚未产生；历史 dirty artifact 不能替代它。结果只对证据中的硬件、dirty worktree/commit 与配置成立；小样本 Mock 吞吐不是 Provider 性能、费用、生产 SLO/SLA 或水平扩展上限。

## 13. 失败排查与完成证据

- 先单独复现最小失败测试，再运行完整层级；保留第一个有因果信息的 traceback。
- SQLite lock、PostgreSQL/Redis 测试 DSN、端口占用或残留环境变量是测试隔离问题，不能通过重试掩盖。
- 快照变化需人工确认是有意 API/协议变化；不得无条件更新 expected output。
- 前端测试通过但 build 失败时任务仍未完成；lint、typecheck、test、build 是不同门槛。
- Docker 不可用时记录 `docker compose config`/启动未运行及原因；不能因此把 Compose 场景写成通过。
- 最终证据应列出工作目录、命令、测试数、耗时和失败数；CI 链接只能补充，不能替代本地实际结果说明。
- evidence 与日志必须先做敏感值检查；不得记录 DSN 密码、Redis URL、Authorization、题目、原始回答或 Provider 正文。Hash 和行数仍属于运维元数据，应按评测数据保护。
