# 2026-08-30 — 修复 Run Detail 错题与部分 Token 展示工作日志

> 本日志记录实际发生的工作，不是事后美化的总结。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-30
- 执行者：Codex
- 关联阶段：[Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)、[Phase 3 — Benchmarks](../phases/PHASE-3-BENCHMARKS.md)
- 关联计划：[2026-08-30-fix-run-detail-metrics.md](../plans/2026-08-30-fix-run-detail-metrics.md)
- 关联 ADR：无；不改变既有协议/架构决定
- 最终状态：completed

## 初始仓库状态

- 当前分支：`codex/complete-evaluation-workflow`，HEAD `bbf6e87`，跟踪 `origin/codex/complete-evaluation-workflow`
- `git status --short --branch` 摘要：只有分支行，无未提交改动
- 已有未提交改动：无
- 相关功能与测试现状：Run Detail 直接把 `error_questions` 标为“错误题”；Response 列表 API 只有逐题 nullable usage，没有全量已知小计/覆盖率；Smoke 固化精确 Run Token 任一缺失即为 `null`
- 环境约束：本地 SQLite/API 可只读核查；自动测试禁止真实 Provider；本任务不修改现有数据库

## 本次目标与背景

用户指出 Run `a3de7e4d-40b2-4d8c-994b-c713047393ae` 的错误题数量与 Token 展示不正确。只读核查确认页面把 2 条执行异常误导性地呈现为全部错题，而 196 条已知 usage 因另外 2 条缺失而被精确总量的 `null` 完全遮蔽。本任务修复信息表达并保留协议完整性。

## 范围

- Response 列表 API 增加与分页无关的已知 Token 小计和 usage 报告数。
- Run Detail 显示未得分、普通答错、执行异常和正确数。
- Run Detail 在精确 Token 缺失时展示已知小计、覆盖率与完整总量未知提示。
- 更新自动化测试和相关 API/测试/状态文档。

## 非目标

- 不回填、估算或修改历史 usage、Response、ledger、成绩或数据库 schema。
- 不改变 Run 精确 Token、成本、排行榜或 Dashboard 的现有协议语义。
- 不调用真实 Provider，不检查或记录凭据。

## 验收标准

- [x] 目标口径能表达正确 179、普通答错 17、执行异常 2、未得分 19。
- [x] 部分 usage 能表达已知输入 45,509、输出 4,561,625、196/198 已报告且完整总量未知。
- [x] 全 usage、部分 usage、零 Response 与分页场景有后端/前端回归。
- [x] API/测试/状态文档与实现一致，既有 protocol-v1 精确聚合不变。
- [x] 目标测试、lint、完整 test、smoke、frontend build 和 Compose config 有真实结果。

## 假设

- `completed_questions - correct_questions` 表示已持久化 Response 中未得分题数；依据为 protocol-v1 单题二元评分。
- 已知 Token 小计只作为 Response 证据下界展示；Provider retry、异常调用和账单真值不由该小计覆盖。
- responses API 的 aggregate 不应用 offset/limit，因此跨页返回相同全量 summary。

## 风险

| 风险 | 影响 | 缓解措施 | 结果 |
|---|---|---|---|
| 部分 Token 被误当精确总量 | 费用/规模判断失真 | 明确标“已知小计”并同时显示覆盖率与“完整总量未知” | 目标实页与组件测试确认 |
| 错题与异常继续混淆 | 用户无法核对成绩 | 主指标使用未得分，辅助拆分普通答错/执行异常 | 目标实页显示 19/17/2 |
| API 新字段影响前端通用类型 | 类型或调用回归 | responses 使用专用 response 类型，字段仅追加 | typecheck、全量测试与 build 通过 |

## 实施步骤

1. [completed] 冻结 API/UI 语义并添加失败回归。
2. [completed] 实现后端只读聚合和前端展示。
3. [completed] 更新文档并完成本地/远程门禁。

## 实际修改

