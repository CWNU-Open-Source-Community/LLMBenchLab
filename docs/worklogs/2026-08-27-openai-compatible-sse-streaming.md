# 2026-08-27 — OpenAI-compatible SSE 流式生成修复工作日志

> 本日志记录实际发生的证据、决定和门禁，不调用真实 Provider。

## 元信息

- 日期：2026-08-27
- 执行者：Codex
- 关联计划：[OpenAI-compatible SSE 流式生成修复计划](../plans/2026-08-27-openai-compatible-sse-streaming.md)
- 关联阶段：[Phase 2 — 可靠性与任务执行](../phases/PHASE-2-RELIABILITY.md)
- 初始分支：`codex/complete-evaluation-workflow`
- 初始 HEAD：`160c90a3b02e91fd62f1021f75464d0c0e15e09e`
- 初始状态：本地与 `origin/codex/complete-evaluation-workflow` 同步，工作树干净
- 最终状态：in_progress

## 用户报告

用户的 llama.cpp 诊断显示最近请求在约 126 秒被客户端/上游代理关闭，生成速度约 8.3–8.8 tok/s、上下文未截断、进程无 OOM/崩溃/排队；建议评测客户端使用至少 300 秒等待并以 `stream:true` 持续消费 SSE。

## 目标、范围与验收

- 目标：把 OpenAI-compatible Chat 从“流式下载最终 JSON”改为真正请求并增量消费 SSE，在慢生成期间持续读取 token/heartbeat，并保留普通 JSON fallback。
- 范围：Adapter 请求/解析/错误/资源/脱敏边界；preflight 与 Web stored-key Worker/report 离线纵向回归；ADR、协议、部署与权威状态文档。
- 非目标：不调用或重跑用户真实模型，不修改/恢复/删除既有 Run，不替用户配置 Cloudflare/Caddy，不改变评分或 protocol-v1 transport 重试语义。
- 验收：严格消费到 `[DONE]`，可选 usage、任意分块/UTF-8/换行、JSON fallback、断流/错误/上限/并发/跨 delta Key 均有回归；全量 lint/test/build/smoke/static gate 通过；提交/push/精确 SHA 远程边界如实记录。

## 假设与风险

- 目标 llama.cpp 遵循当前官方 SSE schema 并能发送 comment ping；以官方源码核对，修复后真实服务仍需用户验证。
- HTTPX read timeout 是等待下一数据块的空闲上限；若 Provider/代理不 flush、缓冲 SSE 或设置绝对总时长，应用实现仍不能消除 524/499。
- protocol-v1 对 transport 异常保留有限重试；部分流不会保存为成功，但重试可能重复上游生成和费用。
- SSE framing 开销不能与最终答案共用一个 4 MiB 上限；独立 wire/event/content 上限属于安全边界变化，必须由新 ADR 记录。

## 本地证据

只读查询 `backend/data/llmbenchlab.db`：

- 当前 Run：`93d8e0a5-6f13-4f0b-aa00-fc820847ebf8`，模型 `Local Qwen3.8 27b Q4`，Benchmark `gpqa-diamond`。
- 快照已是 `max_tokens=8192`、`execution.timeouts_seconds.read=600.0`，因此不是旧 60 秒配置。
- 查询时已有 18 条 Response：16 条为 `provider_5xx` / `Upstream returned HTTP 524: <none>`，按约 126 秒间隔落库；2 条成功题延迟约 102.1/103.3 秒，输出 851/878 tokens。
- 当前 Adapter 的 HTTP 客户端使用 `client.stream()` 有界读取，但 `_build_payload()` 没有 `stream:true`；这只是流式下载最终响应，不会要求 Provider 在生成中发送 token。

## 初步结论

本地快照排除了 LLMBenchLab 600 秒 read timeout 在约 126 秒主动到期。结合旧 payload 没有 `stream:true`、响应链路 headers、Cloudflare 当前默认 125 秒 Proxy Read Timeout 和用户的 llama.cpp 499/cancel 日志，最一致的推断是代理先终止了长时间无响应字节的非流式请求。HTTP 524/499 本身不能单独证明具体代理配置；真正的 SSE 只有在 Provider/代理实际 flush 时才会持续提供字节，且修复后真实链路尚未由本任务调用验证。

