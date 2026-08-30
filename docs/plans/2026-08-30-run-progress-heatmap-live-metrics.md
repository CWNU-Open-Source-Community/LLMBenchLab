# Run Detail 热力图与实时指标执行计划

- Owner: Codex
- Status: active
- Created: 2026-08-30
- Updated: 2026-08-30
- Related requirements: FR-API-05、FR-API-08、FR-UI-05、FR-UI-07、US-04、US-05、NFR-PERF-02、NFR-PERF-03、NFR-REL-01、NFR-UX-01、TST-03
- Related phase: [Phase 3 — Benchmarks](../phases/PHASE-3-BENCHMARKS.md)
- Worklog: [2026-08-30-run-progress-heatmap-live-metrics.md](../worklogs/2026-08-30-run-progress-heatmap-live-metrics.md)
- ADRs: 无；本切片新增只读、向后兼容的进度投影，不改变持久化结构、评分协议或安全边界

## Context

Run Detail 已每秒轮询 Run 与当前 100 条逐题证据，但运行中只有 `completed_questions` 随 Response 提交更新；`correct_questions`、严格总分、回答准确率、完成率、平均延迟和精确 Token/cost 要到终态重聚合后才完整。因此当前进度条会变化，指标卡却可能长时间保持初始值。逐题证据接口还包含 prompt/raw/reference 等大字段，不能为 12,032–20,000 题热力图每秒全量重复拉取。

Question 已有 Benchmark 内唯一 `position`，EvaluationResponse 对 `(run_id, question_id)` 唯一且只在题目完成后追加。可以用计划题数生成未执行槽位，以轻量 Response 投影按 absolute position 覆盖已完成格。为避免把应用时间戳或 UUID 误当成无遗漏的提交序列，进度读取采用固定 512 题 block：索引在同一数据库读取快照中返回 live metrics 与每个 block 的 `response_count`，客户端只补齐或重取计数变化的 block。实时指标由后端复用 protocol-v1 证据聚合语义，终态仍与持久化 Run 汇总核对。

## Objective

在 Run Detail 增加可访问、可悬停的逐题进度热力图，并让运行中的严格总分、准确率、完成率、错误数、平均延迟、已知 Token/成本随已持久化 Response 每秒更新，同时保持大型 Benchmark 的轮询负载有界且不下载题目或回答正文。

## Scope

- 新增 `GET /runs/{run_id}/progress` 只读索引接口，固定返回 `block_size=512`、计划/已完成题数、同一读取快照的证据派生 live metrics，以及全部计划 block 的 `block_index/response_count`；空 block 也以 count 0 返回。
- 新增 `GET /runs/{run_id}/progress/blocks/{block_index}` 只读 payload 接口，只返回该绝对位置范围内已持久化格子的 `position/outcome/score/latency_ms/input_tokens/output_tokens/estimated_cost/error_type`，并按 position 升序。范围由 `block_index * 512` 派生，未返回 position 隐式为 `not_run`。
- Run Detail 渲染绿/红/黑/白矩阵、中文图例、状态计数及鼠标悬停/键盘聚焦 Tooltip；状态不只依赖颜色。
- Run Detail 使用索引中的后端派生 score、completion rate、answered accuracy、平均延迟、正确/异常数与 usage/cost 已知覆盖；不得从未完全 hydrate 的前端格子子集重算主指标。
- 客户端比较索引 block count 与本地已 hydrate count，只读取非空或计数变化的 block；全部非空 block 同步完成前显示“同步中”，不能把尚未加载格误画成 `not_run`。终态先到时继续追齐 block，再停止进度轮询。
- 保留现有逐题详情分页、取消、治理提示和终态轮询停止行为。
- 更新 API、架构、README、测试、Roadmap/Phase/状态/Changelog/NEXT_TASK 和工作日志。

## Non-goals

- 不新增数据库列或 migration，不改写历史 Run/Response/ledger。
- 不改变 `llmbenchlab-protocol-v1` 的评分分母、完成率、answered accuracy 或 Run 精确 Token/cost all-or-nothing 语义。
- 不新增 WebSocket/SSE、Redis UI 通道、题目执行中间态或 Provider 流式 token 级进度。
- 不在热力图接口返回 prompt、choices、raw/parsed/reference answer、error message 或 Provider metadata。
- 不调用真实 Provider，不把本切片并入 P2-07，也不改变 Phase 2/3 整体状态。

## Assumptions

