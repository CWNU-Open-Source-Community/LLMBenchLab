# 2026-08-27 — Web 评测导航、生成预算与布局修复工作日志

> 本日志记录实际发生的工作，不是事后美化的总结。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-27
- 执行者：Codex
- 关联阶段：[Phase 2 — 可靠性与任务执行](../phases/PHASE-2-RELIABILITY.md)、[Phase 3 — 标准 Benchmark](../phases/PHASE-3-BENCHMARKS.md)
- 关联计划：[Web 评测导航、生成预算与布局修复执行计划](../plans/2026-08-27-web-run-ux-and-generation-budgets.md)
- 关联 ADR：无；本任务增加显式 Run 配置与 UI 入口，不改变 `llmbenchlab-protocol-v1` 的通用默认值、评分或持久化模型
- 最终状态：in_progress（实现与本地门禁完成；阶段 commit/push/精确 SHA CI 待执行）

## 初始仓库状态

- 当前分支：`codex/complete-evaluation-workflow`，跟踪 `origin/codex/complete-evaluation-workflow`
- `git status --short --branch` 摘要：分支同步，工作树无未提交文件
- 已有未提交改动：无；本地 `artifacts/` 数据集与开发数据库均由 Git 忽略并须保留
- 相关功能与测试现状：后端全量基线 `427 passed, 6 skipped`，前端基线 `21 passed`；本地 Web/API/Worker 正在运行
- 环境约束：自动化不得调用真实 Provider；本任务只能读取已有 Run 证据，测试使用 Mock/MockTransport

## 本次目标与背景

用户实际运行 GPQA/推理模型时，Web 固定显式提交 `max_tokens=256`，导致 Provider 返回空内容或答案在最终选项前被截断。把额度提高后，固定 60 秒读取超时又会令慢模型失败。Web 还缺少全部 Run 的列表入口，离开详情页后无法重新进入 pending/running/failed/cancelled Run；大数据集详情固定只读取前 100 条证据。响应式 CSS 另有可复现的横向裁剪、表单控件高度不齐和三列表单空列。

## 范围

- 扩展显式 `max_tokens` 配置边界，支持“由 Provider 决定”这一字段省略模式，并增加可快照的单题读取超时。
- 为 Demo、MMLU-Pro direct、MMLU-Pro official CoT、GPQA-Diamond 提供 Web 显式预设与风险说明，不改变 API 的协议默认值。
- 对空内容且 `finish_reason=length` 提供可操作的稳定错误分类。
- 新增全部状态可见的 `/runs` 评测记录页、导航入口、筛选、分页和活跃 Run 刷新；修复 Dashboard/详情页出口。
- 为 Run Detail 逐题证据增加真实分页，避免正式数据集只显示前 100 条。
- 修复已复现的响应式裁剪、表单高度与生成参数网格错位，并做真实浏览器桌面/移动回归。

## 非目标

- 不实现 Provider RPM/TPM、金额预算硬上限或公平调度；仍属于 Phase 2 P2-05。
- 不把“由 Provider 决定”描述为真正无限输出；实际限制由 Provider、模型和上下文窗口决定。
- 不调用用户配置的真实模型，不重跑或删除现有 Run，不修改第三方数据集内容。
- 不改变评分分母、答案解析或排行榜协议分区。

## 验收标准

- [x] Web 对正式 Benchmark 使用可见的合理预设，允许数值上限内自定义或省略 `max_tokens`，并可配置单题读取超时。
- [x] 最终 generation/timeout 值写入 Run 快照并由 Adapter 原样执行；边界与截断诊断有后端测试。
- [x] `/runs` 从主导航可达，全部 Run 状态可筛选、分页、进入详情，活跃 Run 可刷新。
- [x] Run Detail 可分页查看超过 100 条逐题证据，并显示总数/当前范围。
- [x] 390px、关键断点邻接宽度与桌面无横向裁剪或已知表单错位；相关前端组件测试、lint、typecheck、build 通过。
- [x] 后端全量测试、离线 Smoke、文档与秘密检查通过；不调用真实 Provider。

## 假设

- `max_tokens=null` 表示不向 OpenAI-compatible Provider 发送该字段，而不是无穷额度；快照保留 `null`，与任何数值 Run 不直接比较。
- 显式 Web 数据集预设与已有 CLI profile 预设一样，不改变 API 省略字段时的协议默认 `256`，因此无需升级 protocol version。
- 读取超时属于可快照执行配置；增加显式字段不需要数据库迁移，因为快照存于 JSON。

## 风险

