# 2026-08-30 — Run Detail 热力图与实时指标工作日志

> 本日志记录实际发生的工作，不是事后美化的总结。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-30
- 执行者：Codex
- 关联阶段：[Phase 3 — Benchmarks](../phases/PHASE-3-BENCHMARKS.md)
- 关联计划：[2026-08-30-run-progress-heatmap-live-metrics.md](../plans/2026-08-30-run-progress-heatmap-live-metrics.md)
- 关联 ADR：无；新增只读进度投影，不改变持久化/协议/安全决定
- 最终状态：in_progress

## 初始仓库状态

- 当前分支：`codex/complete-evaluation-workflow`，HEAD `a59a706921937924b752466b20f8523349c9de29`，跟踪同名 origin 分支
- `git status --short --branch` 摘要：只有分支行，无未提交改动
- 已有未提交改动：无；本任务从已通过精确 SHA CI 的 Run Detail 指标修复之后继续
- 相关功能与测试现状：Run Detail 每秒并行轮询 Run 与当前 100 条 Responses；只有完成题数运行中变化，其余汇总多在终态聚合；无全题热力图或轻量增量接口
- 环境约束：自动化只用 Mock/Stub；本任务不调用真实 Provider、不修改本地历史数据、不新增依赖或 migration

## 本次目标与背景

用户要求评测展示界面增加逐题热力图，绿色表示通过、红色表示答案错误、黑色表示执行异常、白色表示未执行；鼠标悬停格子可查看 Token、运行时间等信息，并希望准确率等数字在评测过程中动态变化。

## 范围

- 新增不含 prompt/raw/reference/provider metadata 的轻量 Run progress index/block API；固定每 block 512 个 absolute positions。
- Run Detail 增加可 hover/focus 的四态题目矩阵及图例/计数。
- 后端在 progress index 同一读取快照中从已持久化 Response 派生 protocol-v1 live metrics 和 Token/cost 已知覆盖。
- 保留现有详情分页、治理、取消和终态停止轮询。
- 增加后端/前端测试并同步强制文档。

## 非目标

- 不增加题目“执行中”第五种状态，不提供 Provider token 流式进度。
- 不改写 Run/Response/ledger，不增加 migration，不改变评分或精确 Token/cost 语义。
- 不引入 WebSocket/SSE、图表依赖或虚拟列表依赖。
- 不开始 P2-07、不改变 Phase 2/3 整体状态、不合并 PR。

## 验收标准

- [x] 绿/红/黑/白格子与正确计数来自全 Run 计划位置，而非当前详情页。
- [x] hover 与 keyboard focus 均显示题号、状态、Token、延迟等，且状态不只靠颜色。
- [x] 运行中新增 Response 后 score/accuracy/completion/延迟/错题/Token/cost 在下一次 index 轮询更新。
- [x] 每秒只读轻量 index；只 hydrate 非空或 `response_count` 变化的 512 题 block，index→block 并发提交最终收敛且不漏格。
- [x] 非空 block 初始同步完成前显示“同步中”，终态先到时仍追齐全部目标 count；hidden 暂停、visible 恢复同步。
- [x] 现有分页、取消、治理、终态停止轮询和目标历史 Run 展示不回归。
- [ ] API/架构/用户/测试/状态文档一致且本地门禁全绿；远端精确 SHA CI 尚待 commit/push 后执行。

## 假设

- Question.position 为 0-based 稳定槽位；数据库对 Benchmark 内 position 有唯一约束。
- EvaluationResponse 为 Run/Question 唯一追加事实；后续不原地更新已完成 Response。
- 实时指标必须复制 `aggregate_run_evidence` 的现有规则：strict score 以计划题为分母，completion 统计非空 raw，answered accuracy 只统计无 error 的非空 raw。
- 20,000 题对应最多 40 个 index 项；每秒只比较 block counts，不能全量拉取逐题正文。

## 风险

| 风险 | 影响 | 缓解措施 | 结果 |
|---|---|---|---|
| index→block 之间并发提交 | 热力图短暂比 index 更新或少格 | Response 追加计数单调；客户端采纳 block 实际内容并由下轮 index 收敛，reducer 幂等 | 定向自动化通过 |
| 大 Run DOM 性能 | 页面卡顿 | 虚拟化 ARIA grid、事件委托、CSS containment、无正文 payload、只刷新变化 block | 12,032/20,000 题虚拟化自动化通过；实页手工仅验证目标 198 题 Run，不冒充大型 DevTools 性能测量 |
| live/终态公式漂移 | 结果误导 | 后端复用同一证据公式；index 指标与 counts 使用同一读取快照；终态 fixture 对照 | 后端定向与完整回归通过 |
| 颜色不可访问 | 用户无法识别状态 | 中文图例/计数、ARIA label、focus ring 与 Tooltip | 自动化 ARIA/键盘覆盖及实页键盘/Tooltip 通过 |

