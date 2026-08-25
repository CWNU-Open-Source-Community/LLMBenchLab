# 2026-08-24 Bootstrap MVP 工作日志

- 开始：2026-08-24（Asia/Shanghai）
- 完成：2026-08-25（Asia/Shanghai）
- 最终状态：`completed`
- 交付阶段：Phase 0、Phase 1

## 初始仓库状态

- 父工作区是无 commit 的空 Git 仓库。
- 按首轮指令创建 `LLMBenchLab/` 并初始化独立 Git 仓库；初始分支为 `main`。
- 项目目录内无既有文件、remote、已跟踪内容、用户未提交代码或许可证，因此没有需要合并或保留的产品实现。
- 未执行 `git reset --hard`、强制推送、remote 修改、commit 或 push。

## 本次目标

交付 Phase 0 治理与架构文档，以及可离线端到端运行的 Phase 1 MVP：Mock 模型、Demo Benchmark、后台评测、三类客观评分器、持久化结果、前端进度/逐题证据/排行榜，以及测试、CI 和本地/Docker 运行配置。

## 范围

- FastAPI、SQLAlchemy 2.x、Pydantic v2、Alembic、SQLite 后端。
- React、TypeScript、Vite、React Router、Recharts 前端。
- 版本化 Benchmark、严格导入校验、稳定 SHA-256、原创 Demo 数据。
- Mock/OpenAI-compatible Adapter、Evaluator、进程内 Runner、19 个 `/api/v1` 路由。
- 后端/前端自动测试、纯离线 Smoke、Make、脚本、Compose、GitHub Actions 与开源仓库文件。
- README、治理、需求、架构、协议、数据格式、API、测试、部署、安全、Roadmap、Phase、ADR、状态和交接文档。

## 非目标

未实现大型公开数据集下载、执行不可信代码、LLM Judge、Arena、Agent、长上下文、多用户、支付、Kubernetes、微服务、PostgreSQL、Redis 或分布式 Worker；这些只进入 Phase 2–6 Roadmap。

## 假设与决定

- 默认运行模式是单用户、可信本地环境和 SQLite；MVP 不直接暴露公网。
- 自动测试、Smoke 与 CI 只使用 Mock/MockTransport/stub fetch，不调用真实 Provider。
- 默认协议为 `llmbenchlab-protocol-v1`：严格总分、完成率和回答准确率在 API/存储中均使用 0–100。
- 后台任务保留在 API 进程内；启动时把遗留 `running` Run 标记为 `failed`，不伪装为可恢复队列。
- OpenAI-compatible Key 只由 `api_key_env` 指向后端进程环境变量，数据库和 API 不接触实际 Key 值。
- 关键取舍分别记录在 ADR-0001 至 ADR-0004。

## 验收标准

- Phase 0 的目标、需求、架构、协议、Roadmap、阶段文档、ADR、AGENTS、PLANS 与 Project Status 完整。
- Mock Model → Demo Benchmark → Run → 15 个 Response → 汇总指标 → Leaderboard 的离线垂直链路可执行。
- 前端六个页面完成，不是占位壳；具备加载、空、错误、轮询、筛选、Demo 标识和响应式布局。
- 用户规格要求的后端/前端测试、lint、typecheck、build、Smoke、迁移和 Docker 验证提供真实证据。
- 不含真实秘密，不调用真实付费 API，不执行危险 Git 操作。

## 实施步骤

1. 勘察空仓库、记录边界并建立工作日志。
2. 完成 Charter、Requirements、Architecture、Protocol、Roadmap、Phase、ADR、AGENTS、PLANS 和模板。
3. 实现后端配置、领域模型、Schema、Alembic、REST API 与启动恢复。
4. 实现 Dataset Loader/Validator/Hash、Demo、Adapter、Evaluator、Runner、汇总与 Leaderboard。
5. 实现 React 六页面、集中 API Client、轮询、错误状态与移动布局。
6. 增加后端单元/API/Smoke 与前端组件测试。
7. 增加 Make、脚本、环境示例、Docker Compose、CI、Issue/PR 模板、License 与贡献指南。
8. 复跑真实门禁，修复 CORS 双 loopback 配置与协议百分比文档漂移，完成 README、Status、Next Task、Changelog 和日志。

## 实际修改

### 治理与文档

- 创建 `AGENTS.md`、`PLANS.md`、MIT `LICENSE`、`CONTRIBUTING.md`、`CHANGELOG.md` 与完整 README。
- 创建完整的核心 `docs/*.md`、本工作日志、Next Task、Phase 0–6、4 份 ADR 和 4 份复用模板。
- 固化 `llmbenchlab-protocol-v1`、Dataset Hash、0–100 指标口径、可比性规则和 MVP 威胁模型。

### 后端与数据

