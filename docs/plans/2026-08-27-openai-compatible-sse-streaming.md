# OpenAI-compatible SSE 流式生成修复计划

- Owner: Codex
- Status: active
- Created: 2026-08-27
- Updated: 2026-08-27
- Related requirements: FR-MOD-11、FR-RUN-02、FR-RUN-10、NFR-REL-02、NFR-SEC-02、NFR-REP-02
- Related phase: [Phase 2](../phases/PHASE-2-RELIABILITY.md)
- Worklog: [2026-08-27-openai-compatible-sse-streaming.md](../worklogs/2026-08-27-openai-compatible-sse-streaming.md)
- ADRs: [ADR-0008](../decisions/ADR-0008-openai-compatible-sse-transport.md)；部分取代 ADR-0006 的统一 Chat 成功体上限，并保留 protocol-v1 已冻结的有限 transport 重试语义

## Context

GPQA 的本地 Qwen Run 已显式使用 `max_tokens=8192` 和 `read_timeout_seconds=600`，但上游代理仍在约 126 秒返回 HTTP 524；llama.cpp 同期把请求记为客户端关闭的 499。成功题在约 102–103 秒完成，失败题按约 126 秒周期出现。结合 Cloudflare 当前默认 125 秒 Proxy Read Timeout、响应链路 headers 与用户的 llama.cpp 日志，最一致的推断是代理先终止了长时间无响应字节的非流式请求，而不是 LLMBenchLab 的 600 秒 read timeout 或 llama.cpp 崩溃；修复后的真实 Provider 链路尚未验证。当前 Adapter 用 `httpx.AsyncClient.stream()` 读取响应体，却没有在 Chat Completions payload 中发送 `stream:true`，所以这只是流式下载最终响应。

## Objective

让 OpenAI-compatible Adapter 真正请求并持续消费 SSE token 流，在保持响应大小、压缩、秘密脱敏和错误分类边界的前提下聚合最终文本、usage 与 Provider 元数据；兼容忽略 `stream:true` 而返回普通 JSON 的 Provider。

## Scope

- `OpenAICompatibleAdapter` 的 Chat 请求、SSE 增量解析、JSON fallback、超时/重试、资源上限与错误映射。
- Adapter/preflight/Web stored-key Worker/report 的纯离线 MockTransport 回归。
- README、API、协议、架构、安全、部署、测试、Roadmap、Phase 2、状态、下一任务、CHANGELOG、ADR 与工作日志。

## Assumptions

- 目标 llama.cpp 版本实现当前 OpenAI-compatible `stream:true`、usage 尾块、comment ping 与 `[DONE]`；已用官方源码核对，但未由本任务调用用户真实服务。
- Provider/代理需要实际保留 `text/event-stream` 并 flush；若中间层 buffering 或设置绝对总时长，应用真 SSE 仍可能失败。
- `read_timeout_seconds` 沿用 HTTPX “等待下一数据块”的空闲语义；Runner 没有额外整题 wall-clock timeout。
- protocol-v1 的 transport 有限重试保持不变；断流可能重复上游计算/费用，不提供 exactly-once。

## Requirements

- [x] R1：Chat Completions 请求显式发送 `stream:true`，并请求标准流式 usage 统计。
- [x] R2：响应到达后持续读取 SSE 字节，支持任意网络分块、keepalive/comment、可选 usage-only 事件和 `[DONE]`。
- [x] R3：聚合 `delta.content`、finish reason、request ID、returned model、system fingerprint 与 usage，结果契约不变。
- [x] R4：正常 JSON fallback、HTTP 错误重试、压缩拒绝、响应大小上限和 Key 精确脱敏继续成立。
- [x] R5：畸形、错误或未完整结束的 SSE 不得被当作有效答案；长度截断仍映射为 `output_truncated`。
- [x] R6：MockTransport/自定义异步字节流覆盖持续消费、分块、usage、断流、错误、大小边界和全 Worker 链路；不调用真实 Provider。
- [x] R7：用户文档说明 `read_timeout_seconds` 是相邻读取/空闲边界，SSE 可避免无首字节的代理空闲超时，但不能绕过 Provider/代理的绝对总时长上限。

