# Web 评测导航、生成预算与布局修复执行计划

- Owner: Codex
- Status: in_progress (feature pushed; required exact-SHA CI did not trigger because the branch has no PR)
- Created: 2026-08-27
- Updated: 2026-08-27
- Related requirements: FR-MOD-11、FR-RUN-02、FR-RUN-10、FR-REP-02、FR-API-05、FR-UI-01、FR-UI-04、FR-UI-05、FR-UI-07、US-04、NFR-REP-02、NFR-UX-01
- Related phase: [Phase 2](../phases/PHASE-2-RELIABILITY.md)、[Phase 3](../phases/PHASE-3-BENCHMARKS.md)
- Worklog: [2026-08-27-web-run-ux-and-generation-budgets.md](../worklogs/2026-08-27-web-run-ux-and-generation-budgets.md)
- ADRs: 无；不改变通用协议默认、评分、数据库实体或安全架构

## Context

Web 的 New Run 表单始终显式发送 256，导致模型默认参数无法生效，且正式推理题容易耗尽输出预算。已有本地 Run 证明 256 会造成空内容或无最终答案；提高额度后，固定 60 秒读取超时成为第二个失败源。系统已有完整 `GET /runs` API，但前端没有 Run 列表路由，用户离开详情后无法找回非 completed Run。Run Detail 又只读取前 100 条证据。响应式 CSS 在窄屏还会被长 `<pre>` 撑宽后由 `overflow:hidden` 裁掉，并有 label 高度与三列网格错位。

## Objective

让用户能在 Web 中以可见、可快照、有限保护的配置运行长推理评测，随时从“评测记录”找回所有 Run，并完整分页查看正式数据集证据；桌面、移动及关键断点邻接布局均无已复现的裁剪或错位。

## Scope

- 后端 Run/Model 参数校验、Run 快照、OpenAI-compatible 空响应诊断及相关测试。
- 前端 New Run、App 导航、Dashboard、Run 列表、Run Detail、API client/types、CSS 与组件测试。
- README、API、Benchmark Protocol、Architecture、Security、Testing、状态/阶段/NEXT_TASK/CHANGELOG/工作日志。

## Non-goals

- 不实现全局 Token/金额预算、RPM/TPM、背压、公平调度或生产熔断。
- 不保证 Provider 接受 131072 或把省略 `max_tokens` 当作无限；Provider 能力仍是外部约束。
- 不改变既有 Run、数据集 Hash、评分、排行榜分区或协议默认值。
- 不运行真实付费 Provider。

## Assumptions

| 假设 | 依据 | 验证方法 | 不成立时的处理 |
|---|---|---|---|
| 快照 JSON 可保存 null/timeout，无需迁移 | `evaluation_runs.model_parameters_snapshot` 为 JSON | 双方言 schema/现有迁移回归 | 若存在列约束则增加迁移与回滚说明 |
| Adapter 对 `max_tokens=None` 已省略字段 | `_build_payload` 仅转发非 None 值 | MockTransport 单测 | 若未省略则显式分支并测试 |
| `/runs`/responses 已支持分页 | 现有 FastAPI 路由与 PaginationDep | API 测试与本地 GET | 若 API 不足则做兼容性扩展并更新文档 |
| 显式 Web 预设不改变协议默认 | 现有 CLI 已有 profile 预设；最终值快照 | 协议文档与 snapshot 测试 | 若必须改变后端默认则停止并设计 protocol-v2 |

## Requirements

- [x] R1：数值 `max_tokens` 支持 `1..131072`；显式 `null` 表示不发送字段，快照保留 null，默认省略请求仍为协议 256。
- [x] R2：Run 支持有限 `read_timeout_seconds` 并写入 execution snapshot，Web 对长推理数据集提供更长显式预设。
- [x] R3：Demo/MMLU direct/MMLU official CoT/GPQA 的 Web 建议值分别可见且不会在用户手动修改后被静默覆盖。
- [x] R4：空 content 且 `finish_reason=length` 使用可操作的截断错误类型；其他空内容仍为 `empty_response`，秘密不泄漏。
- [x] R5：主导航提供 `/runs`，列表含所有状态、筛选、分页、进度、快照身份、时间和详情链接；仅有 active Run 时刷新。
- [x] R6：Run Detail 的逐题证据使用后端真实 total/offset/limit 分页，不再把前 100 条误报为全部。
- [x] R7：修复 390px Benchmark/Run snapshot 裁剪、模型表单同排输入高度不齐和 New Run system prompt 空列。
- [x] R8：自动化全程 Mock/MockTransport，不读取或输出真实 Key；文档明确费用、Provider 上限与不可比性。