| 假设 | 依据 | 验证方法 | 不成立时的处理 |
|---|---|---|---|
| Question.position 是 Run Benchmark 内稳定、0-based、唯一的计划槽位 | 数据库唯一约束与 Runner 按 position 执行 | API 测试含乱序完成、缺口和边界 position | 若发现非法/越界 position，接口 fail closed，不把格子错位 |
| Response 对 Run/Question 唯一且追加后不更新 | `uq_responses_run_question` 与 Runner 持久化路径 | 单元/Smoke/现有恢复测试 | 若未来允许更新，block 不能只靠 `response_count` 判定变化，需新增持久化 revision/migration |
| 运行中实时指标可由当前持久化 Response 按终态同一公式派生 | NFR-REL-01 与 `aggregate_run_evidence` | 后端 fixture 与前端公式测试对照终态 Run | 任何公式漂移先统一共享合同，不创造第二套评分语义 |
| 最多 20,000 题可由 40 个固定 block 覆盖，且每秒只比较小型 index | Dataset `MAX_QUESTIONS=20_000`、正式集 12,032 题约 24 blocks | API payload 白名单、block 竞态测试、虚拟化浏览器验收 | 若实测不足，调整前端窗口化/刷新节流；不退回正文全量轮询 |

## Requirements

- [x] FR-API-05 / FR-API-08：进度 index/block 接口有明确 Schema、404 Run/422 block 边界、固定 512 block、`Cache-Control: no-store` 和 OpenAPI 测试，只暴露固定轻量字段。
- [x] FR-UI-05 / US-04 / US-05：运行中热力图和指标随 Response 持久化更新，终态停止轮询，逐题详情仍可审计。
- [x] FR-UI-07 / NFR-UX-01：四种状态有图例、文字/ARIA，Tooltip 支持 hover 与 keyboard focus，桌面/移动均可用。
- [x] NFR-PERF-02 / NFR-PERF-03：每秒 index 有界；客户端只 hydrate 非空/计数变化的 512 题 block，并以虚拟化 ARIA grid 控制 DOM；不轮询 prompt/raw/reference 正文全集。
- [x] NFR-REL-01：index live metrics 与 block counts 来自同一读取快照；index→block 之间的新提交不会漏格，旧 Run/旧 block 响应不能污染当前页面；terminal + reconciled 只做一次最终 Run/evidence 刷新，同路由新 `runId` 重置 evidence offset 0。
- [x] TST-03：后端 API、前端组件、轮询竞态、空/部分/终态和离线 Smoke 均有回归。

## Implementation steps

1. [completed] **冻结公共进度合同并添加失败回归**
   - Files/modules: `backend/app/schemas/`、`backend/tests/`、`frontend/tests/run-detail-page.test.tsx`。
   - Validation: 测试复现四种颜色、Tooltip、fixed-block 同步、运行中指标不更新和竞态重取需求，并在实现前按预期失败。初版 cursor 合同 4 个后端 red tests 已失败；因无单调提交序列，在生产实现前替换为本 fixed-block 合同。
2. [completed] **实现轻量后端 block 投影**
   - Files/modules: `backend/app/api/v1/runs.py`、新进度 Schema、日志路由合同。
   - Validation: index/block、空 Run、稀疏/乱序 position、状态优先级、负数/越界 block 422、并发插入、OpenAPI、no-store 与秘密字段断言通过；无 migration。
3. [completed] **实现热力图与实时指标**
   - Files/modules: 前端 API types/client、独立 Heatmap 组件、Run Detail、CSS。
   - Validation: hover/focus Tooltip、绿红黑白、计数、实时准确率/完成率/Token/cost、分页并发与终态停止测试通过。
4. [in_progress] **文档、完整验证、真实页面与交付闭环**
   - Files/modules: README/API/ARCHITECTURE/TESTING/CHANGELOG/ROADMAP/PROJECT_STATUS/PHASE-3/NEXT_TASK/计划/工作日志。
   - Validation: 目标测试、lint、完整 test、Mock smoke、frontend build、Compose config、真实浏览器验收、秘密/diff 检查、commit/push 与精确 SHA CI 全绿。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| index→block 之间并发提交导致 block 比 index 更新 | 中/高 | Response 只追加且 count 单调；block payload 可比旧 index 更新，客户端采纳更大实际 count，下轮 index 收敛 | 保留并发插入/幂等 reducer 测试；未来若允许更新则新增 revision/migration |