| 风险 | 影响 | 缓解措施 | 结果 |
|---|---|---|---|
| 更高输出预算增加费用和时长 | 真实 Run 费用上升 | 保留有限数值上限、展示 Provider/费用提示、默认并发 1 | 已实现提示与边界；没有冒充硬预算 |
| Provider 不支持高数值或字段省略行为不同 | 400 或 Provider 自定义截断 | 明确提示实际上限由 Provider 决定，保存最终快照和错误 | 已记录 null/数字语义与 `output_truncated`；真实兼容性由用户测试 |
| 更长读取超时叠加重试 | 失败收敛变慢且可能重复计费 | 设置有限上限并提示现有 at-least-once/重试边界 | 已限 1–1800 秒并写入快照；P2-05 风险仍保留 |
| 新列表轮询增加 API 请求 | 本地负载增加 | 仅存在 active Run 时低频刷新并分页 | 当前页 active 才轮询；轮询不与分页/筛选请求重叠，终态/卸载清理 timer |
| 响应式修复影响既有桌面布局 | UI 回归 | 组件测试加真实浏览器多视口检查 | 390–1100px Runs 采用字段卡片，1280px 为完整表格；关键断点均无根页面横向溢出 |

## 实施步骤

1. [completed] 只读复现并审计 generation、timeout、导航、证据分页与布局链路。
2. [completed] 实现后端显式预算/超时边界、截断诊断及测试。
3. [completed] 实现 Web 预设、评测记录页、证据分页、导航与布局修复及测试。
4. [completed] 更新 API/协议/架构/安全/测试/用户文档与项目状态文件。
5. [in_progress] 全量本地门禁与浏览器回归已完成；等待最终 diff/秘密检查、commit/push 与精确 SHA CI 查询。

## 实际修改

| 文件/模块 | 修改内容 | 对应需求/原因 |
|---|---|---|
| `backend/app/core/constants.py`、Run/Model schemas、`run_service.py` | 集中定义 131072 token 与 1–1800 秒边界；支持 null；冻结读取超时 | 长推理预算、Provider 默认与可复现快照 |
| `openai_compatible.py`、`evaluation_runner.py` | null 时省略字段；把 `finish_reason=length` 的空/不可解析结果归为 `output_truncated` | 让空结果具有可操作原因，同时保持严格计零 |
| backend tests | 覆盖数字/null/越界、timeout 快照、Adapter payload 与截断分类；同步 CLI 上限回归 | 防止 API/CLI/Adapter 约束漂移 |
| `NewRunPage.tsx`、types/client | Benchmark 建议、手改保护、应用建议、Provider 决定开关、timeout 与费用提示 | 解决 Web 固定 256 和固定 60 秒问题 |
| `RunsPage.tsx`、`App.tsx`、Dashboard | 主导航全状态 Run 列表、筛选、20 条分页、手动/active 刷新和稳定详情入口 | 离开详情后仍能找回评测 |
| `RunDetailPage.tsx` | Responses 每页 100 条、真实 total/范围/全局题号、返回列表 | 大数据集证据不再停在前 100 条 |
| `styles.css` | grid min-width、paint containment、label 对齐、System Prompt 全宽、五项导航断点、≤1100px Runs 卡片与分页 | 修复窄屏/平板根页面溢出和桌面错位 |
| frontend tests | 新增 App/Run 列表/New Run/Run Detail 15 个用例 | 覆盖入口、预设、默认 null、轮询竞态、总数收缩和分页 |
| README 与 docs | 同步 API/协议/架构/安全/测试/阶段/状态/NEXT_TASK/CHANGELOG | 明确 null 非无限、默认兼容及 P2-05 边界 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
|---|---|---|---|
| 16:56 CST | discovery | GPQA + DeepSeek 在 256 时已有 4/4 `empty_response`；4096 时仍有空内容。Local Qwen 在 256 恰好耗尽 256 output tokens 后缺少最终答案，4096 又触发固定 60 秒 read timeout。 | 必须同时处理输出预算、超时与截断诊断。 |
| 16:56 CST | discovery | `GET /runs` 已支持全部状态与分页，但前端无 `/runs` 路由；Dashboard/Leaderboard 只覆盖 completed Run。 | 新增前端列表即可，无需新增后端列表接口。 |
| 16:56 CST | discovery | 正式大数据 Run 详情固定 `limit=100`，计数文案把当前页误称为全部保存证据。 | 本轮增加 Response 分页。 |
| 16:56 CST | decision | 保留 API 的协议默认 256；Web 按 Benchmark 发送显式预设，`null` 只表示 Provider 默认。 | 不升级 protocol version，但文档必须明确可比性。 |
| 17:10 CST | decision | Web 数字上限统一为 131072，读取超时上限 1800 秒；GPQA Web 建议 8192/600。 | Provider 仍可拒绝；数值/null/timeout 均进入快照。 |
| 17:20 CST | discovery | 390px 下宽表虽有内部滚动，但信息密度不适合逐条找 Run。 | 先在 560px 以下改为字段化卡片，最终按断点复核扩展到 1100px。 |
| 17:22 CST | validation | 浏览器实测 Benchmark 长 pre、Run snapshot、新建表单、模型弹窗和 Runs 列表。 | 390px 根宽均为 390；桌面输入框等高、System Prompt 全宽、列表操作列完整。 |
| 17:34 CST | review | Pydantic 会把 JSON `true` 宽松转换为 1 token/1 秒；定时轮询可抢占慢分页请求并使 Loading 永不清除；`seed:null` 与费用提示也有语义漂移。 | 严格拒绝 bool/字符串；新增 Run→Runner→Provider timeout/null 链路测试；轮询串行化并修正 null/费用/总数收敛。 |
| 17:41 CST | validation | 561/681/901px 的宽表滚动内容会传播到根页面；仅验证 390/1280 会漏掉该区间。 | table scroll 增加 paint containment，Runs 卡片断点扩到 1100px；390、561、681、901、1100/1101、1280px 根宽与导航均通过。 |