## Implementation steps

1. [completed] **后端预算、超时与诊断**
   - 修改范围：evaluation schemas、model validator、run service、adapter、后端 tests。
   - 操作：集中定义 131072 上限；允许显式 null；增加有限 read timeout；先读 finish reason 再分类空响应。
   - 完成判据：目标 pytest 覆盖边界、snapshot、payload 省略和 length 分类。
2. [completed] **Web 预设与评测记录信息架构**
   - 修改范围：types/client/NewRun/App/Dashboard/new RunsPage/RunDetail。
   - 操作：实现标准集预设、手动值保护、Provider 默认开关、timeout 控件、Run 列表路由/导航/筛选/分页/轮询、详情返回入口和 evidence 分页。
   - 完成判据：Vitest 覆盖预设、导航、筛选、分页、轮询、证据 total。
3. [completed] **响应式与视觉修复**
   - 修改范围：styles.css 及必要 markup class。
   - 操作：收紧 grid min-width、修复 label alignment、system prompt 全宽、列表和分页响应式布局。
   - 完成判据：桌面、390px 与关键断点邻接宽度浏览器检查无根横向裁剪，关键控件对齐。
4. [completed] **文档与项目状态同步**
   - 修改范围：README/API/PROTOCOL/ARCHITECTURE/SECURITY/TESTING/CHANGELOG/PROJECT_STATUS/Phase 2/3/NEXT_TASK/worklog/plan。
   - 操作：记录精确语义、范围、验证证据和剩余 P2-05 风险。
   - 完成判据：实现与用户说明一致，内部链接/数字无漂移。
5. [completed] **完整门禁与交付**
   - 修改范围：全仓库。
   - 操作：目标测试→全量 lint/test/build/smoke/config/lock，浏览器回归，diff/secret scan，commit/push，查询精确 SHA CI。
   - 完成判据：本地门禁全绿；普通 push 成功；精确 SHA CI 状态如实记录。

## Risks

| 风险 | 可能性 | 影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|---|
| 高输出预算造成高费用 | 中 | 高 | 有限上限、显式提示、并发默认 1 | 建议先小集测试；不宣称预算保护 |
| null 在不同 Provider 行为不一致 | 高 | 中 | 文案使用“Provider 决定”，快照 null | Provider 400/截断按证据显示 |
| timeout 与重试放大等待/重复调用 | 中 | 高 | 有限范围、快照、说明 at-least-once | 用户降低 timeout/取消 Run；P2-05 后续治理 |
| 轮询或分页状态竞态 | 中 | 中 | 使用稳定 offset/limit，仅 active 且无请求进行时刷新 | deferred 请求测试慢分页不被轮询抢占；总数缩小时收敛到有效页 |
| CSS 断点回归 | 中 | 中 | 针对复现元素设置 minmax(0,1fr) | 浏览器桌面/移动迭代 |

## Validation

