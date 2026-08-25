# LLMBenchLab

LLMBenchLab 是一个面向个人开发者与研究人员的轻量级 LLM 评测工作台。它把模型注册、版本化 Benchmark、后台评测、逐题证据、汇总指标和排行榜放进一条可审计的本地流程，并以“默认离线、严格评分、结果可复现”为首要约束。

当前版本聚焦单机 MVP：FastAPI 模块化后端、React 单页应用、SQLite 持久化，以及完全不需要 API Key 的 Mock Demo。项目同时提供 OpenAI-compatible Chat Completions 适配器，但真实 Provider 调用始终是用户主动启用的可选能力。

## 当前状态

- 版本：`0.1.0`（development baseline，尚未发布正式 Release）
- 评测协议：`llmbenchlab-protocol-v1`
- Phase 0（治理、需求、架构和协议）已完成。
- Phase 1 MVP 已具备完整垂直链路：注册模型、载入/导入 Benchmark、创建 Run、后台执行、逐题持久化、结果聚合和前端展示。
- 默认验收路径只使用 Mock adapter 和临时 SQLite，不访问真实模型服务，也不产生模型费用。
- Phase 2–6 的可靠 Worker、标准 Benchmark、Judge/Arena、Agent/Live Benchmark 和公共发布能力尚未实现。

最新、可复核的完成状态与测试证据以 [`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md) 和 [`docs/worklogs/`](docs/worklogs/) 为准；Roadmap 中的计划能力不等于已交付能力。

## 核心特性

- **完全离线的 Mock Demo**：15 道原创双语演示题，覆盖 exact match、multiple choice 和 numeric；结果必须明确标记为 Demo，不能当作正式模型能力结论。
- **模型注册表**：支持 `mock` 与 `openai_compatible`，记录远端模型名、默认生成参数和可选价格信息。
- **版本化 Benchmark**：严格校验 `manifest.json` 与 `questions.jsonl`，支持受限 ZIP 导入、稳定 SHA-256 和导入冲突检测。
- **确定性评分**：内置三类 Evaluator；解析失败和单题调用失败严格计 0 分，并保留错误证据。
- **可解释指标**：严格总分 `score`、完成率 `completion_rate` 和已回答准确率 `answered_accuracy` 分开呈现，避免把缺失回答隐藏在成功样本中。
- **受控后台执行**：Run 在进程内异步执行，支持有限并发、协作式取消、单题故障隔离和进度轮询。
- **可复现记录**：持久化模型参数、Prompt、Benchmark Hash、协议版本、代码 commit（可用时）、raw response、parsed answer、参考答案快照和逐题评分。
- **六个前端页面**：Dashboard、Models、Benchmarks、New Run、Run Detail 和 Leaderboard，含加载、空数据、错误状态与响应式布局。
- **秘密最小化**：数据库和 API 只保存/返回 `api_key_env` 的变量名，绝不接收或持久化对应的 Key 值。
- **开发交付完整**：Alembic、Ruff、pytest、ESLint、TypeScript、Vitest、Vite production build、GitHub Actions、Makefile 和 Docker Compose。

## 产品截图

> **真实截图尚未提供。** 本节仅保留截图位置说明，仓库没有使用设计稿、假数据图片或生成图片冒充已运行界面。启动本地服务后可在 `http://127.0.0.1:5173` 查看真实 UI；后续发布经实际运行验证的截图时，应同时注明 commit、数据集版本和是否为 Demo。

## 技术栈

| 层次 | 技术 |
| --- | --- |
| 后端 | Python 3.11+、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、Uvicorn、httpx |
| 数据 | SQLite；版本化 JSON/JSONL Benchmark；SHA-256 数据集指纹 |
| 前端 | React 19、TypeScript、Vite 7、React Router、Recharts、Lucide |
| 测试与质量 | pytest、httpx MockTransport、Vitest、Testing Library、Ruff、ESLint、TypeScript |
| 交付 | Make、Docker Compose、Nginx、GitHub Actions |

## 架构

```mermaid
flowchart LR
    User[本地用户] --> Web[React + Vite Web]
    Web -->|REST /api/v1| API[FastAPI]

    subgraph Backend[模块化单体]
        API --> Services[应用服务]
        Services --> Loader[Dataset Loader]
        Services --> Runner[Evaluation Runner]
        Runner --> Adapters[Adapter Registry]
        Runner --> Evaluators[Evaluator Registry]
        Services --> ORM[SQLAlchemy]
        Runner --> ORM
    end

    Loader --> Files[manifest.json + questions.jsonl]
    ORM --> DB[(SQLite)]
    Adapters --> Mock[Mock Adapter / 无网络]
    Adapters -->|仅用户主动配置| Provider[OpenAI-compatible API]
    Env[后端进程环境变量] -->|运行时读取 Key| Adapters
```

API 创建 Run 后立即返回 `202`；进程内 Runner 领取任务、逐题调用 Adapter 和 Evaluator，并在每道题后写入证据与进度。前端轮询 Run，进入 `completed`、`failed` 或 `cancelled` 终态后停止。详细设计见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)，评分语义见 [`docs/BENCHMARK_PROTOCOL.md`](docs/BENCHMARK_PROTOCOL.md)。