- 创建五个 SQLAlchemy 实体、Pydantic Schema、UTC 类型、约束/索引和可往返 Alembic 初始迁移。
- 实现 Model、Benchmark/Question、Run/Response、Leaderboard/Metrics 及 Health/Info API，共 19 个路由。
- 实现完全离线 Mock Adapter，以及有超时、有限指数退避、缺失 usage 容忍和错误脱敏的 OpenAI-compatible Adapter。
- 实现 Exact Match、Multiple Choice、Numeric Evaluator 及歧义、冲突、boxed、科学记数法和 tolerance 处理。
- 实现受限目录/ZIP Loader，包含重复键/ID、JSONL 行号、Unicode、资源上限、路径穿越、压缩炸弹、题型兼容与稳定 Hash 校验。
- 创建 15 道原创 `demo-general`，三种题型齐全，Hash 为 `5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe`。
- 实现原子 Run 领取、1–4 并发、单题错误隔离、逐题提交、协作取消、严格汇总和启动遗留状态处理。
- 完整快照模型连接/价格/执行策略并用于历史执行与展示；未知 usage/价格保持 `null`，不同 Benchmark 协议/版本/Hash 不混排。
- 收紧 Model 输入：Mock 禁止远端字段，URL 禁止凭据/query/fragment，默认参数只允许四个实际执行字段且拒绝嵌套/非有限值，价格拒绝 NaN/Infinity，422 不反射原始输入。

### 前端

- 实现 Dashboard、Models、Benchmarks、New Run、Run Detail 与 Leaderboard，集中管理 API/类型和格式化。
- 支持模型 CRUD、Demo 重载/详情、Run 参数、创建后跳转、轮询/取消、配置快照、逐题证据、排行榜筛选排序。
- 提供中文加载、空数据、可操作错误、明显 Demo 警示与桌面/移动响应式布局。
- 增加 4 个 Vitest 文件、13 个完全 stub 的测试，以及 ESLint、TypeScript 与 Vite build 配置。

### 开发与交付

- 创建锁定的 `uv.lock`、`package-lock.json`、`.env.example`、`.gitignore`、`.dockerignore` 和统一 Make targets。
- 创建安全的 setup/dev/smoke 脚本；不会覆盖已有 `.env`，Smoke 使用临时数据库。
- 创建 backend/frontend 非 root/静态容器构建、Nginx 同源 API 代理、双服务 Compose 与持久 SQLite volume。
- 创建 PR/main 触发的 GitHub Actions，以及 Bug/Feature Issue 和 PR 模板。

## 实际运行命令

以下为本任务实际执行过的主要命令；重复的修复后复跑不逐次展开。

```bash
# 仓库勘察与初始化
pwd
rg --files
git status --short --branch
git init

# 安装与静态检查
cd backend && uv sync --extra dev
cd frontend && npm install
cd backend && .venv/bin/ruff check app tests alembic
cd backend && .venv/bin/ruff format --check app tests alembic
cd frontend && npm run lint
cd frontend && npm run typecheck
cd frontend && npm run build

# 自动测试与离线垂直切片
cd backend && .venv/bin/pytest -q
cd frontend && npm test -- --reporter=verbose
make lint
make test
make smoke

# Alembic 临时 SQLite 往返
LLMBENCHLAB_DATABASE_URL=sqlite:////tmp/llmbenchlab-migration.2hvTLK/migration.db .venv/bin/alembic upgrade head
LLMBENCHLAB_DATABASE_URL=sqlite:////tmp/llmbenchlab-migration.2hvTLK/migration.db .venv/bin/alembic check
LLMBENCHLAB_DATABASE_URL=sqlite:////tmp/llmbenchlab-migration.2hvTLK/migration.db .venv/bin/alembic downgrade base
LLMBENCHLAB_DATABASE_URL=sqlite:////tmp/llmbenchlab-migration.2hvTLK/migration.db .venv/bin/alembic upgrade head

# 实际后端启动与探测
LLMBENCHLAB_DATABASE_URL=sqlite:////tmp/llmbenchlab-migration.2hvTLK/server.db .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/health
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/info

# Docker 静态、镜像与独立 Compose 运行验证
docker compose config --quiet
docker compose build backend
docker compose build frontend
docker run --rm -e DATABASE_URL=sqlite:////data/container-smoke.db -e LLMBENCHLAB_CREATE_TABLES_ON_STARTUP=false llmbenchlab-backend sh -c 'alembic upgrade head && alembic current && test -r /app/benchmarks/demo-general/manifest.json'
docker run --rm --add-host backend:127.0.0.1 llmbenchlab-frontend nginx -t
docker compose -p llmbenchlab-final-source build backend frontend
API_PORT=18002 FRONTEND_PORT=18082 docker compose -p llmbenchlab-final-source up -d --wait --wait-timeout 60 --no-build
curl --fail --silent --show-error http://127.0.0.1:18002/api/v1/health
curl --fail --silent --show-error http://127.0.0.1:18082/api/v1/health
curl --fail --silent --show-error http://127.0.0.1:18082/healthz
curl --fail --silent --show-error --output /dev/null http://127.0.0.1:18082/
# 随后经 API 注册带默认生成参数的 Mock、载入 Demo、创建并轮询 Run，再检查有效参数快照、15 Responses、Leaderboard 与 Metrics
API_PORT=18002 FRONTEND_PORT=18082 docker compose -p llmbenchlab-final-source down --volumes --remove-orphans
```

