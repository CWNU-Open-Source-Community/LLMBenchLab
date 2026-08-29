# 数据库启动与迁移恢复修复执行计划

- Owner: Codex
- Status: active
- Created: 2026-08-29
- Updated: 2026-08-30
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-29 数据库启动恢复工作日志](../worklogs/2026-08-29-database-startup-recovery.md)
- ADRs: [ADR-0002 SQLite-first](../decisions/ADR-0002-sqlite-first.md)、[ADR-0015](../decisions/ADR-0015-observability-worker-progress-audit-retention.md)、[ADR-0017](../decisions/ADR-0017-schema-equivalent-governance-index-repair.md)；ADR-0017 显式记录前向 repair 与 accepted P2-07 exact-head amendment

## Context

用户报告旧数据库无法运行，只有重建数据库后才能启动。当前配置指向本地 SQLite；现库已在 Alembic `20260828_0005`，而重建前备份记录了一个早期 `20260827_0004` 结构变体：revision 正确、完整性与外键正常，但缺少后来加入同一 revision 文件的三个索引。启动必须继续拒绝未知/损坏 schema，但这一精确历史变体应能由 `make setup` / `make migrate` 备份并无损升级，不能要求用户删库。

## Objective

定位并修复受支持旧 SQLite 数据库无法升级或启动的原因，使标准 `0004/0005` 和仅缺三个已知治理索引的历史变体可在保留数据和自动备份的前提下升级到新的前向修复 revision `0006`，同时继续 fail closed 拒绝真正的 schema 漂移。

## Scope

- SQLite migration preflight、Alembic `0004 -> 0005 -> 0006` 升级与本地启动入口。
- 基于重建前备份副本的可复现诊断。
- 针对根因的最小代码/迁移修复和回归测试。
- 必要的 setup/deployment/troubleshooting 与状态文档同步。

## Non-goals

- 不修改、删除或降级任何旧失败备份；当前重建库仅在修复和门禁通过后经标准 preflight 备份并前向升级。
- 不迁移默认用户数据到 PostgreSQL，不改变 API、评分协议或数据模型语义。
- 不恢复用户已主动删除的数据；只保证受支持数据库可无损迁移和启动。
- 不推进 P2-07 recovery verifier、Redis rebuild 或告警演练。

## Assumptions

- `backend/data/llmbenchlab.db.pre-alembic-20260828T121151372702Z.bak` 是失败前由迁移流程生成的只读一致性备份；只在临时副本上执行迁移。
- 当前 `.env` 的相对 SQLite URL由仓库脚本在 `backend/` 目录解析，目标是 `backend/data/llmbenchlab.db`。
- 根因已由 `repair-before-0005` 备份的 schema fingerprint 精确复现；另一份 `pre-alembic` 备份已含三个索引并可正常升级。

## Requirements

- 保留 ADR-0002 的 Alembic-only schema owner 与无损迁移原则。
- 不把 `create_all`、删库、stamp 猜测或降低校验作为修复。
- 测试必须先复现旧行为，再证明数据、revision、约束和索引都正确。
- 自动化只用临时 SQLite/Mock，不调用真实 Provider。

## Implementation steps

1. [completed] 在迁移前备份的临时副本上复现并锁定根因。
   - Files/modules: `scripts/migrate.sh`、`backend/app/db/prepare_migrations.py`、`backend/alembic/versions/20260828_0005_worker_progress_retention.py`。
   - Validation: 捕获稳定错误、失败前后 revision/schema/data 摘要，不写原始备份。
2. [completed] 新增失败回归并实施最小修复。
   - Files/modules: migration/preflight 与 `backend/tests/test_migrations.py`，只按根因扩展。
   - Validation: 旧回归在修复前失败、修复后通过；未知/partial schema 仍拒绝。
3. [completed] 验证新库、旧 head、重复迁移和真实启动路径。
   - Validation: 目标 pytest、Alembic upgrade/check、临时副本数据保持、API/Worker startup probe、Ruff。
4. [in_progress] 同步运维/状态文档并完成交付门禁。
   - Validation: diff/secret/staged review、commit/push、精确 SHA GitHub Actions 四 job 全绿。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| 误改用户当前库 | 低/高 | 失败现场只用临时副本；当前重建库确认无服务写入后只走自动备份的标准前向 migration | 立即停止，保留自动备份和现场，不做额外写入 |