## 仓库结构

```text
LLMBenchLab/
├── backend/
│   ├── alembic/             # 数据库迁移
│   ├── app/
│   │   ├── adapters/        # Mock / OpenAI-compatible
│   │   ├── api/v1/          # REST 路由
│   │   ├── core/            # 配置、常量、时间
│   │   ├── db/              # Session 与初始化
│   │   ├── evaluators/      # 三类确定性评分器
│   │   ├── models/          # SQLAlchemy 实体
│   │   ├── runners/         # 进程内评测 Runner
│   │   ├── schemas/         # Pydantic API Schema
│   │   └── services/        # Dataset 与业务服务
│   └── tests/               # 单元、集成与 Smoke 测试
├── frontend/
│   ├── src/api/             # 集中式 API Client 与类型
│   ├── src/components/      # 通用 UI 组件
│   ├── src/pages/           # 六个产品页面
│   └── tests/               # Vitest / Testing Library
├── benchmarks/demo-general/ # 15 道原创 Demo 题
├── docs/
│   ├── decisions/           # ADR
│   ├── phases/              # Phase 0–6 计划与验收
│   ├── templates/           # 计划、工作日志、ADR、Phase 模板
│   └── worklogs/            # 实际执行记录
├── scripts/                 # setup、dev、offline smoke
├── compose.yaml
├── Makefile
└── .env.example
```

## Quickstart