| 文件/模块 | 修改内容 | 对应需求/原因 |
|---|---|---|
| `docs/plans/2026-08-30-fix-run-detail-metrics.md` | 建立跨后端/API/前端执行计划 | AGENTS/PLANS 强制流程 |
| 本工作日志 | 冻结目标、范围、风险和验收 | AGENTS 强制流程 |
| `backend/app/schemas/evaluation_response.py` | 为 Responses 列表增加四个非负、带说明的 Run-wide usage summary 字段 | 公共 API 明确部分 usage 证据 |
| `backend/app/api/v1/runs.py` | 用一次聚合查询同时返回总数、输入/输出已知小计与独立上报数 | 不增加查询次数且不受分页影响 |
| `backend/tests/test_response_metadata_api.py`、`backend/tests/test_smoke.py` | 覆盖 OpenAPI、零/全/部分/非对称 usage、合法零 Token 与分页 | 防止字段漂移或部分小计回填 Run 精确值 |
| `frontend/src/api/types.ts`、`frontend/src/api/client.ts` | 为 Responses 定义专用列表类型 | 不污染通用 `ListResponse` |
| `frontend/src/pages/RunDetailPage.tsx` | 显示未得分/普通答错/执行异常；显示精确或明确不完整的 Token 小计 | 修复用户可见误导并处理并行快照竞态 |
| `frontend/tests/run-detail-page.test.tsx` | 覆盖目标 179/17/2、页内拆分、精确/部分/零/非对称 Token、快照不一致和分页 | 固化可见文案与保守语义 |
| `README.md`、`docs/API.md`、`docs/TESTING.md`、`CHANGELOG.md` | 同步用户、API、测试与变更说明 | 文档与实现一致 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
|---|---|---|---|
| 09:31 CST | discovery | 198 条 Response 中 179 正确、17 普通答错、2 异常；196 条有 usage | 修复展示而不改成绩事实 |
| 09:31 CST | decision | 追加分页无关的 usage evidence summary，保留 Run 精确 Token `null` | 公共 API 只做向后兼容字段扩充，需更新 API 文档和测试 |
| 09:40 CST | test | 失败先行回归按预期失败：后端缺少 summary 字段，前端仍显示旧“错误题”与破折号 | 证明测试能捕获原缺陷后才实施 |
| 09:43 CST | discovery | Run 与 Responses 并行请求可能来自相邻快照；两个边际 reported count 相等不等于同题完整 usage | 精确 Token 需题数、全覆盖、小计一致；部分文案分别说明输入/输出覆盖 |

## 实际运行命令

