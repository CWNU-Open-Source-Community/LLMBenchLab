# 2026-08-25 — 修复 setup 的 Alembic 既有表冲突

> 本日志记录实际发生的工作，不是事后美化的总结。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-25
- 执行者：Codex
- 关联阶段：[Phase 1 — MVP](../phases/PHASE-1-MVP.md)
- 关联计划：本日志“执行计划”章节
- 关联 ADR：[ADR-0002 — SQLite first](../decisions/ADR-0002-sqlite-first.md)
- 最终状态：completed

## 初始仓库状态

- 当前分支：`main`，仓库尚无 commit。
- `git status --short --branch` 摘要：`## No commits yet on main`；全部 Phase 0/1 文件均为未跟踪文件。
- 已有未提交改动：整个仓库属于用户现有工作；本任务只修改数据库初始化、迁移入口、迁移测试及相关文档，没有覆盖无关文件。
- 相关功能与测试现状：上一轮已验证后端 111 项、前端 13 项及 Alembic 临时库往返；本次用户实际执行 `make setup` 时，当时唯一的 `20260824_0001` revision 创建 `models` 报 `table models already exists`。
- 环境约束：本机数据库为 `backend/data/llmbenchlab.db`；自动测试必须离线且不得调用真实 Provider。

## 本次目标与背景

应用默认启动路径曾执行 `Base.metadata.create_all()`，而 `make setup` 又以 Alembic 管理相同五张表，形成两个 schema owner。现有 SQLite 已含 1 个模型、1 个 Benchmark、15 道题、1 个 Run 和 15 条 Response，且没有有效 revision，不能删库或盲目 stamp。本任务让 Alembic 成为唯一运行时 schema owner，并在保留全部现有数据的前提下让 `make setup` 成功且可重复执行。

## 范围

- 移除普通应用启动时的自动建表行为，缺少 schema 时给出明确迁移提示。
- 为无版本标记的受支持旧 SQLite schema 增加严格识别、一致性备份和 Alembic 收养入口。
- 把旧 schema 到当前 schema 的数据保留变更纳入可前进/回退的 revision 链。
- 让 setup、migrate 与容器启动复用相同行为。
- 增加 clean、legacy、漂移拒绝、幂等与数据保留测试，并同步运行文档。

## 非目标

- 不推进 Phase 2 的 PostgreSQL/Redis/Worker。
- 不更改 REST API、Benchmark 协议或评分语义。
- 不删除、重建或手工编辑用户现有 SQLite 数据。

## 验收标准

- [x] 全新空库可迁移到 head，重复执行不报错。
- [x] 当前未版本化旧库先生成一致性备份，再无损迁移到 head。
- [x] 模糊、部分或未知漂移 schema 会在 stamp/迁移前拒绝，原库保持不变。
- [x] 普通 backend 启动不再调用 `create_all`，缺少迁移时快速给出可操作错误。
- [x] 目标迁移测试、完整后端测试、Ruff、离线 Smoke 和 `make setup` 通过。
- [x] README、Deployment、Testing、Changelog、Project Status、Phase 1、Next Task 与本日志一致。

## 假设

- 仓库仍是未发布的 `0.1.0` development baseline，因此可以补充 legacy revision，同时保持当前 head ID `20260824_0001` 不变。
- SQLite 旧库来自本项目较早的 ORM `create_all`。metadata diff 恰有六项已知差异；任何额外差异均拒绝自动收养。
- 旧题目插入顺序可由 SQLite `rowid` 恢复；旧 Loader 按数据集顺序连续插入，当前 `position` 采用从 0 开始的顺序。

## 风险

| 风险 | 影响 | 缓解措施 | 结果 |
| --- | --- | --- | --- |
| 错误 stamp 掩盖未知 schema | 后续查询或迁移损坏 | metadata diff、PK/UQ/FK/index/CHECK 多重签名、DDL modifier/table option、trigger、完整性和外键检查；保留重复约束数量；head 也校验 | 未知与构造漂移均在 stamp/backup 前拒绝 |
| SQLite DDL 中途失败 | 部分迁移 | 收养前用 SQLite backup API 生成同目录一致性 `.bak`；legacy 数据和 rowid 在 batch DDL 前校验 | 实际备份完整；往返与失败前置测试通过 |
| 新增 `position` 改变题目次序 | 结果与数据集不一致 | legacy SQLite 按每个 Benchmark 的 `rowid` 赋 0-based position | 实际 15 题为 0–14，行数与关联记录不变 |
| 启动不再自动建表 | 未执行 setup 的用户启动失败 | fail-fast 提示 `make setup`/`make migrate`，同步 Quickstart 与部署文档 | 启动门禁测试和实际 Health/Info 通过 |

## 执行计划

