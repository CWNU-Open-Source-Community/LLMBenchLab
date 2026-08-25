# LLMBenchLab 测试指南

## 1. 测试原则

LLMBenchLab 的默认验收路径必须完全离线、可重复且不产生模型费用。自动化测试和 CI 只能使用 `MockModelAdapter`，或使用进程内 `httpx.MockTransport` 验证 OpenAI-compatible 协议；不得访问真实 Provider，不要求 API Key，不把“本机碰巧可用”当作通过证据。

每次报告测试结果时必须写明实际命令、通过/失败、测试数量、失败原因和未运行项。没有执行的 Docker、浏览器或联网验证不能写成通过。

## 2. 测试分层

| 层级 | 目的 | 当前入口/文件 | 外部网络 |
| --- | --- | --- | --- |
| 后端单元测试 | Evaluator、Adapter、Loader/Hash 的边界语义 | `backend/tests/test_evaluators.py`、`test_adapters.py`、`test_dataset_loader.py` | 禁止；Provider 用 MockTransport |
| API 集成测试 | FastAPI Schema、状态码、秘密安全、SQLite CRUD | `backend/tests/test_api.py` | 禁止 |
| 迁移回归测试 | 空库往返、旧库无损收养、备份、拒绝未知漂移、启动 revision 门禁 | `backend/tests/test_migrations.py` | 禁止 |
| Mock 端到端 Smoke | 注册 Mock → Demo → Run → Responses → Leaderboard/Metrics | `backend/tests/test_smoke.py`，marker 为 `smoke` | 禁止 |
| 前端单元/组件测试 | 格式化、状态/指标、错误/空态、主要页面交互 | `frontend/src/**/*.test.ts(x)`、`frontend/tests/` | API 必须 stub/mock |
| 静态检查 | Python lint/format、ESLint、TypeScript | Ruff、ESLint、`tsc` | 不需要 |
| 构建检查 | 确认生产前端可编译打包 | `npm run build` | 安装完成后不需要 |
| 配置检查 | 校验 Compose 插值和服务定义 | `docker compose config` | 不启动 Provider |

单元测试定位纯逻辑错误；API 集成测试覆盖 HTTP/数据库边界；Smoke 证明最小垂直链路。三者不能互相替代。

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
make format     # 运行项目约定的格式化器
```

格式化会修改文件；仅检查时使用下面的直接命令。提交 PR 前还应执行前端 production build。

### 4.1 后端

```bash
cd backend
uv run pytest
uv run pytest -m smoke
uv run ruff check .
uv run ruff format --check .
```

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
docker compose config
```

这只验证配置渲染，不证明镜像已构建或服务健康。若要验证容器启动，应另行执行
`make docker-up`、检查 `/api/v1/health`，最后执行 `make docker-down`，并如实记录 Docker 是否可用。

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

## 6. API 与迁移集成测试

### 6.1 API

`backend/tests/conftest.py` 在导入应用前设置一个临时目录中的 SQLite URL。每个 API client fixture 会重建表，因此测试不会读写开发数据库 `backend/data/llmbenchlab.db`。

当前 API 集成路径验证：

- `/health`、`/info` 不访问 Provider。
- Model CRUD、分页、Provider 必需字段和名称冲突。
- API 只返回 `api_key_env` 名称，不返回环境变量值。
- SQLAlchemy 基本 CRUD 与外键/Schema 基线。
- Run 创建状态码、轮询、逐题证据、汇总和排行榜。

增加或修改路由时至少断言：成功状态码与 Schema、一项校验错误、404/409 等业务错误、分页/筛选（若适用），以及响应中不出现秘密值。API 行为改变必须同步更新 [API.md](API.md)。

### 6.2 Alembic 与遗留 SQLite

`backend/tests/test_migrations.py` 使用独立临时 SQLite 和 Alembic 子进程验证：

- 空库 upgrade/check/downgrade/upgrade 往返，以及最终 revision/约束。
- 有模型、Benchmark、题目、Run 与 Response 的 legacy schema 被一致性备份、严格识别并无损升级；题目按原插入顺序回填 0-based `position`。
- 与当前 metadata 一致但没有版本标记的库可安全收养，已有 head 重复 preflight 不生成多余备份。
- 部分表、server default/CHECK 内容或重名、PK/UNIQUE/FK/index/partial index、trigger、SQLite conflict policy/generated column、`STRICT`/`WITHOUT ROWID` 等未知 drift 在创建版本标记和备份前被拒绝；已在 head 的库同样验证。
- versioned legacy 的非法 Provider 配置数据会在任何 SQLite batch DDL 前失败，不残留临时重建表。
- 应用启动 revision 门禁拒绝未迁移库；测试夹具中的 `create_all` 仅用于隔离临时库，并显式 stamp 到与 metadata 对应的 head，不是运行时建表路径。

目标化运行：

```bash
cd backend
uv run pytest tests/test_migrations.py
```

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
4. 创建 Run 并取得 `202` 与 Run ID。
5. 在有限期限内轮询到终态，不使用无限等待。
6. 断言 15 道题均有 Response，三种题型都出现。
7. 断言严格总分、完成率和 answered accuracy 都在合法范围；当前确定性 Demo 预期为 100。
8. 断言排行榜出现该 Run 且保留 Demo 标记，Dashboard 汇总增加 completed Run。