## 实施步骤

1. [completed] 冻结 API/UI 合同并添加失败测试；初版 cursor red tests 已失败，合同已在实现前切换为 fixed blocks。
2. [completed] 实现后端轻量 progress index/block API 与同快照 live metrics。
3. [completed] 实现虚拟化热力图、Tooltip、独立轮询和实时指标。
4. [in_progress] 文档、本地目标/完整门禁与浏览器验收已完成；剩余 commit/push、最终 SHA 记录和远端精确 SHA CI。

## 实际修改

| 文件/模块 | 修改内容 | 对应需求/原因 |
|---|---|---|
| 本计划与工作日志 | 冻结范围、fixed-block 性能/竞态/协议边界和验收 | AGENTS/PLANS 强制流程 |
| 后端 progress Schema/service/routes、共享证据聚合、日志合同与测试 | 固定 512 block index/payload、同快照 live metrics、typed 边界和固定白名单 | 运行中动态指标与大型 Run 轻量同步 |
| 前端 API types/client、`useRunProgress`、`RunProgressHeatmap`、Run Detail/CSS 与测试 | block reducer/poller、终态追齐、虚拟化 ARIA grid、Tooltip 与响应式布局 | 四态进度和动态指标 UI |
| README/API/TESTING/REQUIREMENTS/ARCHITECTURE/Phase/Roadmap/Status/Changelog/NEXT_TASK | 按实现与实际本地证据同步产品/API/测试/状态边界 | 强制文档同步；最终 SHA/远端 CI 待收尾 |
| `docs/BENCHMARK_PROTOCOL.md` | 修正逐题 Provider transport metadata 已持久化的既有文档漂移 | 与实现、API 和 SECURITY 的既有事实对齐；不改变 protocol-v1 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
|---|---|---|---|
| 10:13 CST | discovery | 当前 Runner 每题只写回 completed count；其余 Run 汇总在终态 `aggregate_run_evidence` | 新 read model 必须从 Response 证据实时派生并由前端展示，不能只轮询旧 Run 汇总字段 |
| 10:16 CST | decision | 新接口只暴露 Tooltip/公式需要的固定字段，不包含题目/回答正文 | 保持轮询轻量且不扩大敏感正文面 |
| 10:18 CST | deviation | 初版 `(created_at,id)` opaque cursor 的 4 个后端 red tests 按预期失败；进一步复核发现应用 `created_at` 与 UUID 都不是数据库单调提交序列，并发提交无法证明绝对不漏 | 在生产实现前废弃 cursor 测试/合同，改用固定 512 absolute-position blocks；无需 migration |
| 10:25 CST | decision | `GET /runs/{id}/progress` 返回同快照 live metrics 与全部 block counts；`GET /runs/{id}/progress/blocks/{block_index}` 返回 absolute-position 固定白名单 cells | 前端只补齐非空/变化 block；12,032/20,000 题分别约 24/40 blocks |
| 10:27 CST | decision | outcome 优先级为 `error_type != null -> error`、否则 `score == 1 -> passed`、否则 `wrong`；没有 Response 的计划 position 为 `not_run` | 执行异常不会被重复算成普通答错，四态计数互斥 |
| 10:29 CST | decision | live 主指标由后端证据聚合并与 block index 使用同一读取快照；前端不从部分 hydrate Map 重算 | 防止“同步中”窗口产生虚假分数；Run 精确 nullable Token/cost 保持不变 |
| 10:32 CST | discovery | 用户 Run `a3de7e4d-40b2-4d8c-994b-c713047393ae` 的 Run/Response 对账为 total/completed/correct/error=`198/198/179/2`，198 条 Response 中 `score < 1` 为 19、`error_type` 非空为 2 | 四态应为通过 179、普通答错 17、执行异常 2、未执行 0；旧页面把执行异常 2 当成全部“错误题”，已复现用户问题 |
| 10:32 CST | discovery | 同一 Run 的已知 input/output Token 小计为 `45,509 / 4,561,625`，两列覆盖均为 `196/198`，平均延迟 `181,454.235 ms`；Run 精确 input/output/cost 均为 `null` | UI 应显示已知小计和覆盖率，不能把两条 usage 缺失解释为 0，也不能回填精确 Run 字段；未记录任何敏感 Response 正文 |
| 本地验收 | validation | 目标 Run 实页显示通过 179、普通答错 17、执行异常 2、未执行 0；Token `45,509 / 4,561,625`、输入/输出覆盖均为 `196/198` | 用户报告的两处展示问题已在真实本地页面闭环 |
| 本地验收 | validation | desktop、768px、375px 无横向溢出，console 无 warning/error，键盘定位与 Tooltip 通过 | 常见本地宽度和关键非鼠标路径已验证；不把它扩大为 VoiceOver/NVDA 认证 |
| 前端终审 | fix/validation | terminal + progress reconciled 后只执行一次最终 Run/当前 evidence 页刷新 | 避免终态 `Promise.all` 交错让较旧证据覆盖最终证据；新增回归通过 |
| 前端终审 | fix/validation | 同一路由切换 `runId` 时把 evidence offset 重置为 0 | 防止从旧 Run 的后续页请求新 Run；新增回归通过 |

