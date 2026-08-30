# 2026-08-30 — Provider API 三协议适配工作日志

> 本日志记录实际发生的工作，不是事后美化的总结。所有命令以仓库根目录为基准。

## 元信息

- 日期：2026-08-30
- 执行者：Codex
- 关联阶段：[Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- 关联计划：[执行计划](../plans/2026-08-30-provider-api-protocols.md)
- 关联 ADR：[ADR-0019](../decisions/ADR-0019-explicit-provider-api-protocol-adapters.md)
- 最终状态：completed（实现提交已普通 push，exact-SHA CI 四个必需 job 全绿）

## 初始仓库状态

- 当前分支：`codex/complete-evaluation-workflow`，跟踪 `origin/codex/complete-evaluation-workflow`
- `git status --short --branch` 摘要：分支同步，工作区无已有改动
- 已有未提交改动：无
- 相关功能与测试现状：`openai_compatible` 仅实现 Chat Completions；离线定向测试 `2 passed`，明确断言 `/chat/completions` 与 Chat payload
- 环境约束：可联网读取官方文档；自动化不得调用真实 Provider；不重启当前本地 API/Worker/frontend，不迁移其活动数据库

## 本次目标与背景

OpenCode Go 按模型要求 `/chat/completions`、`/responses` 或 `/messages`。用户确认直接修改项目，使三种协议都能在现有可审计评测链路中显式配置和执行。

## 范围

- Adapter 类型、URL/payload/headers/JSON/SSE/usage 归一化
- Model/API/Run snapshot/Runner/CLI/preflight 与 Alembic migration
- Models/New Run UI 与前端类型/测试
- ADR、API、安全、测试、架构、状态及发布文档

## 非目标

- 不真实调用 OpenCode Go，不验证套餐余额、当日模型可用性或真实费用
- 不实现 tools、多模态、自动协议推断或协议 fallback
- 不改变 Benchmark/评分协议

## 验收标准

- [x] 旧 `openai_compatible` Chat 行为与历史数据无回归
- [x] Responses/Messages 普通 JSON 与 typed SSE 都能归一化文本、usage 和 metadata
- [x] 已知错误 endpoint、unsupported seed、Messages null max tokens 在外发前失败
- [x] Model API、Run snapshot、Worker/CLI 与 Web 可显式选择三类 Adapter
- [x] 双方言 migration、完整 lint/test/Mock smoke/build/Compose config 通过
- [x] 普通 push 后精确 SHA GitHub Actions 四个必需 job 全绿

## 假设

- `provider_type` 是 Adapter key，不另增 `api_protocol` 列；由 ADR-0019 固化。
- Responses/Messages 只实现文本评测所需的官方共同子集；上游私有扩展仍不承诺。

## 风险

| 风险 | 影响 | 缓解措施 | 结果 |
|---|---|---|---|
| 协议映射错误 | 请求失败或额外费用 | 精确 MockTransport、无 fallback、有限 retry | 三协议 endpoint/payload/header 与已知错误 suffix 回归通过 |
| 截断流被当成功 | 错误评分证据 | 每协议终止事件与 EOF fail-closed | `[DONE]` / `response.completed` / `message_stop` 与截断流回归通过 |
| 迁移回退丢新配置 | 配置丢失 | populated downgrade guard | 隔离 PostgreSQL 中有新类型时 DDL 前拒绝，清空后可安全往返 |
| Key 经新增 header/错误泄漏 | 凭据泄漏 | 不记录 headers、递归脱敏、假 Key 测试 | 假 Key 反射/错误脱敏与 credential 回归通过 |

## 实施步骤

1. [completed] 建立 ADR/计划/日志和失败先行测试
2. [completed] 实现后端三协议、迁移与执行链联动
3. [completed] 实现前端协议与参数 UX
4. [completed] 文档、完整本地门禁与技术/安全终审
5. [completed] commit、普通 push 与 exact-SHA CI

## 实际修改

| 文件/模块 | 修改内容 | 对应需求/原因 |
|---|---|---|
| `docs/decisions/ADR-0019-*` | 固化显式 Adapter 类型、参数、终止与回滚边界 | 架构/公共 API/迁移变更前置决定 |
| `docs/plans/2026-08-30-provider-api-protocols.md` | 建立可持续执行计划 | 跨数据库/后端/前端复杂任务 |
| `backend/app/adapters/` | 新增 Responses/Messages Adapter，保留 Chat 默认；实现 endpoint、JSON/SSE、usage、typed retry 与参数 fail-fast | 三协议执行核心 |
| `backend/app/models/`、`schemas/`、`api/v1/`、`runners/`、`services/` | 扩展显式类型并冻结到 Run snapshot | 公共 API 与可靠执行链一致 |
| `backend/app/providers/`、`cli/evaluate.py` | `/models` 推导、协议鉴权、Messages bounded pagination 和按显式协议执行 canary | 可信本地正式入口不再绑定 Chat |
| `backend/alembic/versions/20260830_0008_provider_api_protocols.py` | `provider_type` `VARCHAR(17)→18` 并替换 Provider 类型/远程配置两个 check；有新类型时 downgrade fail closed | 历史 Chat 数据兼容与可审计回滚 |
| `frontend/src/`、`frontend/tests/` | Models 显式协议选择、New Run sampling/seed/max token 边界及回归 | Web 可配置且不静默丢参数 |
| README 与 `docs/` | 更新 API、架构、安全、测试、部署、状态和运维合同 | 用户/运维说明与实现一致 |

## 决定、偏差与发现

| 时间 | 类型 | 事实与理由 | 后续影响 |
|---|---|---|---|
| 2026-08-30 Asia/Shanghai | discovery | OpenCode Go 当前模型分属三种 endpoint；现有 Adapter 只识别 Chat | 需要独立 payload/parser，不能只改 URL |
| 2026-08-30 Asia/Shanghai | decision | 扩展 Adapter `provider_type`，旧值继续表示 Chat | 保持旧 API/DB/Run snapshot 兼容 |
| 2026-08-30 Asia/Shanghai | discovery | 前端失败先行用例暴露缺少 Chat Completions 选择项及新协议 seed 边界 | 增加三项显式选择并在非 Chat 时禁用/清空 seed |
| 2026-08-30 Asia/Shanghai | discovery | canary 直接转发 `max_tokens=null` 会形成无界探测或 Messages 配置错误 | 新协议 canary 固定为最小有限 16；正式 Messages null 在外发前拒绝 |
| 2026-08-30 Asia/Shanghai | review | 只按 HTTP 状态不足以表达 Responses/Messages SSE transient error | 增加协议 typed transient 白名单；未知流错误保持 fail closed，每次重试独立 ledger 结算 |
| 2026-08-30 Asia/Shanghai | review | Responses/Messages 的隐式 Chat sampling default 会让部分模型在请求解析阶段拒绝；Messages discovery 也不接受 Bearer-only 假设 | 请求/Model 默认都省略时冻结 `temperature/top_p/seed=null`；Messages 另限制 `temperature<=1`，discovery 按协议鉴权并跟随有界 `after_id` 分页 |
| 2026-08-30 Asia/Shanghai | review | 多页 Messages discovery 和解析异常仍需独立资源/秘密边界 | `after_id` 聚合限制为 100 页/60 秒/2 MiB/10k entries 并拒绝重复 cursor；malformed JSON/SSE、oversized 与 transport 错误均不链回原始 Provider 内容 |

## 实际运行命令

| 命令 | 目的 | 退出码 | 结果摘要 |
|---|---|---:|---|
| `cd backend && uv run pytest -q tests/test_adapters.py::test_openai_compatible_sends_chat_completion_fields tests/test_provider_preflight.py::test_chat_canary_uses_run_fields_and_requires_parseable_a` | 确认现有 Chat 合同 | 0 | `2 passed`；仅 MockTransport |
| `cd frontend && npm test -- --run tests/models-page.test.tsx tests/new-run-page.test.tsx`（失败先行） | 固化 Web 协议选择/参数边界 | 1 | `2 failed, 12 passed`；缺少 Chat 选项和 seed 行为，符合预期红灯 |
| `cd backend && uv run pytest -q tests/test_provider_protocol_adapters.py tests/test_provider_protocol_plumbing.py tests/test_provider_preflight.py tests/test_evaluation_cli.py tests/test_api.py tests/test_migrations.py tests/test_web_credentials.py tests/test_evaluation_runner_reliability.py` | 三协议执行、API、迁移、Runner、CLI 合并目标回归 | 0 | 全部通过；仅 MockTransport/本地数据库 |
| `make lint`（首次） | 静态门禁 | 1 | 仅 7 个本次 Python 文件需要 Ruff format；随后机械格式化并重跑通过 |
| `make lint`（最终） | Ruff/format、ESLint、TypeScript | 0 | 全部通过；164 个 Python 文件 format clean |
| `make test` | 完整后端/前端回归 | 0 | backend `1079 passed, 36 skipped`；frontend `72 passed` |
| `make smoke` | 离线垂直链路 | 0 | `1 passed, 7 deselected`；Mock-only |
| `cd frontend && npm run build` | 生产前端构建 | 0 | 通过；保留既有大 chunk warning |
| `docker compose config --quiet` | Compose 静态配置 | 0 | 通过 |
| 高置信 staged-candidate 秘密扫描 | 检查本次 modified/untracked 文件中的真实 Key/私钥格式 | 1（无匹配） | 仅更宽的初筛命中明确命名的测试 canary/secret marker；高置信格式无匹配 |
| 隔离 PostgreSQL 16：preflight、`upgrade head`、`check`、populated downgrade、清空后 downgrade/upgrade/check | 验证真实 PostgreSQL `0008` 约束与回滚门禁 | 0/预期拒绝/0 | 新类型存在时 downgrade 以 RuntimeError 在 DDL 前拒绝；清空两条测试 Model 后往返与 check 通过；测试容器已停止并删除 |
| `git commit`、普通 `git push`；`gh run watch 33304667092 --exit-status` | 发布实现并验证精确 SHA | 0 | 实现 SHA `6943aa29a154c82bdfbe5efb2578c916c3cbf632` 已 push；backend、backend integration、real-Compose reliability、frontend 四个必需 job 全部成功 |

## 测试结果

- 通过：三协议目标回归、完整 backend/frontend、Mock smoke、build、Compose config 与隔离 PostgreSQL 16 migration 门禁均通过。
- 失败并已修复：前端失败先行 `2 failed, 12 passed`；首次 `make lint` 仅要求 7 文件格式化。
- 已知 warning：Python 3.14 上游弃用/async warning 与既有 Vite large-chunk warning；无新增失败。

## 未运行验证

- 真实 Provider：按安全规则有意不运行。

## 未完成项

- 无；证据文档作为独立收尾提交，提交后同样接受 exact-SHA CI 门禁。

## 已知问题与限制

- Responses/Messages 只实现纯文本评测共同子集，不含 tools、多模态或供应商私有扩展。
- 本地自动化不能证明 OpenCode Go 当日模型、额度或真实账单兼容性。

## 安全检查

- 真实密钥扫描：已对本次 modified/untracked 文件执行高置信格式扫描，无匹配；较宽初筛只命中明确命名的假 canary/secret marker 测试值
- 真实 API 调用：否
- 日志/API 脱敏：三协议 malformed JSON/SSE、oversized、transport 与反射假 Key 回归通过，安全异常不保留原始 Provider cause/context
- 危险 Git 操作（force push/reset 等）：无
- 阶段 push：实现 SHA `6943aa29a154c82bdfbe5efb2578c916c3cbf632` 已普通 push
- 远程 CI：[run `33304667092`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33304667092) 对该实现 SHA 4/4 成功
- 遗留安全风险：自定义 HTTPS `base_url` 的 SSRF/数据外发风险不变

## 结果与下一步

三协议实现、本地门禁、隔离 PostgreSQL 迁移、最终审查、普通 push 与实现 exact-SHA CI 全部完成。下一独立任务恢复为 P2-07 最小只读 recovery verifier。

## 最终 Git 状态

```text
实现提交后工作树干净；本次仅追加独立 evidence closeout 文档提交
```
