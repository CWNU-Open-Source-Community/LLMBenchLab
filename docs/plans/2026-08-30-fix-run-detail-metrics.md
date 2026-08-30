# 修复 Run Detail 错题与部分 Token 展示执行计划

- Owner: Codex
- Status: active
- Created: 2026-08-30
- Updated: 2026-08-30
- Related requirements: FR-RUN-08、FR-RUN-10、FR-API-08、FR-UI-05、NFR-UX-01、TST-04
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)、[Phase 3 — Benchmarks](../phases/PHASE-3-BENCHMARKS.md)
- Worklog: [2026-08-30-fix-run-detail-metrics.md](../worklogs/2026-08-30-fix-run-detail-metrics.md)
- ADRs: 无；本修复保留已接受的 `llmbenchlab-protocol-v1` 聚合语义，只扩充只读展示证据

## Context

Run `a3de7e4d-40b2-4d8c-994b-c713047393ae` 有 198 条 Response：179 条正确、17 条可解析但答错、2 条 Provider 异常。现有 Run Detail 把只统计异常的 `error_questions` 泛称为“错误题”，导致用户误以为未得分题只有 2 条。196 条 Response 有完整 usage，2 条异常没有 usage；协议要求精确 Run Token 在任一 usage 缺失时保持 `null`，但 UI 只显示破折号，未展示仍可审计的已知小计与覆盖率。

## Objective

在不改写历史 Response、不把部分 Token 冒充精确总量、也不改变 protocol-v1 排名口径的前提下，让 Run Detail 明确显示未得分、普通答错、执行异常，以及全量 Response 的已知 Token 小计和 usage 覆盖率。

## Scope

- 扩充 `GET /runs/{run_id}/responses` 的列表元数据，返回全量 Response 的已知 input/output Token 小计与各自报告题数。
- Run Detail 使用 `completed_questions - correct_questions` 显示“未得分”，并拆分普通答错与执行异常。
- Run 精确 Token 非空时继续显示精确总量；为 `null` 时显示已知小计、覆盖率和“完整总量未知”。
- 更新后端/前端回归测试、API/测试文档及强制状态文档。

## Non-goals

- 不修改 `EvaluationRun.input_tokens/output_tokens` 的 all-or-nothing 语义。
- 不回填或猜测两条历史异常调用的 usage，不更改成绩、Response、ledger 或数据库 schema。
- 不调用真实 Provider，不修改价格或成本口径。
- 不改变排行榜或 Dashboard 的完整 Token 聚合语义。

## Assumptions

| 假设 | 依据 | 验证方法 | 不成立时的处理 |
|---|---|---|---|
| built-in protocol-v1 每题分数为 0 或 1 | FR-EVL-07 与协议定义 | 后端既有测试及目标 fixture | 若出现分数扩展，改为新增明确的零分计数聚合，不能用整数相减 |
| Response 列表端点可承载与分页无关的只读聚合元数据 | 端点已返回全量 `total`，详情页与证据同源 | API schema/分页测试 | 若造成不可接受查询开销，改为专用 summary 端点并记录偏差 |
| 部分 Token 小计只能作为已知下界/证据覆盖，不是账单真值 | 两条异常 usage 为 `null`，Provider 调用非 exactly-once | 文案与测试断言“完整总量未知” | 禁止显示为无条件精确总 Token |

## Requirements

- [x] FR-UI-05：完成 Run 显示“未得分 19、普通答错 17、执行异常 2、正确 179”，口径互相可解释。
- [x] FR-RUN-08 / FR-RUN-10：保留精确 Run Token `null`，同时返回已知 input/output 小计与报告覆盖率。
- [x] FR-API-08：新增字段有明确、非负、分页无关的 Schema 与文档，不泄漏 Provider 正文或秘密。
- [x] NFR-UX-01：部分 usage 明确标为“已知小计”且提示完整总量未知；零 Response/全 usage/部分 usage 均可读。
- [x] TST-04：后端 API、前端组件、类型检查和 production build 覆盖新行为。

## Implementation steps

1. [completed] **冻结 API 与 UI 语义并建立回归夹具**
   - 修改范围：后端 responses Schema/API 测试、前端 Run Detail 测试设计。
   - 操作：定义 `known_input_tokens`、`known_output_tokens`、`input_token_reported_responses`、`output_token_reported_responses`，确认其为全量而非当前页聚合。
   - 完成判据：测试能复现 179/17/2 与 196/198 部分 usage 的展示要求。
2. [completed] **实现后端只读 usage 汇总与前端展示**
   - 修改范围：`backend/app/api/v1/runs.py`、`backend/app/schemas/evaluation_response.py`、`frontend/src/api/types.ts`、`frontend/src/api/client.ts`、`frontend/src/pages/RunDetailPage.tsx`、必要格式函数。
   - 操作：单次聚合返回 count/sum；UI 区分精确与部分 Token，并拆分未得分/普通答错/异常。
   - 完成判据：目标后端与前端测试通过，既有分页/轮询行为不变。
