# Provider API 三协议适配执行计划

- Owner: Codex
- Status: completed
- Created: 2026-08-30
- Updated: 2026-08-30
- Related phase: [Phase 2 — Reliability](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [工作日志](../worklogs/2026-08-30-provider-api-protocols.md)
- ADRs: [ADR-0019](../decisions/ADR-0019-explicit-provider-api-protocol-adapters.md)

## Context

当前 `openai_compatible` Adapter 只实现 Chat Completions。OpenCode Go 按模型分别要求 `/chat/completions`、`/responses` 或 `/messages`，完整的后两类地址会被现有 URL 拼接器继续追加 `/chat/completions`。本任务必须在不改变旧 Chat Run 的前提下，为公共 Model API、Worker/CLI、Run snapshot 与 Web 表单增加显式协议能力。

## Objective

用户可显式注册并离线验证 Chat Completions、OpenAI Responses、Anthropic Messages 三类远程 Model；每类请求使用正确 endpoint/payload/headers/JSON/SSE parser，并保留既有安全、治理、恢复和证据合同。

## Scope

- 扩展 Provider/Adapter 类型和数据库 check constraint，提供向前 migration 与受保护 downgrade。
- 实现 Responses/Messages JSON 与 typed SSE、usage/metadata 归一化、URL/参数校验。
- 让 Adapter registry、Runner、可信本地 CLI/preflight、Run snapshot 与 API CRUD 支持新类型。
- 更新 Models/New Run UI、前端类型、组件测试、API/架构/安全/测试/状态文档。

## Non-goals

- 不调用真实 OpenCode Go 或其他付费 Provider。
- 不实现自动模型名→协议映射、跨协议 fallback、工具调用、多模态或完整供应商私有扩展。
- 不改变题目 prompt、Evaluator、评分分母或 `llmbenchlab-protocol-v1`。

## Assumptions

- `provider_type` 是现有 Adapter registry key；扩展它比增加重复的协议列更符合当前模型。
- OpenCode Go 文档中的 `/responses` 与 `/messages` 分别遵循对应官方协议的文本生成共同子集；通过 MockTransport 固化这个子集。
- 当前工作区起点干净，运行中的本地服务不重启、不迁移其正在使用的数据库。

## Requirements

- FR-MOD-02～11：显式 Adapter、配置校验、凭据、重试、usage 和参数合同。
- FR-RUN-02：单题隔离、稳定错误与恢复。
- FR-REP-01～04：Run 快照与可比性不漂移。
- NFR-SEC-01～05：无真实 Key、无真实 Provider、HTTPS/loopback、脱敏和有界响应。

## Implementation steps

1. [completed] 建立失败先行协议/迁移/API/UI 测试与 ADR
   - Files/modules: `backend/tests/`, `frontend/tests/`, `docs/decisions/ADR-0019*`
   - Validation: 新测试在旧实现上因缺少新类型/Adapter/控件而失败。
2. [completed] 实现后端三协议与持久化/API/Runner/CLI 联动
   - Files/modules: `backend/app/adapters/`, `models/`, `schemas/`, `runners/`, `providers/`, `cli/`, Alembic
   - Validation: 定向 Adapter/API/migration/Runner/CLI 测试通过。
3. [completed] 实现前端协议选择与参数边界
   - Files/modules: `frontend/src/api/`, `frontend/src/pages/`, `frontend/tests/`
   - Validation: Models/New Run 组件测试、ESLint、TypeScript 通过。
4. [completed] 文档与完整本地门禁
   - Files/modules: README、API、ARCHITECTURE、SECURITY、TESTING、状态/阶段/Changelog/Next Task/工作日志
   - Validation: lint、完整 test、migration、Mock smoke、frontend build、Compose config、diff/秘密检查通过。
5. [completed] 提交、push 与精确 SHA CI
   - Files/modules: Git/Actions
   - Validation: 普通 push；该精确 commit 的四个必需 job 全部成功。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| 协议字段映射错误导致付费 4xx/重复调用 | 中/高 | 无 fallback；MockTransport 精确断言；有限 retry 只重试既有可重试分类 | 稳定失败并保留单题错误，不切换协议 |
| 新解析器接受截断流 | 中/高 | 每协议独立终止事件和 EOF 失败测试 | 丢弃部分内容，记录 `incomplete_provider_stream` |
| 迁移破坏旧 Model | 低/高 | 精确 `VARCHAR(17)→18` 列宽变更并替换 Provider 类型/远程配置两个 check；旧值逐行保持；双方言往返 | downgrade 对新类型 fail closed，否则精确恢复列宽与两个旧 check |
| 新 header/错误泄漏 Key | 低/高 | 复用 SecretStr、禁日志、递归脱敏与假 Key 回显测试 | 测试失败即不提交 |
| 参数在协议间静默丢失 | 中/中 | 新协议默认省略未配置的采样字段；unsupported seed、Messages `temperature>1`/nullable max tokens 显式拒绝并在 UI 解释 | 返回稳定配置错误，不发网络 |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| Adapter 三协议 | `cd backend && uv run pytest -q tests/test_provider_protocol_adapters.py tests/test_provider_protocol_plumbing.py ...` | 全部 MockTransport 用例通过 | 通过；合并目标套件零失败 |
| API/迁移/Runner/CLI | 同一后端目标套件及隔离 PostgreSQL 16 往返 | 新类型持久化、快照与执行通过 | 通过；`0008` upgrade/check、populated downgrade 拒绝、清空后 downgrade/upgrade/check 均符合合同 |
| 前端 | `cd frontend && npm test` | 协议选择和参数 UX 通过 | `72 passed` |
| 完整门禁 | `make lint && make test && make smoke` | 零失败、无真实 Provider | 通过；backend `1079 passed, 36 skipped`，frontend `72 passed`，Mock smoke `1 passed, 7 deselected` |
| 构建/部署静态检查 | `cd frontend && npm run build`; `docker compose config --quiet` | exit 0 | 均为 exit 0；保留既有 Vite chunk warning |
| 远程门禁 | GitHub Actions exact SHA | 4/4 required jobs success | 实现 SHA [`6943aa29a154c82bdfbe5efb2578c916c3cbf632`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/commit/6943aa29a154c82bdfbe5efb2578c916c3cbf632) 已普通 push；[run `33304667092`](https://github.com/CWNU-Open-Source-Community/LLMBenchLab/actions/runs/33304667092) 四个必需 job 全部成功 |

## Rollback

停止 API/Worker 后回退应用与 migration。若数据库存在新类型 Model，downgrade 先拒绝；操作者必须先在新应用中确认无 active Run，并显式删除或转换这些 Model。旧 Chat Model/Run/Response 不重写、不删除。

## Documentation updates

- [x] README / 用户操作说明
- [x] API / Requirements
- [x] Architecture / Security / ADR
- [x] Testing / migration / CLI 说明
- [x] CHANGELOG、PROJECT_STATUS、阶段文档、NEXT_TASK、工作日志

## Completion evidence

- Changed files: Adapter、Model/API/Runner/CLI/preflight、`0008` migration、Models/New Run UI、自动化测试与合同/运维文档
- Commands run: 目标 pytest、`make lint`、`make test`、`make smoke`、frontend build、Compose config、隔离 PostgreSQL 16 migration 往返
- Acceptance evidence: 本地门禁全绿；实现 SHA `6943aa29a154c82bdfbe5efb2578c916c3cbf632` 的 exact-SHA Actions run `33304667092` 4/4 全绿
- Not run: 真实 Provider（有意不运行）
- Known issues: Responses/Messages 仅覆盖纯文本评测共同子集；未对真实 OpenCode Go 当日模型、额度或账单做自动化验证

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-30 | decision | 用三个显式 `provider_type` Adapter 值，不新增重复协议列 | 旧 `openai_compatible` 保持 Chat 默认；migration 扩展列宽并替换 Provider 类型/远程配置两个 check |
| 2026-08-30 | decision | 不做 URL/模型名自动协议推断或失败 fallback | 防止不透明的重复付费请求，Run 快照可复现 |
| 2026-08-30 | discovery | 新协议 canary 的 `max_tokens=null` 不能直接进入有限输出请求 | canary 归一化为 16；正式 Messages 请求仍要求有限非空值 |
| 2026-08-30 | decision | 只对白名单 typed transient error 重试，未知 SSE error fail closed | Responses 与 Messages 保留既有逐 attempt ledger 语义且不扩大重试面 |
| 2026-08-30 | review | Responses/Messages 模型可能拒绝隐式 Chat sampling default；discovery 的 Messages 协议也不使用 Bearer | 请求/Model 默认都省略时冻结 `temperature/top_p/seed=null`；discovery 按显式协议鉴权并有界分页 |