## 实际运行命令

| 命令 | 目的 | 退出码 | 结果摘要 |
|---|---|---:|---|
| `git status --short --branch` | 初始工作树检查 | 0 | 分支同步，工作树干净 |
| 本地只读 API/浏览器检查 | 复现 Run 证据、导航与多视口布局 | 0 | 根因与明确 CSS 裁剪/错位已定位；未调用 Provider |
| 显式目标 pytest | 校验 API snapshot、数字/null/timeout、Adapter 省略与 length 分类 | 0 | `11 passed`；CLI 上限回归另 `4 passed` |
| 新增前端定向 Vitest | 校验 New Run、Runs、Run Detail、App 导航 | 0 | 共 `11 passed` |
| 首次 `make test` | 全量回归 | 2 | 后端 `434 passed, 6 skipped`，发现一条 CLI 测试仍把 32769 当旧上限；测试已更新为集中常量 + 1 |
| 最终 `make test` | 后端与前端全量 | 0 | 后端 `442 passed, 6 skipped`；前端 9 files / `36 passed` |
| 首次 `make lint` | 格式与静态检查 | 2 | Ruff 报 2 个新增测试文件需格式化；仅运行 Ruff formatter 后重试 |
| 最终 `make lint` | Ruff/format、ESLint、typecheck | 0 | 全部通过 |
| `make smoke` | 隔离 SQLite 的离线 Mock 垂直链路 | 0 | `1 passed, 6 deselected` |
| `cd frontend && npm run build` | production bundle | 0 | 构建成功；保留既有约 659 kB 主 chunk warning |
| `cd backend && uv lock --check` | 锁文件一致性 | 0 | 50 packages resolved，锁一致 |
| `docker compose config --quiet` | Compose 静态配置 | 0 | 通过 |
| `git diff --check` | Patch whitespace | 0 | 通过 |
| in-app browser 390/561/681/901/1100/1101/1280px | 多视口真实 UI 回归 | 0 | 五项导航完整；关键页面无根横向溢出；中小屏 Runs 卡片与桌面表格通过 |

## 测试结果

- 通过：后端全量 `442 passed`；前端 9 files / `36 passed`；新增目标用例均通过。
- 失败：中途仅有 1 条旧 CLI 上限断言和 2 个 Ruff 格式提示；均按新集中边界/formatter 修正，最终门禁为零失败。
- Lint/typecheck/build：通过；build 只有既有 Vite 大 chunk warning。
- Smoke/Docker：离线 Smoke 通过；Compose config 通过。本任务未改变队列/Compose，因此未重复昂贵的 Phase 2 八场景故障套件。

## 未运行验证

- 未调用真实 Provider，未运行付费模型准确率或 Provider 对 131072/null 的实际兼容性测试。
- 未重复真实 PostgreSQL/Redis integration 与 Phase 2 Compose 8/8；本切片没有修改数据库 schema、租约、队列或基础设施拓扑，相关历史证据保持原边界。

## 未完成项

- 阶段 commit、普通 push 与精确 SHA GitHub Actions 查询。

## 已知问题与限制

- P2-05 的 Provider 限流、预算、背压和公平调度仍不在本任务范围。

## 安全检查

- 真实密钥扫描：高置信 added-diff/untracked 扫描通过；既有 `test_api.py` 的伪 `sk-` 路径 fixture 经脱敏定位后确认不在新增行；实现和测试只使用固定 marker/MockTransport/stub fetch
- 真实 API 调用：否；只读取本地数据库已存在的公开 Run 证据
- 日志/API 脱敏：既有全量测试继续通过；新增 Adapter 错误消息不包含响应体或 Key
- 危险 Git 操作（force push/reset 等）：无
- 阶段 push：待执行
- 远程 CI：待执行
- 遗留安全风险：见 [SECURITY.md](../SECURITY.md) 的费用与 Provider at-least-once 边界

## 结果与下一步

实现、本地门禁、文档与浏览器回归均已完成；进入阶段 commit/push 与精确 SHA CI 查询。Phase 2 继续保持 `in_progress`，下一工作仍由 [NEXT_TASK.md](../NEXT_TASK.md) 指向 P2-05/P2-06/P2-07。

## 最终 Git 状态

```text
待任务结束时补充
```