另一个故障隔离测试给单题注入 Mock Adapter 错误，断言 Run 仍完成、失败题计 0、错误被持久化、其余题继续执行，严格总分为 `14/15*100`。

Smoke Test 只证明单进程 MVP 链路，不证明真实 Provider 兼容性、任务重启恢复、Docker 网络或生产可靠性。

## 8. 不调用真实 API 的机制

自动化安全依赖多层约束：

1. 所有端到端测试注册的 Provider 都是 `mock`；`MockModelAdapter.generate` 不执行网络 I/O。
2. OpenAI-compatible 协议测试向 Adapter 注入 `httpx.MockTransport`，响应在进程内生成。
3. 测试数据库和日志级别在应用导入前通过 fixture 环境变量设置，不读取开发 `.env`。
4. CI 不配置任何 Provider Key；即使误走 OpenAI-compatible 路径，缺少 `api_key_env` 对应值也会在 HTTP 请求前失败。
5. 测试数据中的 Key 与域名必须是明显无效占位符，不从开发者环境复制。

当前测试套件没有操作系统级的“禁止所有出站网络”沙箱，因此最后一层仍是代码 Review：任何新增测试若构造真实 `httpx` client、读取开发 Key 或依赖在线服务，都必须被拒绝。公开 CI 加固可在后续增加 egress-disabled runner 或网络拦截 fixture；不能因为 CI runner 通常没有 Key 就认定任意网络访问安全。

真实 OpenAI-compatible Provider 只允许作为用户主动执行、明确知晓费用和数据政策的可选手工验证；它不是 PR、CI、Smoke 或 Phase 1 完成条件。

## 9. 前端测试要求

Phase 1 当前有 4 个 Vitest 文件、13 个用例，实际自动化覆盖：

- 分数、百分比、Token 合计/未知值、费用、UTC 时间、Hash 与答案的格式化。
- `pending/running/completed/failed/cancelled` 五种状态标签。
- Dashboard 主页面的 API 加载、严格总分、完成率、Run/模型/Benchmark 汇总与最近运行。
- 后端不可达时的结构化、可重试错误状态。

所有 fetch 与 Recharts 均在进程内 stub，没有真实网络。这满足 MVP 的最低前端测试门槛，但下列交互目前仍由 production build、后端 Smoke 和手工验收覆盖，应在后续迭代补为组件测试：Models CRUD 表单、Benchmark Demo 标识、新建 Run 跳转、Run Detail 的 raw/parsed/reference/score/error 与终态停止轮询，以及 Leaderboard 分区筛选。不得把这份待补清单描述为已有自动化覆盖。

测试应优先按可见文本、label 和 role 查询 DOM，不依赖内部 class 或实现细节。时间、ID 和 API 返回应固定；不要用长 sleep 消除竞态。

## 10. CI

GitHub Actions 对 `main` push 和 Pull Request 触发，至少执行：

- 后端 Ruff lint 与 format check。
- 后端完整 pytest（其中包含离线 Smoke）。
- 前端 ESLint、TypeScript typecheck、Vitest。
- 前端 production build。

CI 使用临时 SQLite，不启动 PostgreSQL/Redis，不配置 API Key，不调用真实模型。所有必需检查通过后才能合并；跳过测试、降低断言或把失败改成 `continue-on-error` 不算修复。具体分支和 Review 门槛见 [GITHUB_WORKFLOW.md](GITHUB_WORKFLOW.md)。

## 11. Mock 手工验收

这套验收不需要网络模型。先在两个终端分别运行：

```bash
make backend
```

```bash
make frontend
```

### 11.1 API 验收

1. 访问健康与能力信息：

   ```bash
   curl -sS http://127.0.0.1:8000/api/v1/health
   curl -sS http://127.0.0.1:8000/api/v1/info
   ```

   预期均为 `200`，健康检查不出现 Provider 请求。

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

可额外创建一个 Run 并立即调用 `POST /runs/{id}/cancel`，确认最终为 `cancelled`，重复取消不会报错。Mock 很快，可能在取消前已经完成，这也是合法竞态。

不要用 kill 进程来宣称恢复测试通过。MVP 的定义行为是重启后把遗留 `running` 标为 `failed`，不会续跑；可靠恢复属于 Phase 2。

## 12. 失败排查与完成证据

- 先单独复现最小失败测试，再运行完整层级；保留第一个有因果信息的 traceback。
- SQLite lock、端口占用或残留环境变量是测试隔离问题，不能通过重试掩盖。
- 快照变化需人工确认是有意 API/协议变化；不得无条件更新 expected output。
- 前端测试通过但 build 失败时任务仍未完成；lint、typecheck、test、build 是不同门槛。
- Docker 不可用时记录 `docker compose config`/启动未运行及原因，不影响伪造结论。
- 最终证据应列出工作目录、命令、测试数、耗时和失败数；CI 链接只能补充，不能替代本地实际结果说明。