3. [in_progress] **文档、完整验证与交付复核**
   - 修改范围：API、TESTING、CHANGELOG、PROJECT_STATUS、Phase 2/3、NEXT_TASK、工作日志和本计划。
   - 操作：记录兼容语义、运行目标测试、lint、完整 test、smoke、build、Compose config，检查 diff/秘密/状态。
   - 完成判据：本地门禁通过并记录真实结果；按仓库规则 commit/push 后等待精确 SHA CI。

## Risks

| 风险 | 可能性 | 影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|---|
| 部分小计被误读为精确账单 | 中 | 高 | 主值和辅助文案同时标“已知/完整总量未知” | 回退部分展示，保留覆盖率诊断 |
| 分页页码影响全局小计 | 低 | 中 | 聚合查询不应用 offset/limit，分页测试跨两页断言相同 summary | 修正为独立聚合查询 |
| 新字段破坏现有通用 ListResponse 类型 | 中 | 中 | 为 responses 定义专用前端类型，保留既有 `items/total/offset/limit` | 调整 client 泛型而非污染所有列表 |
| 聚合查询增加详情读取成本 | 低 | 中 | 与现有 count 合并为一个常数列聚合查询 | 若实测异常，增加针对 run_id 的执行计划检查或专用缓存设计 |

## Validation

| 验收项 | 命令/检查 | 预期结果 | 实际结果与证据 |
|---|---|---|---|
| 后端 responses summary | `cd backend && uv run pytest tests/test_response_metadata_api.py` | 新旧分页与 Token 覆盖断言通过 | 与 Smoke 合并运行，后端目标共 `11 passed`；全量亦通过 |
| 后端聚合回归 | `cd backend && uv run pytest tests/test_smoke.py` | protocol-v1 精确 Token nullable 语义保持 | 合并目标 `11 passed`；离线 Smoke `1 passed, 7 deselected` |
| 前端 Run Detail | `cd frontend && npm test -- --run tests/run-detail-page.test.tsx tests/format.test.ts` | 错题拆分、部分/完整 Token 与分页通过 | `20 passed` |
| 静态与构建 | `make lint`、`cd frontend && npm run build` | Ruff/ESLint/TS/build 通过 | 首次 lint 仅 2 个 Ruff format 差异；格式化后全绿，build 2192 modules 成功并保留既有 chunk warning |
| 完整回归 | `make test`、`make smoke` | 全量自动化只用 Mock/Stub 且通过 | backend `951 passed, 33 skipped`；frontend `47 passed`；Smoke `1 passed, 7 deselected` |
| 部署配置 | `docker compose config --quiet` | exit 0 | exit 0 |
| 目标实页核对 | 本地 API + Browser 读取目标 Run | 179/17/2、196/198 和已知小计可见 | API `45,509/4,561,625`、`196/198`；页面 19/17/2、460.7万，第二页 8 未得分/2 执行异常，console error 0 |
| 秘密与无关改动检查 | `git diff --check`、`git status --short` 及敏感词检查 | 无格式错误、无凭据、范围正确 | added diff + untracked 高置信扫描无命中；`diff --check` 通过；18 个候选文件均在计划范围 |

## Rollback

本任务没有数据库迁移或数据写入。回滚只需反向应用本任务明确文件的补丁；现有 Run/Response/ledger 与 `EvaluationRun.input_tokens/output_tokens` 均不受影响。不得使用会覆盖其他工作的 reset/checkout。

## Documentation updates

- [x] `docs/API.md`：Response 列表新增全量 usage 汇总字段与精确/部分语义
- [x] `docs/TESTING.md`：部分 usage 与错题拆分回归
- [x] `CHANGELOG.md`
- [x] `docs/PROJECT_STATUS.md` 与 Phase 2/3 文档
- [x] `docs/NEXT_TASK.md` 与本次工作日志
- [x] README：补充用户可见的错题拆分与部分 Token 语义

## Completion evidence

- 修改文件：待完成
- 实际命令：待完成
- 验收对应：待完成
- 未运行：待完成
- 已知问题：两条历史异常调用的真实 usage 不可恢复；已知小计不等于 Provider 账单

## Decision and discovery log

| 日期时间 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-30 09:31 CST | discovery | 目标 Run 为 179 正确、17 普通答错、2 异常；196/198 有 usage，已知合计 4,607,134 | UI 必须拆分口径，Token 必须标部分覆盖 |
| 2026-08-30 09:31 CST | decision | 保留 protocol-v1 精确 Token all-or-nothing，只在 responses 读取 API 增加已知小计与覆盖率 | 无迁移、无历史数据改写、无需 ADR |
| 2026-08-30 09:44 CST | discovery | Run 与 Responses 并行读取没有共同事务快照；输入/输出报告数相等也不证明来自同一批题 | 精确展示要求题数、两侧全覆盖及小计一致；部分覆盖文案始终明确输入/输出边际 |