| 验收项 | 命令/检查 | 预期结果 | 实际结果与证据 |
|---|---|---|---|
| 后端目标行为 | `cd backend && uv run pytest <target tests>` | 新边界、snapshot、adapter 诊断通过 | 通过；显式目标用例 `11 passed`，CLI 新上限回归 `4 passed` |
| 前端目标行为 | `cd frontend && npm test -- --run <target tests>` | 预设/列表/分页/导航通过 | 首轮新增入口用例 `11 passed`；最终竞态/null/收敛复核为 3 files / `14 passed` |
| 全量质量门禁 | `make lint && make test && make smoke && cd frontend && npm run build` | 全部退出 0；无真实 Provider | 通过；后端 `442 passed, 6 skipped`，前端 9 files / `36 passed`，Smoke `1 passed, 6 deselected`，build 成功；仅既有 Vite chunk warning |
| 配置与锁 | `uv lock --check`、`docker compose config` | 退出 0 | 通过；二者退出 0 |
| 视觉回归 | 本地浏览器 390/561/681/901/1100/1101/1280px 检查 | 无已复现裁剪/错位，入口可达 | 通过；五项导航完整，根页面无横向溢出，Runs 中小屏卡片/桌面表格与关键页面对齐 |
| 秘密与无关改动检查 | `git diff --check`、`git status --short` 及高置信敏感词扫描 | 无格式错误、无 Key、范围正确 | 通过；added tracked lines 与 untracked files 的高置信扫描均无命中 |
| 远程交付边界 | push、精确 SHA Actions/PR 查询 | 普通 push 成功；远程状态如实记录 | `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 已 push；Actions `[]`、PR `[]`，未触发而非通过 |

## Rollback

本任务不迁移或重写持久数据。代码回退会恢复旧 UI/校验，既有 Run 快照仍保持自包含；含新 `read_timeout_seconds` 或 `max_tokens=null` 的历史快照由 Runner 读取 JSON，回退前应先停止 active Run，避免旧代码遇到新快照语义。不得使用 destructive Git 命令覆盖用户工作。

## Documentation updates

- [x] README / API / Architecture / Benchmark Protocol / Security / Testing
- [x] ADR 不适用；无数据库迁移
- [x] `CHANGELOG.md`
- [x] `docs/PROJECT_STATUS.md` 与 Phase 2/3
- [x] `docs/NEXT_TASK.md` 与本次工作日志

## Completion evidence

- 修改文件：后端 schema/service/adapter/runner、前端 Runs/New Run/Run Detail/App/API/CSS、对应测试与项目文档。
- 实际命令：目标 pytest/Vitest、`make test`、`make lint`、`make smoke`、frontend build、lock/Compose/diff 检查及真实浏览器多视口检查。
- 验收对应：R1–R8 均有实现、自动化或浏览器证据。
- 未运行：真实付费 Provider、Phase 2 Compose 故障套件与外部基础设施集成；本切片未改变队列/迁移/基础设施。
- 已知问题：P2-05 全局限流/Token 或金额硬预算仍未完成；功能提交已 push，但 workflow 仅监听 PR/main 且本分支无 PR，因此精确 SHA 没有必需 job，计划仍保持 `in_progress`。

## Decision and discovery log

| 日期时间 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-27 16:56 CST | discovery | 256 截断/空响应与 60 秒 timeout 均由已有 Run 证据确认。 | 后端和 Web 必须一起修。 |
| 2026-08-27 16:56 CST | decision | 数值上限提高到 131072，同时提供 null=Provider default；不称为无限。 | 更新前后端约束、文档与费用提示。 |
| 2026-08-27 16:56 CST | decision | API 默认仍为 256，Web/CLI profile 是显式预设。 | 保持 protocol-v1 默认与旧 Run 分区。 |
| 2026-08-27 17:20 CST | discovery | Runs 的宽表在 390px 虽可容器滚动，但信息密度和页面滚动边界仍不理想。 | 改为带字段标签的卡片行；最终扩展到 1100px 并覆盖断点邻接宽度。 |
| 2026-08-27 17:41 CST | review/validation | 严格类型、慢分页轮询竞态和 561–901px 根横向溢出在最终审查中被复现。 | 严格拒绝 bool/字符串、串行 quiet poll、paint containment 与扩展卡片断点；新增后端链路/前端 deferred 回归。 |
| 2026-08-27 17:43 CST | validation | 后端/前端全量、lint/typecheck/build、Smoke、lock/Compose/diff 与多视口浏览器回归通过。 | 进入文档最终化、commit/push 与精确 SHA CI 查询。 |
| 2026-08-27 17:47 CST | delivery | 功能提交 `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 已普通 push；`gh run list --commit <sha>` 与分支 PR 查询均返回空数组。 | workflow 仅监听 PR/main 且未获授权创建 PR；记录为“未触发”，不冒充远程通过。 |