| 12k–20k DOM 格子拖慢页面 | 中/中 | 轻量 block、虚拟化 ARIA grid、事件委托、CSS containment、只刷新变化 block | 浏览器实测不足时调窗口/节流；不能减少状态正确性或恢复正文轮询 |
| live metrics 偏离终态聚合 | 低/高 | 后端复用 `aggregate_run_evidence` 语义并在 index 同快照派生；终态 fixture 逐字段对照 | 统一后端聚合实现，禁止前端从部分 cells 创造第二套主指标 |
| 颜色对色觉/键盘用户不可用 | 中/高 | 图例、状态计数、ARIA label、focus Tooltip、可见 focus ring | 无障碍测试失败则不交付该组件 |
| Token/cost 部分证据被误当完整账单 | 中/高 | 运行中始终标“已知小计/覆盖”，只有全题全覆盖且 Run 精确字段一致才显示精确值 | 回退精确标签，保留已知小计与覆盖率 |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| 后端进度 API | `cd backend && uv run pytest tests/test_run_progress_api.py tests/test_response_metadata_api.py -q` | index/block、状态、指标、竞态、字段边界、404 Run/422 block/OpenAPI/no-store 通过 | `37 passed`；初版 cursor red tests 的 `4 failed` 保留为已废弃合同的失败先行记录 |
| 前端 Run Detail | `cd frontend && npm test -- --run tests/run-detail-page.test.tsx tests/run-progress-heatmap.test.tsx` | 热力图、Tooltip、实时指标、轮询/竞态通过 | `32 passed`（Run Detail `20` + heatmap `12`）；含 terminal reconciled 单次最终刷新与同路由 `runId` 切换 offset 归零回归 |
| 静态与构建 | `make lint`、`cd frontend && npm run build` | Ruff/format、ESLint、TS、production build 通过 | 两项均通过 |
| 完整回归 | `make test`、`make smoke` | 全量与离线 Mock 纵向链路通过 | backend `964 passed, 33 skipped`；frontend `64 passed`；Smoke `1 passed, 7 deselected` |
| 部署配置 | `docker compose config --quiet` | exit 0 | 通过 |
| 实页交互 | Browser 打开历史 198 题 Run | 状态/指标、四种颜色、Tooltip、移动宽度、console 无错 | Run `a3de7e4d-40b2-4d8c-994b-c713047393ae` 显示 179 passed / 17 wrong / 2 error，Token `45,509 / 4,561,625`、覆盖 `196/198`；desktop/768/375 无横向溢出，console 无 warning/error，键盘与 Tooltip 通过 |
| 大型虚拟化 | 前端自动化使用 12,032 / 20,000 题 fixture | DOM 节点有界且键盘定位正确 | 通过；这是自动化组件验证，不是 12,032/20,000 题实页浏览器性能测量 |
| 安全与范围 | `git diff --check`、staged diff、秘密扫描、`git status --short` | 无凭据/正文泄漏、无无关改动 | 本地范围/秘密/diff 复核通过；提交前按流程重复确认 staged tree |

## Rollback

无数据库迁移或数据写入。可反向应用本任务的 API/UI/文档补丁；旧 `/runs/{id}` 与 `/responses` 合同继续兼容。不得使用 reset/checkout 覆盖其他工作。

## Documentation updates

- [x] README / `docs/REQUIREMENTS.md` / 用户操作与验收边界
- [x] `docs/API.md` / `docs/ARCHITECTURE.md`
- [x] `docs/TESTING.md`
- [x] `CHANGELOG.md`、`docs/ROADMAP.md`、`docs/PROJECT_STATUS.md`、Phase 3、`docs/NEXT_TASK.md`、工作日志
- [x] `docs/SECURITY.md`：无需修改；接口为同一可信本地 Run Detail 的只读固定白名单，不扩大既有安全边界

## Completion evidence

- Changed files: 后端 progress Schema/service/routes、共享证据聚合、日志合同与测试；前端 API types/client、polling hook、虚拟化 Heatmap、Run Detail/CSS 与测试；本计划列出的强制文档
- Commands run: 后端/前端定向测试、`make test`、`make lint`、`make smoke`、frontend build、`docker compose config --quiet`、可信 loopback 浏览器验收
- Acceptance evidence: backend target `37 passed`；frontend target `32 passed`（Run Detail `20` + heatmap `12`）；完整 backend `964 passed, 33 skipped`、frontend `64 passed`；Smoke `1 passed, 7 deselected`；lint/build/Compose config 通过；目标 Run 实页与三档宽度、键盘/Tooltip/console 验收通过
- Not run: commit/push 后的远端精确 SHA CI；12,032/20,000 只做了自动化虚拟化边界测试，未将其描述为大型真实 Run 的 DevTools 性能测量
- Known issues: 无本地阻断；计划保持 `active`，直到提交、push 与远端 exact-SHA CI 完成

## Decision and discovery log

| 日期时间 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-30 | discovery | 运行中仅 `completed_questions` 逐题写回；正确数、准确率、平均延迟等终态才聚合 | 实时指标必须基于持久化 Response 只读投影，不能只改 CSS |
| 2026-08-30 | deviation | 初版先写了 `(created_at,id)` cursor 合同，4 个后端 red tests 按预期失败；复核发现 Response 没有数据库单调提交序列，应用 `created_at` 与 UUID 不能证明并发提交无遗漏 | 在任何生产实现前废弃 cursor，测试与文档改为固定 512 absolute-position block；本切片仍无需 migration |
| 2026-08-30 | decision | `/progress` index 在同一读取快照返回 live metrics 与所有 block counts；`/progress/blocks/{block_index}` 返回固定白名单 absolute-position cells | 每秒 index 有界，乱序完成和 index→block 并发可通过单调 response_count 最终收敛 |
| 2026-08-30 | decision | live 主指标由后端按 protocol-v1 证据派生；前端只展示 index 结果和同步 block，不从部分 Map 重算 | 避免同步窗口制造成绩漂移；Run 精确 nullable Token/cost 语义不变 |
| 2026-08-30 | decision | 未执行格只需要隐式 position，不返回未执行题正文/答案；已完成格只返回 Tooltip 必需字段 | 热力图不扩大正文暴露面，轮询负载有界 |