## Non-goals

- 不自动调用或重跑用户的真实模型，不删除或取消现有 Run。
- 不承诺 SSE 能绕过所有 CDN/Gateway 的绝对请求时长限制或 buffering 配置。
- 不实现 Provider 全局 RPM/TPM/费用预算、断点续传或 exactly-once 计费。
- 不改变 `llmbenchlab-protocol-v1` 的评分与 API 默认生成参数。

## Implementation steps

1. [completed] 核对本地 Run/Response 与上游 499/524 时间线，审查 SSE 兼容格式。
   - Files/modules: 本地 SQLite 只读证据、Adapter、llama.cpp/HTTPX/Caddy/Cloudflare 官方文档。
   - Validation: 已确认 Run 是 `read=600`、失败约 126 秒，旧 payload 无 `stream:true`。
2. [completed] 在 Adapter 中发送流式请求并实现有界 SSE 聚合与 JSON fallback。
   - Files/modules: `backend/app/adapters/openai_compatible.py`。
   - Validation: 真 SSE payload、严格 `[DONE]`、独立 wire/event/content 上限和稳定错误已由目标测试覆盖。
3. [completed] 增加 Adapter、preflight 与 Worker 纵向测试，覆盖错误/脱敏/大小/持续消费。
   - Files/modules: `backend/tests/test_adapters.py`、`test_provider_preflight.py`、`test_web_credentials.py`。
   - Validation: Adapter 50 passed；最终全量后端 453 passed / 6 skipped，前端 36 passed。
4. [completed] 更新 README、API、协议、架构、安全、部署、测试、Roadmap、状态、阶段、CHANGELOG、ADR 和工作日志。
   - Files/modules: 权威文档、ADR-0006/0008、本计划与工作日志。
   - Validation: 当前文档明确 Mock-only 证据、空闲 timeout、可选 usage、代理边界和资源上限。
5. [in_progress] 完成 staged 审查、commit、普通 push 与精确 SHA Actions/PR 查询。
   - Files/modules: 全部本任务变更与远程工作分支。
   - Validation: 功能提交 `af345af1048eeddffd784fdca1da419df95da7e2` 已 push；该精确 SHA 的 Actions 与分支 PR 查询均为 `[]`，因 workflow 仅监听 PR/main 而未触发。证据文档提交/查询仍在收尾，远程绿色前计划保持 active。

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| Provider 不实现标准 SSE 或忽略 `stream:true` | 中/中 | 普通 JSON fallback；严格稳定 stream 错误 | 按 Provider 文档调整 endpoint/version，不把部分流计分 |
| `stream_options.include_usage` 支持不一致 | 中/低 | usage 允许缺失；以当前 llama.cpp 官方 schema 验证 | 明确 4xx 证据；未来兼容变更另行计划 |
| SSE 事件开销扩大响应体 | 低/高 | wire 64 MiB、event 1 MiB、content 4 MiB 三层上限 | 中止并记录 `provider_response_too_large` |
| 连接在部分输出后断开 | 中/中 | 不保存部分文本；文档明示 protocol-v1 有限重试 | 重试可能重复计算/费用；最终失败保留稳定错误 |
| 代理缓冲或绝对时限 | 中/高 | 文档要求全链路 Content-Type/flush/timeout 核对 | 在 Provider/Gateway 侧调整；应用不声称已绕过 |
| 非法 Unicode 绕过错误契约 | 低/中 | 严格 UTF-8/Unicode 验证与 lone-surrogate 回归 | 映射为 `invalid_provider_stream` |

## Validation