- Owner: Codex
- Status: completed
- Created: 2026-08-25
- Updated: 2026-08-25
- Related phase: [Phase 1](../phases/PHASE-1-MVP.md)
- ADRs: 沿用 [ADR-0002](../decisions/ADR-0002-sqlite-first.md)，本修复恢复其“Alembic 是唯一 schema owner”的既有决定，无需新增 ADR。

### Context

失败迁移留下空 `alembic_version` 表，而五张业务表来自较早 `create_all`。当前 schema diff 为：两个价格列由 NOT NULL 改为 nullable、Model 缺两个 Provider 配置 CHECK、Question 缺 `position` 及其唯一约束。

### Objective

使用户可直接再次执行 `make setup`，获得备份、保留已有数据并到达 Alembic head；后续 setup/migrate 均幂等。

### Requirements

- ADR-0002：所有运行时 schema 变化必须由 Alembic 管理。
- AGENTS.md：数据库改动必须有前进 migration、回滚说明、测试和文档证据。

### Implementation steps

1. [completed] 固化根因、旧库签名、数据量与迁移策略。
2. [completed] 实现 legacy revision、schema preflight/backup/stamp 与统一迁移入口。
3. [completed] 移除普通启动 `create_all`，增加缺 schema 的明确错误。
4. [completed] 增加 clean/current/legacy/unknown/idempotent 与 SQLite 反射盲区回归测试。
5. [completed] 迁移实际数据库，运行 setup、迁移往返、测试、lint、smoke 和容器验证。
6. [completed] 更新状态文档、完成代码/文档审查和安全边界检查。

### Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
| --- | --- | --- | --- |
| 迁移回归 | `cd backend && uv run pytest tests/test_migrations.py -q` | 全部通过 | 19 passed |
| 后端回归 | `make test` | 0 failed | 130 passed；86 warnings；前端 13 passed |
| 代码质量 | `make lint` | 0 error | Ruff/format、ESLint、TypeScript 全通过 |
| 实际 setup | `make setup`（现有开发库） | 迁移 head、退出 0 | 连续执行两次均退出 0，无新备份 |
| 数据核验 | SQLite revision/integrity/FK/count/position | head、完整、数据不丢失 | `0001`、`ok`、0 FK violation、1/1/15/1/15、position 0–14 |
| 离线链路 | `make smoke` | Mock smoke 通过、无真实网络 | 1 passed，4 deselected |
| 前端构建 | `npm --prefix frontend run build` | production build 成功 | 成功；仅既有 >500 kB chunk warning |
| 容器迁移 | build 后在隔离 volume 两次 preflight/upgrade | 首次到 head、再次幂等 | 成功，`alembic check` 无新操作 |

### Rollback

收养前生成的备份为 `backend/data/llmbenchlab.db.pre-alembic-20260825T024715642384Z.bak`。恢复时先停止 backend，把当前 DB 另存，再将该备份复制为 `backend/data/llmbenchlab.db`，随后运行 `make migrate`，让 preflight 重新验证并升级。`0001 -> 0000` 会把 NULL 价格写成 0 并删除显式题目位置，`downgrade base` 会删除业务表；二者只允许在隔离临时库或明确接受数据损失且已有备份时执行，本任务未对实际开发库 downgrade。

### Documentation updates

- [x] README / 用户操作说明
- [x] Deployment / Testing / Architecture
- [x] CHANGELOG、PROJECT_STATUS、Phase 1、NEXT_TASK、工作日志

## 实际修改

| 文件/模块 | 修改内容 | 对应需求/原因 |
| --- | --- | --- |
| `backend/alembic/versions/20260824_0000_legacy_schema.py`、`20260824_0001_initial.py` | 将 ORM-era 结构固化为 `0000`，由 `0001` 数据保留升级到当前结构；升级前校验数据和 rowid | 为已知旧库提供真实 revision 目标与可测试迁移链 |
| `backend/app/db/prepare_migrations.py`、`backend/alembic/env.py` | 在写锁内识别、验证、备份和 stamp；校验 metadata、约束数量/内容、SQLite DDL/table options、trigger、完整性与 FK | 防止盲目 stamp、TOCTOU 数据变化和 SQLite 反射盲区 |
| `backend/app/main.py`、配置与测试 fixture | 删除运行时 `create_all`；启动必须在 Alembic head；仅隔离测试显式建表并 stamp | 让 Alembic 成为唯一运行时 schema owner |
| `scripts/migrate.sh`、setup/Smoke、Makefile、Docker/Compose/CI | setup、migrate、容器入口共用 preflight + upgrade；删除旧启动建表开关 | 各运行入口行为一致且幂等 |
| `backend/tests/test_migrations.py` | 19 项空库、legacy、备份、数据保留、head、重复/漂移拒绝与启动门禁回归 | 证明修复及拒绝边界 |
| README、Deployment、Testing、Architecture、状态与本日志 | 记录操作、恢复、限制和真实验证结果 | 用户可复现且状态一致 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
| --- | --- | --- | --- |
| 10:35 CST | discovery | 业务库有完整 Demo 数据，`alembic_version` 存在但无 revision；不能删库重建 | 必须实现数据保留收养 |
| 10:38 CST | discovery | Alembic metadata diff 精确为六项已知旧结构差异 | 用 legacy revision 与严格白名单识别 |
| 10:45 CST | decision | 沿用 ADR-0002，让 Alembic 成为唯一运行时 schema owner | 移除普通启动 create_all |
| 11:05–11:25 CST | review | Inspector/autogenerate 不覆盖 CHECK 重名、冲突策略、generated shorthand、重复 UQ/FK 等 SQLite 语义 | 增加 raw DDL/PRAGMA/约束数量防线和 head 校验；最终审查无 P0/P1 |