> 历史说明：上述 `LLMBENCHLAB_CREATE_TABLES_ON_STARTUP` 变量随后在 setup 修复中删除；当前容器入口先运行 `app.db.prepare_migrations`，再执行 Alembic upgrade。

还执行了 `bash -n`、ShellCheck、Actionlint、YAML 解析、OpenAPI/文档端点反查、内部链接检查、秘密模式扫描和 Git 状态检查。Docker 清理只删除独立 project `llmbenchlab-final-source` 的临时验证容器、网络和 SQLite volume，不涉及用户数据。补充 `.dockerignore` 后，frontend build context 从约 184.6 MB 降至 277.8 kB，并再次构建通过。

## 测试结果

| 命令/验证 | 结果 | 数量 | 失败与说明 |
| --- | --- | --- | --- |
| `.venv/bin/pytest -q` / `make test` 后端部分 | 通过 | 111 passed | 0 failed；83 条为 Python 3.14 下的上游弃用 warning |
| `npm test -- --run` | 通过 | 4 files / 13 passed | 0 failed；fetch/Recharts 均 stub，无网络 |
| `make smoke` | 通过 | 1 passed | 0 failed；临时 SQLite + Mock；其余非 smoke 用例 deselected |
| Ruff lint + format check | 通过 | 60 个 Python 文件检查 | 0 error |
| ESLint + TypeScript | 通过 | 全部 frontend 源码和测试 | 0 error / 0 warning |
| Vite production build | 通过 | 2191 modules transformed | 0 failed；有一个非阻断 >500 kB chunk warning |
| Alembic upgrade/check/downgrade/upgrade | 通过 | 1 个初始 revision | 0 schema drift / 0 failure |
| Uvicorn Health/Info | 通过 | 2 个 HTTP 200 | 0 failure |
| Mock API 垂直链路 | 通过 | 15/15 Responses | score、completion、answered accuracy 均 100；Leaderboard 有记录 |
| Docker images + Compose | 通过 | 2 images / 2 healthy services / 15/15 production Mock Run | API 直连、Nginx 代理、healthz、SPA、Responses、Leaderboard 与 Metrics 均通过；首次脱离 Compose 的 Nginx `nginx -t` 因 `backend` DNS 不存在失败，加入临时 host 映射后通过 |

## 未完成项与未运行验证

- 未调用真实 OpenAI-compatible Provider，也未配置真实 API Key；这是安全边界，不是 Phase 1 缺口。
- GitHub Actions workflow 已做静态校验，但未 push，因此没有 GitHub-hosted CI run URL。
- 未提交真实产品截图；README 明确保留截图说明，未用假图冒充。
- 未做公网、恶意多用户、高并发、长期负载或灾难恢复验证；均超出本地 MVP 范围。
- Phase 2–6 Roadmap 能力未实现，符合本次非目标。

## 已知问题

- 进程内任务无法在重启后自动恢复；遗留 `running` 会被标记为 `failed`。
- SQLite 与单进程去重不适合多实例或高并发；没有持久队列、租约、背压和 Provider 预算控制。
- 取消为协作式；已发出的上游请求可能持续到响应或超时。
- `base_url` 仍存在 SSRF、DNS 重绑定和题目外发风险；系统没有鉴权、TLS、限流或公网加固。
- 当前只有原创 Demo 和三个确定性 Evaluator，不代表正式模型能力。
- 前端 build 的 Recharts 依赖产生非阻断 chunk-size warning；可在后续做 route/chart lazy loading。

## 完成结论

Phase 0 和 Phase 1 的全部验收项均有代码、文档和实际执行证据，状态更新为 `completed`。仓库未 commit 或 push；最终 `git status --short` 中所有项目文件仍是新建未跟踪内容，留给用户审查。

## 下一步

进入 [NEXT_TASK.md](../NEXT_TASK.md) 定义的 Phase 2 可靠任务执行基础：以 PostgreSQL、Redis 和独立 Worker 增加持久恢复、租约、幂等与并发控制，同时保持现有 API 和 `llmbenchlab-protocol-v1` 语义兼容。
