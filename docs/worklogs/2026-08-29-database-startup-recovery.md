# 2026-08-29 — 数据库启动与迁移恢复工作日志

## 元信息

- 日期：2026-08-29
- 执行者：Codex
- 分支：`codex/complete-evaluation-workflow`
- 初始 HEAD：`6df84d9bb26ee81c2f91ed9772dd6f45e9e17741`
- 计划：[数据库启动与迁移恢复修复](../plans/2026-08-29-database-startup-recovery.md)
- 阶段：[Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- 状态：`completed`

## 目标与背景

用户反馈旧数据库无法运行，只有重建数据库后才能启动。本任务先利用仓库保留的迁移前一致性备份安全复现，再修复受支持旧库的迁移/启动路径；不能把“删库重建”当解决方案。

## 范围

- Alembic head gate、SQLite migration preflight 和 `0004 -> 0005 -> 0006` 前向修复。
- 临时副本复现、回归测试、最小修复与必要文档。
- 本地/远程交付门禁。

## 非目标

- 诊断与回归不写原始 `.bak`；当前重建库只在生产修复和全量门禁通过后，经标准 preflight 自动备份再执行一次前向 migration。
- 不恢复已删除业务内容，不推进 P2-07，不改变 API/协议/依赖。
- 不通过 `create_all`、盲 stamp、drop 或降低完整性检查来过绿。

## 验收标准

- 重建前缺三个索引的 `0004` 备份临时副本能无损升级到 `0006` 并通过 `alembic check`。
- migration 可重复，当前 head 新库仍正常；partial/unknown schema 仍 fail closed。
- 启动入口连接预期数据库，API/Worker 不再要求重建库。
- 目标/全量测试、lint、smoke、文档/秘密/staged review 与 exact-SHA CI 通过。

## 假设与风险

- 备份是失败前一致性快照，只读原件、仅复制到临时目录。
- SQLite DDL 非事务性，故必须验证失败重入与部分结构，不能只测空库。
- 仓库存在 root `data/llmbenchlab.db` 与 backend `backend/data/llmbenchlab.db` 两个文件；必须确认真实入口，避免修错文件。

## 实施步骤

1. 只读核对配置、当前库、旧备份、Alembic revision 和启动入口。
2. 在临时副本复现失败并记录稳定根因。
3. 先补回归，再实施最小迁移/启动修复。
4. 完成目标/全量/启动验证和文档同步。
5. staged 审查、独立 commit、普通 push 与精确 SHA CI。

## 初始勘察

- 初始工作树 clean，HEAD 与 origin 同步。
- 当前 backend SQLite quick check 正常，revision=`20260828_0005`；业务表为空，存在两条 Worker generation 行。
- root 下另有 revision=`20260827_0003` 的小型旧 SQLite，但 Make 启动入口进入 `backend/` 后再加载相对 URL。
- 两个重建前 100 MB 备份 quick check 正常，revision=`20260827_0004`，且都没有 0005 新表/索引。`repair-before-0005` 精确缺少三个 canonical 0004 索引；随后的 `pre-alembic` 备份已补齐三者。
- 当前没有运行中的 LLMBenchLab 进程或 Compose service。
- 根因不是 SQLite 损坏：旧库在早期 0004 文件执行后已写入 0004 revision，随后同一 migration 文件增加了 `ix_evaluation_runs_started_at_id`、`ix_evaluation_runs_finished_at_id` 和 `uq_governance_policies_single_active`。当前 preflight 看到 revision/schema 不一致后按设计拒绝，在 0005 有机会运行前退出。

## 实际修改

- 新增 `20260829_0006` 前向 repair revision：先拒绝多条 active policy，再条件补齐三个 canonical governance indexes；已有同名索引的 reflection-visible 列序、唯一性和 partial predicate 必须匹配，SQLite 反射不完整的 DDL modifier 由标准 preflight 深检。downgrade 只回退 revision marker，保留本属于 canonical `0004` 的索引。
- `prepare_migrations` 将 `0005` 纳入 historical validation，只对白名单 canonical `0004/0005` 或三个 repair 索引的缺失子集放行，并在 SQLite 一致性备份前完成完整 schema/integrity/FK、索引和 single-active 行校验；新近成为 historical 的 PostgreSQL `0005` 也执行 metadata diff，任何额外 drift 继续拒绝。
- migration 回归覆盖真实历史形态、`0005` 部分 DDL 后中断重入、PostgreSQL historical drift、数据保持、错误索引定义、重复 active policy、额外 drift 和 no-op downgrade。
- audit archive v1 明确兼容 schema-equivalent `0005/0006`；Compose acceptance 分离 current head `0006` 与 Worker progress boundary `0005`。
- 新增并接受 ADR-0017，显式记录 repair revision、archive-v1 双 head 兼容，以及对已接受 ADR-0016/P2-07 exact recovery head 的 `0006` amendment；没有静默改写既有决定。
- 同步 README、Architecture、Security、Deployment、Operations、Testing、ADR-0015/0016、Phase 2、NEXT_TASK、CHANGELOG、状态/计划/日志。
- 在实现与全量门禁通过后，对当前 `backend/data/llmbenchlab.db` 执行标准 `make migrate`；preflight 生成 `llmbenchlab.db.pre-alembic-20260829T155710705255Z.bak`，随后仅推进 `0005 -> 0006`。

## 已运行命令与结果

- 强制文档、ADR-0002、Deployment/Testing、Make/setup/dev/migrate、migration/preflight 源码只读检查：完成。
- `git status --short --branch`：初始 clean。
- 两个数据库及备份的 SQLite `quick_check`/revision/table/index 匿名摘要：完成，未修改原文件。
- `repair-before-0005` 真实失败备份临时副本：`prepare`、`0004 -> 0005 -> 0006`、`alembic check` 全部通过；最终 integrity=`ok`、FK violations=`0`、三个 repair index 全部存在、匿名业务表计数保持；原件未修改，临时副本已清理。
- `cd backend && uv run pytest tests/test_migrations.py`：`52 passed`（含 SQLite partial-DDL resume 与 PostgreSQL historical `0005` drift/行门禁控制流）。
- 迁移/audit/archive/acceptance-script 定向集合：通过；包含新增 archive-v1 `0006` compatibility 回归。
- `make lint`：通过；Ruff check、153 files format check、ESLint、TypeScript typecheck 均为 exit 0。
- 最终 `make test`：通过；后端 `927 passed, 33 skipped, 1238 warnings`，前端 `38 passed`；warning 为既有 Starlette/httpx、Python 3.14 pytest-asyncio 弃用提示及迁移负向用例的 SQLAlchemy/Alembic warning。
- `npm --prefix frontend run build`：通过；2192 modules，保留既有 662.39 kB chunk warning。
- `make smoke`：`1 passed, 7 deselected`，完全离线 Mock。
- `docker compose config --quiet`：exit 0；未启动长期服务。
- 当前重建库 `make migrate`：exit 0，自动备份后 revision=`20260829_0006`；随后 `initialize_database()`=0、quick check=`ok`、FK violations=`0`、repair indexes=`3`、`alembic check` 为 `No new upgrade operations detected`。迁移前后业务表均为 0 行，既有 `worker_processes=2` 保持。
- staged review：`git diff --cached --check` 与秘密扫描通过，独立终审无 Blocker/High/Medium；实现 commit `8fb51b690ae6335b8ef93b3cbe54e039781fb173` 已普通 push 到 `origin/codex/complete-evaluation-workflow`。
- 精确 SHA GitHub Actions [run `33263405214`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33263405214)：4/4 成功；backend、真实 PostgreSQL/Redis integration、完整 Compose reliability acceptance 与 frontend job 全绿。

## 已知问题与下一步

- 当前新库已在自动备份后完成 `0005 -> 0006`，完整性、外键、schema fingerprint、startup head gate 与 `alembic check` 均正常；业务表计数和 Worker facts 保持。
- 修复只接受 canonical schema 或三个已知索引的缺失子集，并在补建唯一部分索引前拒绝多条 active policy；不会自动挑选或改写治理事实，额外 drift 仍拒绝。
- root `data/llmbenchlab.db` 是旧 `0003` 文件；支持的 `make setup/migrate/dev/backend/worker` 入口都会进入 `backend/`，本任务不删除或合并这个旧文件。不要从仓库 root 直接以相对 SQLite URL 绕过统一入口。
- 本地未单独重跑真实 PostgreSQL integration 或完整 Compose acceptance；实现精确 SHA run `33263405214` 的 PostgreSQL/integration/full-stack job 已完成远程覆盖，不把历史 P2-06 evidence 冒充本次运行。historical PostgreSQL `0005` 缺索引的 preflight/create 分支本次只有 Mock 控制流回归；标准真实 PostgreSQL CI 覆盖 fresh canonical existing-index 分支，不宣称覆盖该历史缺口。
- 实现、当前库、回归、普通 push 与实现精确 SHA 远程门禁均已完成；本任务停止于数据库兼容修复，不推进 P2-07。