## 实际运行命令

| 命令 | 目的 | 退出码 | 结果摘要 |
| --- | --- | ---: | --- |
| `sqlite3 backend/data/llmbenchlab.db ...` | 检查表、DDL、版本、完整性、FK 和行数 | 0 | 初始 revision 为空、最终为 `0001`；1/1/15/1/15 不变 |
| `cd backend && uv run python ...` | 比较旧库与当前 ORM metadata | 0 | 6 项已知差异 |
| `make setup && make setup` | 实际库迁移与幂等验证 | 0 | 两次均完成，无 schema 冲突或新备份 |
| `./scripts/migrate.sh && uv run alembic current && uv run alembic check` | 统一入口与 schema drift | 0 | head；No new upgrade operations detected |
| `make lint && make test && make smoke` | 全量质量、回归与离线垂直链路 | 0 | 130 后端、13 前端、1 smoke 全通过 |
| 并行 `make setup` / `make lint` 后串行重跑 lint | 验证安装与质量门禁 | 2，随后 0 | 首次 lint 与 npm 重装 `node_modules` 竞争，ESLint 文件瞬时缺失；安装完成后同命令全通过 |
| `npm --prefix frontend run build` | 前端生产构建 | 0 | 2191 modules；非阻断 chunk warning |
| `docker build ...` + 隔离 volume `docker run ...` | Python 3.12 镜像和两次迁移 | 0 | 首次 `0000 -> 0001`，第二次幂等，check 通过 |
| `docker compose config --quiet && bash -n scripts/*.sh` | 配置与脚本语法 | 0 | 无错误 |
| 本地 Uvicorn + `/health`、`/info` | 启动门禁后的实际服务探测 | 0 | 两个 HTTP 200，正常停止 |

## 测试结果

- 后端：130 passed，0 failed；86 条为 Python 3.14 上游弃用或刻意构造 SQLite 漂移产生的 warning。
- 迁移专项：19 passed，覆盖 clean/legacy/current/head、备份、往返、数据保留、启动门禁及未知 SQLite 语义拒绝。
- 前端：4 files / 13 passed；lint、typecheck 和 production build 通过。
- Smoke：1 passed、4 deselected；仅 Mock 与临时 SQLite，无真实 Provider。
- Docker：最终 backend image 构建成功；隔离 volume 首次迁移、第二次幂等与 `alembic check` 通过。

## 未运行验证

- 未调用真实 OpenAI-compatible Provider，也未配置真实 API Key；这是安全边界，不是本修复缺口。
- 未 push，因此没有 GitHub-hosted CI run URL；本地执行了对应 lint/test/build 门禁。
- 本次未重复启动完整双服务 Compose UI 栈；此前 Phase 1 工作日志已验证该栈，本次重新验证了 Compose 配置、backend image、容器迁移和本地 API 健康检查。

## 未完成项

- 无本任务内未完成项。

## 已知问题与限制

- 自动收养只支持 SQLite，且只接受严格匹配的 current 或已知 legacy schema；未知数据库必须人工评估。
- preflight 在验证、备份和 stamp 期间持有 `BEGIN IMMEDIATE`，但 setup 脚本随后以独立 Alembic 进程升级；不应在迁移期间同时运行可写 backend。
- `0001 -> 0000` 是有损 downgrade：NULL 价格会归零，`position` 会被删除；恢复实际数据优先使用一致性备份。

## 安全检查

- 真实密钥模式扫描：未发现提交内容中的真实 Provider Key、Bearer Token 或私钥；`.env`、数据库和 `.bak` 均被忽略。
- 真实 API 调用：否。
- 危险 Git 操作或 push：无；未 commit、未 reset、未删除用户数据。

## 结果与下一步

`make setup` 的既有表冲突已修复。实际开发库已安全到达 `20260824_0001 (head)`，原有 1/1/15/1/15 条记录和题目顺序均保留，备份完整可读；setup/migrate/容器路径均幂等。下一入口仍是 [NEXT_TASK.md](../NEXT_TASK.md) 的 Phase 2 可靠任务执行基础。

## 最终 Git 状态

```text
## No commits yet on main
?? （仓库全部 Phase 0/1 文件仍为未跟踪；本任务未 commit 或 push）
```
