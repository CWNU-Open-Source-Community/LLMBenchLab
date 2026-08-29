# 修复观测 Token 估算误触发全局 overdraw 执行计划

- Owner: Codex
- Status: in_progress
- Created: 2026-08-30
- Updated: 2026-08-30
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-30 工作日志](../worklogs/2026-08-30-fix-observational-token-overdraw.md)
- ADR: [ADR-0018](../decisions/ADR-0018-observational-token-estimates-are-not-hard-reservations.md)

## Context

OpenCode Go `hy3` 的 managed Run 在 15 题中成功完成 7 题后以 `governance_global_overdrawn` 终止。只读核查证明没有 429、HTTP retry 或 conservative settlement；第七次 Provider actual input 为 75，而本地非 hard UTF-8 估算为 59。所有 hard policy/Run override 均为 `null`，因此这是估算被错误当作硬预留的实现缺陷。

## Objective

在不改写 Provider actual usage、历史 Response、attempt ledger 或评分协议的前提下，分离观测估算与显式 hard reservation，安全修复历史 materialized overdrawn，并让当前数据库重新接纳 managed Run。

## Scope

- Runner/治理 repository 的 input/cost reservation 与 overdraw 派生修正。
- 双方言 Alembic 数据迁移和安全回滚。
- 当前 SQLite 的受控迁移与只读验证。
- 前端 overdrawn 文案与目标回归测试。
- README、API、Operations、Security、Testing、Phase/Status/Next Task/Changelog 同步。

## Non-goals

- 不重跑或继续旧失败 Run，不发送真实 Provider 请求。
- 不实现 OpenCode Responses/Anthropic Messages 协议。
- 不增加 Tokenizer 依赖、Provider-specific 模型白名单或自动预算推荐。
- 不删除/改写历史 ledger、actual usage、Response、audit 或 Run 终态。

## Assumptions

- `evaluation_runs.input_token_reservation` 是历史 managed Run 是否显式冻结 input hard bound 的权威事实。
- `reserved_output_tokens` 非空来自显式 `max_tokens`，继续是可执行的输出预留。
- 当前 API/Worker 可在迁移窗口停止；数据库没有 active reservation。

## Implementation steps

1. [completed] 只读定位 Run、policy、ledger、scope 和触发差异，接受 ADR-0018。
2. [completed] 实现 runtime reservation/overdraw 语义和 `20260830_0007` 数据迁移，并通过 SQLite/真实 PostgreSQL 迁移与回归验证。
3. [completed] 修正前端文案并补充 governance/runner/migration/frontend 回归；frontend `39 passed`。
4. [completed] 停止本地服务，备份并迁移当前 SQLite；head=`0007`，四层误判 `4→0`，旧事实保持。
5. [completed] 运行目标/完整测试、`make lint`、frontend build、Mock smoke、Compose config 与双方言 migration；完整结果见下表和工作日志。
6. [in_progress] 实现 SHA `c6212db…` 已 push；首次精确 SHA CI 的 backend、PostgreSQL/Redis integration、frontend 三项成功，real-Compose job 暴露验收脚本对合法 nullable conservative settlement 执行 `float(None)`。最小兼容修正与本地 9/9 Compose 验收已通过，修正 commit/push 和新的精确 SHA CI 待完成。

## Risks

| 风险 | 影响 | 缓解 |
|---|---|---|
| 清除真实 hard overdraw | 后续请求突破预算 | runtime/migration 都要求显式 Run input bound；output hard reservation保持独立 |
| 只改 flag 导致 ledger drift | 所有 admission fail closed | migration 与 runtime 共用同一可测试谓词语义，保留 ledger并重算 materialization |
| 活跃请求中迁移 | counter/结算竞态 | migration 拒绝 active reservation；本地先停止 API/Worker并复核 |
| 新 head 破坏 adoption/archive/acceptance | setup、归档或 CI 回归 | 更新 prepare-migration historical head、compatible allowlist、脚本常量和双方言测试 |
| 误发真实请求 | 产生费用/条款风险 | 自动化仅 Mock/fixture；本地验证只查 DB/API 状态，不创建 Run |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| Governance unit | `pytest tests/test_governance.py` | 新旧 hard/observational 边界全通过 | 已纳入 backend 全量；backend `946 passed, 33 skipped` |
| Runner unit | 目标 reliability 测试 | 无显式 bound 时 ledger reservation 为 null | 已纳入 backend 全量通过 |
| Migration | SQLite + PostgreSQL migration tests | 0006→0007/回滚/guard 全通过 | SQLite 与真实 PostgreSQL upgrade/downgrade/upgrade/check 通过；真实 PostgreSQL+Redis integration `33 passed` |
| Frontend | Run detail test | 新文案通过 | frontend `39 passed`；lint/typecheck/build 通过，保留既有 662.40 kB chunk warning |
| Current DB | backup + migrate + SQL/API check | 4 scopes overdrawn→0，历史 7 Responses/usage 不变 | head=`0007`；scope `4→0`；7 Responses/7 ledger、407 input/599 output 保留；13 业务表行数一致；`quick_check=ok`、FK=0 |
| Full gates | `make test`、`make lint`、build、smoke、Compose config | 全部通过 | backend `946 passed, 33 skipped`；frontend `39 passed`；`make lint` exit 0；Mock smoke `1 passed`；frontend build 与 `docker compose config` 通过 |
| Compose acceptance correction | `make phase2-acceptance` | nullable hard-bound seam 与其余故障矩阵通过 | `9/9 passed`，隔离容器、卷和网络清理通过；目标脚本单测 `16 passed` |
| Remote | push + exact-SHA GitHub Actions | 必需 jobs 全绿 | `c6212db…` run `33270167616` 为 3/4；real-Compose 因 acceptance-only `float(None)` 失败，修正后的精确 SHA 待执行 |

## Rollback

停止 API/Worker；保留 migration 前 SQLite 一致性备份。代码回滚时先在维护窗口执行 `0007 -> 0006`，它按旧 overdraw 谓词重算 flag，再运行旧应用。不得只替换代码、直接改 scope flag、删除 reservation 或把 actual usage 裁剪成 reservation。