| 把 schema 漂移误当可恢复 | 中/高 | 只接受精确已知 revision/fingerprint；保留 fail-closed 测试 | 拒绝自动修复并报告具体稳定错误 |
| SQLite 非事务 DDL 留下半迁移 | 中/高 | 迁移前一致性备份、测试失败重入与结构探测 | 从备份副本复核；不得 stamp 猜测 |
| 相对 URL 指向不同文件 | 中/中 | 测试 root/backend 两种 cwd，统一解析/诊断 | 固定入口解析并更新文档 |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| 旧库复现 | 两个 `0004` 备份只读 fingerprint + 临时副本 preflight | 缺三索引变体稳定触发既有严格拒绝；标准变体正常 | 已确认；原件未修改 |
| Migration tests | `cd backend && uv run pytest tests/test_migrations.py` | 全部通过 | `52 passed` |
| SQLite round trip | 真实失败备份副本 `0004 -> 0005 -> 0006 -> check` | 数据/约束/index/revision 正确 | 通过；integrity ok、FK 0、三索引存在、匿名计数保持 |
| Startup | 当前重建库标准 migrate + head gate | 能启动并只连预期库 | 通过；备份后到 `0006`，`initialize_database()`=0 |
| Full gates | `make lint`、`make test`、`make smoke`、frontend build、Compose config | 全部通过 | 通过；后端 927/33、前端 38、smoke 1/7、build/config exit 0 |
| Docs/security | links、diff、secret、staged review | 无未解决 Blocker/High/Medium | diff check 已通过；staged 终审待 commit 前执行 |
| Remote | 普通 push + exact-SHA CI | 四个 required job 全绿 | 待执行 |

## Rollback

代码修复可回退；迁移回归和失败现场验证仅作用于临时副本。当前重建库已通过标准 preflight 生成一致性备份后从 canonical `0005` 前进到 schema-equivalent `0006`；downgrade 到 `0005` 只回退 marker，但正常使用仍应保留当前 head。现有旧 `.bak` 不删除或改写。若新逻辑不能证明已知 schema exact match，则保持拒绝并让操作者从原备份恢复，而不是自动 stamp、drop 或重建。

## Documentation updates

- [x] README / setup 与旧库升级说明
- [x] Deployment / migration 排障与恢复路径
- [x] Testing / 新回归与实际结果
- [x] CHANGELOG、PROJECT_STATUS、Phase 2、NEXT_TASK、工作日志
- [x] API / Benchmark：不适用；Security 已同步 fail-closed compatibility 边界

## Completion evidence

- Changed files: migration/preflight/audit compatibility/acceptance constants、回归、README 与运维/安全/测试/状态文档。
- Commands run: migration 52 tests、真实失败备份副本 upgrade/check、`make lint/test/smoke`、Compose config、当前库 migrate/startup/check。
- Acceptance evidence: 本任务已执行的本地门禁全部通过；远程 exact-SHA 待 push 后记录。
- Not run: 本地真实 PostgreSQL integration 与完整 Compose acceptance 未单独重跑；required CI 的 PostgreSQL/integration/full-stack job 将作为精确 SHA 远程门禁。historical PostgreSQL `0005` 缺索引的 preflight/create 分支只有 Mock 控制流回归；标准 CI 的真实 PostgreSQL migration 覆盖 fresh canonical existing-index 分支，不把两者混写。
- Known issues: root `data/llmbenchlab.db` 仍是未由统一 Make 入口使用的旧 `0003` 文件；未删除。旧失败备份保留，是否含需恢复的业务数据由用户决定。

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-29 | discovery | 当前库为 SQLite `20260828_0005`，quick check 正常；仓库另有 root 下的旧 `0003` 文件，但 Make 入口从 `backend/` 解析数据库。 | 不修改当前库；从配置入口和重建前备份复现。 |
| 2026-08-29 | discovery | 重建前两个 100 MB 备份均 quick-check 正常、revision=`20260827_0004`、无 `worker_processes`/0005 indexes。 | 这是精确的旧 head 输入，可在临时副本验证无损升级。 |
| 2026-08-29 | root cause | `repair-before-0005` 比 canonical `0004` 精确缺少 Run started/finished 游标索引和 single-active policy 唯一部分索引；`pre-alembic` 备份已补齐三者。 | 只对白名单 fingerprint 放行，由新 `0006` 条件补建；未知 drift 与多 active policy 继续拒绝。 |
| 2026-08-29 | local delivery | 修复、全量门禁和失败备份副本验证通过后，当前重建库由 `make migrate` 自动备份并前进到 `0006`；startup/check 通过，数据计数保持。 | 当前本地运行阻塞解除；进入 staged/remote gate。 |
| 2026-08-30 | decision | 新 head 影响 ADR-0015 archive allowlist 与已接受 ADR-0016 的 P2-07 exact recovery head。 | 新增 accepted ADR-0017 显式 amendment，并同步 P2-07 plan/worklog/NEXT；不把这项合同变化藏在实现或旧 ADR 中。 |