| 验收项 | 命令/检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| Adapter 目标测试 | `cd backend && uv run pytest tests/test_adapters.py -q` | SSE/JSON/错误/脱敏全部通过 | 50 passed |
| 纵向与全量测试 | `make test` | snapshot/Worker/result 贯通，前后端零失败 | 后端 453 passed / 6 skipped；前端 36 passed |
| Lint/type/format | `make lint` | 全部退出 0 | 通过；Ruff 109 files formatted，ESLint/typecheck 通过 |
| 离线 Smoke | `make smoke` | Mock 纵向链路通过 | 1 passed / 6 deselected |
| 前端 production build | `cd frontend && npm run build` | build 成功 | 通过；仅既有约 660 kB chunk warning |
| 数据库/静态门禁 | Alembic upgrade/check、lock、Compose config、diff、secret scan | 全部通过 | 全部退出 0；无 migration drift/高置信秘密命中 |
| 交付门禁 | commit、普通 push、精确 SHA Actions/PR 查询 | 状态如实记录，不把未触发当绿色 | 功能 SHA `af345af1048eeddffd784fdca1da419df95da7e2` 已 push；Actions `[]`、PR `[]`，未触发，不是远程绿色 |

## Rollback

无数据库迁移。代码回退会恢复非流式请求；回退前停止 active Run/Worker，避免同一 Run 在不同 transport 行为间继续执行。不得用 destructive Git 命令覆盖用户工作。

## Documentation updates

- [x] README / 用户操作与超时说明。
- [x] API / Benchmark protocol / Architecture / Deployment / Security / Testing。
- [x] ADR-0008，并在 ADR-0006 标记部分取代关系。
- [x] CHANGELOG、ROADMAP、PROJECT_STATUS、Phase 2、NEXT_TASK、本计划与工作日志。

## Completion evidence

- Changed files: Adapter；3 个后端测试文件；ADR-0006/0008；README、CHANGELOG 与相关 `docs/` 权威文档。
- Commands run: Adapter 50 passed；`make test` 后端 453 passed/6 skipped、前端 36 passed；`make lint`、`make smoke`、frontend build、Alembic、lock、Compose config、diff、秘密扫描均通过。
- Acceptance evidence: R1–R7 的 Mock/静态/纵向本地证据均满足；未调用真实 Provider。
- Not run: 修复后的用户 llama.cpp/Cloudflare/Caddy 链路；真实 Provider 调用由用户显式执行。未重复与本 transport 变更无关的 Phase 2 真实基础设施 8 场景验收。
- Known issues: proxy buffering/绝对时限仍须用户链路配置；protocol-v1 transport 重试可能重复上游生成/费用；功能 SHA 的 workflow 因分支无 PR 未触发，精确 SHA 远程绿色尚未取得。

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| 2026-08-27 | discovery | 当前 Run 已冻结 `max_tokens=8192` 与 `read=600s`；16/18 条响应在约 126 秒收到 Cloudflare 524，2 条在约 102–103 秒成功。 | 排除 LLMBenchLab 600 秒读取超时主动中断，修复目标收敛到真 SSE。 |
| 2026-08-27 | decision | 显式发送 `stream:true` 与 `stream_options.include_usage:true`，按 SSE 事件持续消费到 `[DONE]`；普通 JSON 成功体作兼容 fallback。 | 流中 token/心跳刷新代理和 httpx 空闲读取窗口，但不绕过绝对总时长或代理缓冲。 |
| 2026-08-27 | decision | 保留 protocol-v1 的有限重试：真实 transport 异常仍按冻结策略重试；正常 EOF 却缺少 `[DONE]` 记为不完整流。 | 不升级 `protocol_version`；文档继续明示 Provider 调用不是 exactly-once，断线重试可能重复计费。 |
| 2026-08-27 | deviation | 文档终审发现 ADR-0006 的统一 4 MiB Chat 成功边界不能直接覆盖 SSE framing 开销。 | 新增 ADR-0008，明确 JSON/SSE wire/event/content 独立上限并回链旧决定。 |
| 2026-08-27 | discovery | 终审用离线事件复现 JSON escaped lone surrogate 会裸抛 `UnicodeEncodeError`。 | 增加严格 Unicode 映射和回归，统一为 `invalid_provider_stream`。 |