前置要求：Python 3.11 或更新版本、[`uv`](https://docs.astral.sh/uv/)、Node.js 22 或兼容版本，以及 npm。Docker 仅在 Compose 模式需要。

```bash
git clone https://github.com/OWNER/LLMBenchLab.git
cd LLMBenchLab
make setup
make dev
```

`make setup` 会按锁文件安装前后端依赖、仅在 `.env` 不存在时从 `.env.example` 创建它，并执行 Alembic migration；已有 `.env` 不会被覆盖。该命令可重复执行。若检测到由早期开发版自动建表留下的未版本化 SQLite，只有在结构与完整性严格匹配已知版本时才会先创建同目录 `.bak` 一致性备份并无损收养；未知或部分结构会在写入版本标记前停止。普通 backend 启动不会隐式建表，未迁移时会提示先运行 `make setup` 或 `make migrate`。`make dev` 在一个终端启动前后端，`Ctrl-C` 会一起停止。

默认地址：

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:8000`
- OpenAPI：`http://127.0.0.1:8000/docs`

健康检查：

```bash
curl -sS http://127.0.0.1:8000/api/v1/health
```

如需分别观察日志，可在两个终端运行 `make backend` 和 `make frontend`。所有可用命令可通过 `make help` 查看；更完整的环境变量、迁移和排障说明见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

## Mock Demo：完整离线流程

这条路径不需要任何 API Key，不会访问网络模型：

1. 执行 `make setup` 和 `make dev`，打开 `http://127.0.0.1:5173`。
2. 进入 **模型** 页面，新建模型；名称可填 `Offline Mock`，Provider 选择 `mock`，保持启用。Mock 不需要 Base URL、远端模型名或 `api_key_env`。
3. 进入 **评测集** 页面，点击重载/载入内置 Demo。确认它显示 `demo-general`、版本 `1.0.0`、15 道题，以及“Demo 数据，不代表正式模型能力”的提示。
4. 点击 **新建评测**，选择刚注册的 Mock 和 Demo Benchmark。默认参数可直接使用；推荐可复现基线为 `temperature=0`、`top_p=1`、`max_tokens=256`、`seed=42`、`concurrency=1`。
5. 提交后进入 Run Detail。页面会轮询 `pending/running` 状态，展示进度、配置快照和逐题结果，并在终态停止轮询。
6. 确定性 Mock Demo 正常应完成 15/15，严格总分、完成率和已回答准确率均为 100；逐题区域会分别显示 raw response、parsed answer、reference、score 和 error。
7. 进入 **排行榜**，按模型或 Benchmark 筛选，核对协议版本、数据集 Hash、完成率和醒目的 Demo 标识。该成绩只证明本地垂直链路可工作。

同一流程也可通过 API 完成：

```bash
# 1. 注册离线 Mock；保存响应中的 id 为 MODEL_ID
curl -sS -X POST http://127.0.0.1:8000/api/v1/models \
  -H 'Content-Type: application/json' \
  -d '{"name":"Offline Mock","provider_type":"mock","enabled":true}'

# 2. 幂等载入 Demo；保存响应中的 id 为 BENCHMARK_ID
curl -sS -X POST http://127.0.0.1:8000/api/v1/benchmarks/reload-demo

# 3. 替换两个占位 ID，创建 Run；保存响应中的 id 为 RUN_ID
curl -sS -X POST http://127.0.0.1:8000/api/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"model_id":"<MODEL_ID>","benchmark_id":"<BENCHMARK_ID>","temperature":0,"top_p":1,"max_tokens":256,"seed":42,"concurrency":1}'

# 4. 有限次数轮询至终态，再检查逐题证据与排行榜
curl -sS 'http://127.0.0.1:8000/api/v1/runs/<RUN_ID>'
curl -sS 'http://127.0.0.1:8000/api/v1/runs/<RUN_ID>/responses?limit=100'
curl -sS 'http://127.0.0.1:8000/api/v1/leaderboard?benchmark_id=<BENCHMARK_ID>&order=score_desc'
```

可直接运行自动化垂直切片：

```bash
make smoke
```

Smoke 使用临时 SQLite 和 Mock adapter，不接触开发数据库或真实 Provider。

## 接入 OpenAI-compatible Provider

LLMBenchLab 使用 Chat Completions 风格接口。若 `base_url` 为 `https://provider.example/v1`，Adapter 会请求 `https://provider.example/v1/chat/completions`；如果填写的 URL 已以 `/chat/completions` 结尾，则不会重复追加。

1. 选择一个后端环境变量名，例如 `LOCAL_COMPAT_API_KEY`，通过操作系统 Keychain、秘密管理器或受控 shell 把真实 Key 注入**启动 backend 的同一进程环境**。
2. 在 Models 页面或 `POST /api/v1/models` 中把 `api_key_env` 填为字符串 `LOCAL_COMPAT_API_KEY`。这里只填变量名，不能填真实值。
3. 同时填写可信的 `base_url` 和 Provider 的 `remote_model_name`，然后创建 Run。注册模型本身不会调用 Provider；Run 执行时 Adapter 才读取环境变量并发起请求。

API 示例中的域名是故意无效的占位符：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/models \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"My Compatible Model",
    "provider_type":"openai_compatible",
    "base_url":"https://provider.example.invalid/v1",
    "remote_model_name":"replace-with-provider-model-name",
    "api_key_env":"LOCAL_COMPAT_API_KEY",
    "enabled":true,
    "default_parameters":{"temperature":0}
  }'
```

真实 Key 不应进入 API JSON、数据库、Git、Issue、日志、截图或 `VITE_*` 变量。Model Schema 会拒绝 `base_url` query，并将 `default_parameters` 限定为 `temperature`、`top_p`、`max_tokens`、`seed` 四个严格校验的生成字段；浏览器不会直接调用 Provider。当前 MVP 尚无 SSRF allowlist，只可使用已审查的 Provider 地址，并在执行前确认题目外发许可、数据政策和费用。缺少目标环境变量时，相关单题会安全记录为 `missing_api_key`。

## 测试与质量检查

从仓库根目录运行：

```bash
make lint       # Ruff lint/format check + ESLint + TypeScript
make test       # 完整 pytest + Vitest
make smoke      # 纯离线 Mock 垂直链路
```

前端 production build 是独立门槛：

```bash
cd frontend
npm run build
```

测试策略的关键约束：自动化和 CI 不配置 Provider Key，不调用真实或付费 API；OpenAI-compatible 协议测试使用进程内 `httpx.MockTransport`；API/Smoke 使用隔离的临时 SQLite；前端 API 使用 stub/mock。完整测试矩阵与手工验收见 [`docs/TESTING.md`](docs/TESTING.md)。

## Docker Compose

Docker 模式会构建 FastAPI backend 和由 Nginx 提供的前端，并把 SQLite 保存到 named volume `sqlite-data`：

```bash
make docker-up
```

默认地址：

- Web（Nginx 同源代理 `/api/`）：`http://localhost:8080`
- API 健康检查：`http://localhost:8000/api/v1/health`