## 执行记录

| 时间 | 类型 | 事实/操作 | 结果 |
|---|---|---|---|
| 19:15 CST | discovery | 检查当前 Run 快照和 Response 错误。 | 确认 600 秒客户端读取配置下仍是约 126 秒 HTTP 524。 |
| 19:16 CST | code audit | 检查 `OpenAICompatibleAdapter.generate/_build_payload/_read_response_body`。 | 确认请求未发送 `stream:true`，当前“stream”只指 httpx 响应读取方式。 |
| 19:20 CST | upstream audit | 核对 llama.cpp 官方 stream schema、SSE 事件顺序与心跳实现。 | 确认支持 `stream:true` / `include_usage:true`，正常序列含 finish、usage-only 块、`[DONE]`，并默认发送 SSE comment ping。 |
| 19:24 CST | protocol decision | 复核 protocol-v1 已冻结的有限重试语义。 | transport 异常继续按现有策略重试；缺少 `[DONE]` 的正常 EOF 不作成功，不升级协议版本。 |
| 收尾前 | implementation | Chat payload/headers 增加真 SSE；实现 request-local 有界 parser、聚合结果、JSON fallback 与稳定错误。 | 支持任意分块/UTF-8/换行/comment/multi-data/finish/可选 usage/`[DONE]`；reasoning/timings 不混入答案。 |
| 收尾前 | security | 分离 JSON 4 MiB、SSE wire 64 MiB、event 1 MiB、content 4 MiB、error 64 KiB；content 聚合后脱敏。 | 原始 SSE 不记录，跨 delta Key 可清除，超限统一中止。 |
| 收尾前 | vertical tests | 扩展 Adapter、preflight 与 Web stored-key Worker/report MockTransport。 | 流式 payload、snapshot timeout、usage/token、持久化/report 脱敏贯通。 |
| 收尾前 | review finding | 终审离线复现 escaped lone surrogate 会裸抛 `UnicodeEncodeError`。 | 严格映射为 `invalid_provider_stream` 并增加回归；Adapter 最终 50 passed。 |
| 收尾前 | governance | 文档终审发现 ADR-0006 的统一 Chat 4 MiB 成功边界与 SSE wire 64 MiB 冲突。 | 新增 ADR-0008 并在 ADR-0006 标记部分取代，不静默改写 Accepted 决定。 |

## 实际实现

- 请求发送 `stream:true`、`stream_options.include_usage:true`、`Accept: text/event-stream` 与 identity-only encoding。
- 2xx SSE 用异步字节迭代器增量解析；支持 BOM、LF/CRLF/CR、comment、多个 `data:` 行、UTF-8 跨 chunk、role/null delta、llama.cpp reasoning/timings、finish、可选 usage-only 尾块和 `[DONE]`。
- 只聚合 `delta.content`；usage 保存最后一个累计对象，稳定 request/model/fingerprint 冲突会失败，finish 后继续读到 `[DONE]`。干净 EOF 缺终止标记为 `incomplete_provider_stream`。
- 2xx 非 SSE 继续普通 JSON fallback；非 2xx、压缩拒绝、状态码重试和 protocol-v1 transport 有限重试保持原边界。transport 重试会丢弃前一 attempt 的部分流，可能重复上游费用。
- SSE wire/event/content 分别限制为 64 MiB/1 MiB/4 MiB；普通 JSON success 4 MiB、error 64 KiB。流内 JSON/Unicode/字段错误、Provider error、超限和空/长度截断具有稳定 AdapterError。
- content 完整聚合后再精确替换当前 Key，避免跨 delta 拆分绕过；raw SSE 事件不进入日志、错误或持久层。

## 修改文件