## 实际运行命令

| 命令 | 目的 | 退出码 | 结果摘要 |
|---|---|---:|---|
| `git status --short --branch` | 确认初始工作区 | 0 | 分支干净，跟踪 origin 同名分支 |
| `rg`/`sed` 读取 AGENTS、README、状态、Roadmap、Phase、API、Testing、Security、Architecture 与相关代码/测试 | 冻结约束和现状 | 0 | 确认跨 API/UI 需计划；无 migration/ADR；现有 Run 运行中汇总不完整 |
| 后端定向 red tests（初版 cursor 合同） | 失败先行验证接口尚不存在 | 失败（预期） | `4 failed`；因无单调提交序列，已在实现前废弃该合同，随后 fixed-block 定向测试 `37 passed` |
| `git diff --check -- <本切片文档>` | 检查文档补丁空白/冲突 | 0 | 文档先行补丁 clean；最终代码/测试 diff 仍由主任务收尾复核 |
| `cd backend && uv run pytest tests/test_run_progress_api.py tests/test_response_metadata_api.py -q` | progress API/聚合/边界定向回归 | 0 | `37 passed` |
| `cd frontend && npm test -- --run tests/run-detail-page.test.tsx tests/run-progress-heatmap.test.tsx` | 热力图、动态指标与轮询定向回归 | 0 | `32 passed`（Run Detail `20` + heatmap `12`）；含两条终审竞态与 12,032/20,000 题虚拟化自动化边界 |
| `make test` | 完整本地回归 | 0 | backend `964 passed, 33 skipped`；frontend `64 passed` |
| `make lint` | Ruff/format、ESLint、TypeScript | 0 | 通过 |
| `make smoke` | 完全离线 Mock 纵向链路 | 0 | `1 passed, 7 deselected` |
| `cd frontend && npm run build` | production build | 0 | 通过 |
| `docker compose config --quiet` | Compose 配置 | 0 | 通过 |
| 可信 loopback 浏览器验收 | 目标历史 Run、响应式、键盘、Tooltip 与 console | 0 | 179/17/2、Token/覆盖正确；desktop/768/375 无横向溢出；console 无 warning/error |

## 测试结果

- 通过：backend target `37 passed`；frontend target `32 passed`（Run Detail `20` + heatmap `12`）；完整 backend `964 passed, 33 skipped`、frontend `64 passed`
- 失败：初版 cursor 后端 red tests `4 failed`（预期且已废弃）；fixed-block 实现后的目标/完整回归零失败
- Lint/typecheck/build：`make lint` 与 frontend production build 通过
- Smoke/Docker：Mock smoke `1 passed, 7 deselected`；`docker compose config --quiet` 通过

## 未运行验证

- 仅 commit/push 后的远端精确 SHA CI 尚未运行。12,032/20,000 题是自动化虚拟化边界测试；没有把它写成大型真实 Run 的手工 DevTools 性能/内存测量。

## 未完成项

- 本地实现、文档、目标/完整门禁与目标 Run 浏览器验收已完成；仅 commit/push、最终 SHA 记录和远端 exact-SHA CI 待完成。

## 已知问题与限制

- 已持久化 Response 前无法从当前数据模型区分“正在 Provider 执行”与“尚未开始”，二者按用户指定统一显示白色未执行；本任务不新增中间态事实。

## 安全检查

- 真实密钥扫描：本地范围/秘密复核通过；提交前按流程复核 staged diff
- 真实 API 调用：否；计划内仅 Mock/Stub 与本地只读页面
- 日志/API 脱敏：progress 合同禁止 question/external ID、prompt/choices/raw/parsed/reference/error message 与 Provider metadata；只返回 absolute position 和指标白名单
- 危险 Git 操作（force push/reset 等）：无
- 阶段 push：待完成
- 远程 CI：待完成
- 遗留安全风险：与现有 Run 证据相同，仅适合可信 loopback；见 `docs/SECURITY.md`

## 结果与下一步

`in_progress`。本地实现、验证与浏览器闭环已完成；下一步仅为提交、push、记录最终 SHA 并等待该精确 SHA 的远端 CI。Phase 3/P3-06 在远端门禁完成前保持 `in_progress`，P2-07 仍是既定下一可靠性切片。

## 最终 Git 状态

```text
本地实现与验证已完成；工作树待提交，远端精确 SHA CI 待运行。
```