停止服务并保留数据：

```bash
make docker-down
```

配置静态校验：

```bash
docker compose config
```

`docker compose down -v` 会删除 SQLite volume，属于破坏性操作；除非明确要丢弃数据且已有备份，否则不要执行。Compose 的 host ports 也可能监听所有宿主机接口，因此它仍是本地开发配置，不是公网部署方案。备份、恢复、Provider secret override 和完整容器说明见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

## Roadmap

| 阶段 | 主题 | 状态摘要 |
| --- | --- | --- |
| Phase 0 | 项目治理、需求、架构、协议 | 已完成 |
| Phase 1 | FastAPI + React + SQLite 的 MVP 垂直链路 | 已完成 |
| Phase 2 | PostgreSQL、Redis、独立 Worker、恢复与可观测性 | 计划中 |
| Phase 3 | 合规标准 Benchmark 与隔离代码评测 | 计划中 |
| Phase 4 | LLM Judge、人工校准与 Arena | 计划中 |
| Phase 5 | Agent、工具调用与 Live Benchmark | 计划中 |
| Phase 6 | 公共发布、多用户、安全与运营加固 | 计划中 |

每个阶段的目标、非目标、依赖、验收标准和风险见 [`docs/ROADMAP.md`](docs/ROADMAP.md) 与 [`docs/phases/`](docs/phases/)。

## 安全

- MVP 没有认证、授权、限流、TLS、多租户隔离或生产级秘密管理，**不得直接暴露到公网**。
- 本地 Make 模式默认只监听 `127.0.0.1`；CORS 是浏览器策略，不是访问控制。
- API 与数据库只保存 `api_key_env` 名称；真实 Key 只由 backend 在请求时从环境读取。
- `VITE_*` 会进入浏览器构建产物，永远不能用于存放秘密。
- Benchmark ZIP 会做路径、文件类型、大小、压缩比、Schema 和题数校验，但导入者仍需审查来源、许可证、敏感数据和提示注入风险。
- 任意 OpenAI-compatible `base_url` 存在 SSRF 与数据外发风险；公开部署前必须加入地址策略、出站隔离、鉴权和费用控制。
- SQLite 包含题目、参考答案、原始回答和错误证据；数据库与备份需使用本机权限和加密存储保护。

威胁模型、秘密轮换和公开部署前门槛见 [`docs/SECURITY.md`](docs/SECURITY.md)。

## 当前限制

- SQLite 与进程内后台任务只适合个人、本地和低并发；不支持多副本协调。
- 进程退出后不会续跑：启动时发现遗留 `running` Run 会标记为 `failed`，已保存逐题证据仍保留。
- 取消是协作式的；已经发出的上游请求可能要等到返回或超时。
- 没有预算上限、Provider 级速率限制、队列背压或端到端费用保护。
- OpenAI-compatible 只实现 Chat Completions 共同子集，不保证覆盖各供应商私有参数和响应扩展。
- 当前只含原创 Demo，不捆绑 MMLU-Pro、GPQA、IFEval 等标准数据集，也不执行任何不可信代码。
- 评分仅含三个确定性客观 Evaluator；没有 LLM Judge、人工评审、Arena、Agent 或 Live Benchmark。
- Dataset Hash 用于一致性检查，不是发布者签名，也不能证明数据没有污染。
- 当前没有正式性能基线、灾难恢复 SLA、SBOM 或生产部署支持。

## 贡献

开始前请阅读 [`AGENTS.md`](AGENTS.md)、[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)、当前 Phase 文档和 [`CONTRIBUTING.md`](CONTRIBUTING.md)。贡献应保持小而可审查，新增行为必须有测试；涉及评分、数据格式、公开 API、持久化或安全边界的修改，需要同步更新协议、ADR 或相关文档。

提交 Pull Request 前至少运行：

```bash
make lint
make test
make smoke
cd frontend && npm run build
```

任何自动化测试都不得使用真实 Provider 或付费 API。无法运行的检查必须如实记录命令、原因和剩余风险，不能写成已通过。

## License

本项目采用 [MIT License](LICENSE)。