| 命令 | 目的 | 退出码 | 结果摘要 |
|---|---|---:|---|
| `git status --short --branch` | 确认初始工作区 | 0 | 分支干净，无未提交改动 |
| `git rev-parse --show-toplevel` / `git log -1 --oneline --decorate` | 确认仓库与 HEAD | 0 | 仓库根为当前目录，HEAD `bbf6e87` |
| `cd backend && uv run pytest tests/test_response_metadata_api.py -q`（实现前） | 验证失败回归 | 1 | `1 passed, 1 failed`；新增 summary KeyError，符合预期 |
| `cd frontend && npm test -- --run tests/run-detail-page.test.tsx`（实现前） | 验证失败回归 | 1 | `5 passed, 4 failed`；旧标题/Token/页头不满足新要求 |
| `cd backend && uv run pytest tests/test_response_metadata_api.py tests/test_smoke.py -q` | 目标 API/纵向回归 | 0 | `11 passed`；仅有既有上游弃用 warning |
| `cd frontend && npm test -- --run tests/run-detail-page.test.tsx tests/format.test.ts` | 目标 UI/格式回归 | 0 | `20 passed` |
| `git diff --check && make lint`（首次） | 格式/静态门禁 | 2 | 逻辑 lint 通过，仅两个 Python 测试文件需 Ruff format；如实保留 |
| `cd backend && uv run ruff format tests/test_response_metadata_api.py tests/test_smoke.py && cd .. && make lint` | 修正并重跑静态门禁 | 0 | Ruff/format、ESLint、TypeScript 全绿 |
| `make test` | 完整自动化回归 | 0 | backend `951 passed, 33 skipped`；frontend `47 passed`；仅既有上游弃用 warning |
| `make smoke` | 离线纵向验收 | 0 | `1 passed, 7 deselected`，只用 Mock |
| `cd frontend && npm run build` | production build | 0 | 2192 modules 成功；保留既有 663.81 kB chunk warning |
| `docker compose config --quiet` | 部署配置解析 | 0 | 无输出，配置有效 |
| 本地 API 读取目标 Run Responses summary | 真实目标记录只读验真 | 0 | total 198；known input/output `45,509/4,561,625`；两侧 `196/198` |
| Browser 打开目标 Run Detail、翻到第二页并检查 console | 实页视觉/交互验收 | 0 | 首屏 19/17/2、已知小计 460.7万；第二页 8 未得分/2 执行异常；console error 0 |
| `git diff --check` + added diff/untracked 高置信 secret scan | 最终范围、空白与秘密复核 | 0 | 无 whitespace error、无 Key/Bearer/private-key 命中；18 个候选文件均在计划范围 |
| `git commit -m "fix: clarify run detail result metrics"` / `git push origin codex/complete-evaluation-workflow` | 形成并发布实现阶段 | 0 | commit `0003e4291769a851005ba46c7e59b156a6b789eb`；远端分支与本地一致 |
| `gh pr create ...` | 触发分支远程门禁 | 0 | [PR #5](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/pull/5) 已创建；未执行合并 |
| `gh run watch 33286730109 --exit-status` | 等待实现精确 SHA CI | 0 | backend、真实 PostgreSQL/Redis integration、real-Compose acceptance、frontend 4/4 success |

## 测试结果

- 通过：后端目标 `11 passed`；前端目标 `20 passed`；完整 backend `951 passed, 33 skipped`、frontend `47 passed`；lint/build/smoke/config/实页验收全绿
- 失败：实现前失败回归分别为后端 `1 failed`、前端 `4 failed`，均在实现后转绿
- Lint/typecheck/build：全绿；build 只保留既有大 chunk warning
- Smoke/Docker：离线 Smoke 与 Compose config 通过

## 未运行验证

- 真实 Provider 未运行（有意）：没有 API Key，也不需要产生模型费用。本地没有另跑真实 PostgreSQL/Redis integration 或 real-Compose acceptance；实现精确 SHA 的远程 CI 已实际运行这两项并通过。

## 未完成项

- 无功能、测试或远程门禁未完成项。PR #5 保持打开；合并不在本次授权范围，也不是本阶段 commit/push/exact-SHA CI 的完成条件。

## 已知问题与限制

- 两条历史 Provider 异常没有 usage，无法恢复精确完整 Token；只能展示已知小计与覆盖率。

## 安全检查

- 真实密钥扫描：added diff/untracked 高置信模式无命中；实现/API/浏览器核对未读取或记录凭据正文
- 真实 Provider API 调用：否；只读取本地 API 的目标 Run 汇总
- 日志/API 脱敏：本任务不新增正文或 Provider-controlled 聚合字段
- 危险 Git 操作（force push/reset 等）：无
- 阶段 push：实现 commit `0003e429…` 已普通 push；无 force push
- 远程 CI：[run `33286730109`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33286730109) 对实现精确 SHA 4/4 success；只有 Node.js 20 action deprecation annotation，无失败
- 遗留安全风险：已知 Token 小计不是 Provider 账单真值，必须保持明确文案

## 结果与下一步

本维护为 `completed`：实现、文档、本地门禁、普通 push 和实现精确 SHA CI 均已闭环。下一独立任务仍是 `docs/NEXT_TASK.md` 中的 P2-07 最小只读 recovery verifier；本次不合并 PR、不开始 P2-07。

## 最终 Git 状态

```text
实现 commit/push 后 clean；证据收尾文档将形成独立 commit，最终状态在交付回复中复核。
```