- 实现：`backend/app/adapters/openai_compatible.py`。
- 测试：`backend/tests/test_adapters.py`、`backend/tests/test_provider_preflight.py`、`backend/tests/test_web_credentials.py`。
- 决定/计划/日志：ADR-0006、ADR-0008、本任务 plan/worklog。
- 用户与权威文档：`README.md`、`CHANGELOG.md`、API、Architecture、Benchmark Protocol、Deployment、Security、Testing、Roadmap、Phase 2、Project Status、Next Task。

## 验证结果

| 命令/检查 | 结果 | 证据 |
|---|---|---|
| `cd backend && uv run pytest tests/test_adapters.py -q` | 0 | `50 passed`，含真 SSE、JSON fallback、断流、严格终止、大小、并发、Key、lone surrogate |
| `make test` | 0 | 后端 `453 passed, 6 skipped`；前端 9 files / `36 passed`；6 skip 是未注入 DSN 的既有 infrastructure marker |
| `make lint` | 0 | Ruff check/format `109 files`、ESLint、TypeScript typecheck 通过 |
| `cd frontend && npm run build` | 0 | production build 成功；仅既有约 660 kB chunk warning |
| `make smoke` | 0 | `1 passed, 6 deselected`，隔离 SQLite + Mock |
| `cd backend && uv run alembic upgrade head && uv run alembic check` | 0 | SQLite 在 head；`No new upgrade operations detected` |
| `cd backend && uv lock --check` | 0 | 50 packages 解析一致 |
| `docker compose config --quiet` | 0 | 配置有效 |
| `git diff --check` | 0 | 无 whitespace error |
| 高置信 added-diff/untracked secret scan | 0 | 无真实 Key、Bearer token 或私钥模式命中 |

## 中间失败与修复

- 第一次 `make lint` 的 Ruff check 通过，但 format check 报 `openai_compatible.py` 一处自动格式差异；执行 Ruff formatter 后重跑通过，没有降低断言。
- 首轮全量测试为后端 452 passed/6 skipped；终审补上 lone-surrogate 回归和实现后，最终全量变为 453 passed/6 skipped 并再次通过。

## 未运行验证

- 未调用修复后的真实 llama.cpp/Provider，未产生真实 API 请求或费用；用户报告的 126 秒问题是否在其完整 Cloudflare/Caddy 链路消失仍需用户显式 Run 验证。
- 未检查或修改用户的 Cloudflare/Caddy 实际配置；文档只记录官方一般行为和需要核对的 flush/buffering/timeout 边界。
- 未重复 `make phase2-acceptance` 或独立真实 PostgreSQL/Redis integration；本任务没有修改数据库 schema、lease、queue 或 Compose 拓扑，既有证据保持其原边界。
- 功能提交 `af345af1048eeddffd784fdca1da419df95da7e2` 已正常 push；其精确 SHA Actions 查询为 `[]`、分支 PR 查询为 `[]`。workflow 仅监听 PR/main，当前未触发，远程绿色前任务保持 `in_progress`。

## 安全与运行边界

- 未读取或输出 stored API Key/keyring/密文；数据库查询只显示公开快照、错误类型和时延。
- 未调用真实 Provider，未取消或修改用户当前 Run。
- 只读复核时该 Run 已进入 `cancelled`，完成 18/198；本任务未发起取消、恢复或任何真实 Provider 请求。
- 自动化必须继续使用 MockTransport/自定义字节流，保持成功体、错误体、压缩和秘密脱敏上限。
- 没有 destructive Git 操作、force push、PR、Issue、Release 或外部配置变更。

## 当前待完成

- 提交并 push 本次纯文档 evidence update，再查询其精确 SHA；不创建未获授权的 PR。
- 要取得必需远程绿色，仍需用户授权创建 PR，或由维护者把工作合入 `main` 触发 workflow。

## 当前 Git 状态

```text
branch: codex/complete-evaluation-workflow
initial HEAD: 160c90a3b02e91fd62f1021f75464d0c0e15e09e
feature commit: af345af1048eeddffd784fdca1da419df95da7e2
push: origin/codex/complete-evaluation-workflow succeeded
feature exact-SHA Actions runs: []
branch pull requests: []
evidence update: pending separate documentation commit
working tree: 仅本次远程证据文档更新尚未提交
```
