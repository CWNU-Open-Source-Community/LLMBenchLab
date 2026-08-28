# ADR-0008：OpenAI-compatible 真 SSE 与空闲超时边界

- **Date**: 2026-08-27
- **Deciders**: LLMBenchLab maintainers（响应用户真实 llama.cpp 长生成被中途断开的报告）
- **Scope**: OpenAI-compatible Chat transport、超时语义、响应资源上限与失败边界
- **Related requirements**: FR-MOD-11、FR-RUN-02、FR-RUN-10、NFR-REL-02、NFR-SEC-02、NFR-REP-02
- **Supersedes**: 部分取代 [ADR-0006](ADR-0006-local-real-provider-evaluation.md) 把 Chat 成功响应统一限制为 4 MiB 的决定；普通 JSON 与最终聚合 content 仍为 4 MiB，SSE wire/单事件改为 64 MiB/1 MiB
- **Complements**: [ADR-0005](ADR-0005-durable-task-execution.md) 的 at-least-once/有限重试边界，以及 [ADR-0007](ADR-0007-web-provider-credentials.md) 的 write-only stored credential 边界

## Status

Accepted。

## Context

一个 GPQA-Diamond 本地 Run 已冻结 `max_tokens=8192` 和 `read_timeout_seconds=600`，但其 18 条已落库 Response 中有 16 条在约 126 秒收到 HTTP 524，另有 2 条在约 102–103 秒成功。用户同时提供 llama.cpp 日志证据：上游请求在约 126 秒被客户端/代理关闭，服务随后记录 cancel task；模型进程、上下文、队列、显存和生成速度均正常。

当时的 Adapter 虽使用 HTTPX 的 streaming response API 有界下载正文，却没有发送 `stream:true`。因此 Provider 仍会在完整生成结束后才返回一个 JSON body；结合响应链路的 Cloudflare/Caddy headers、Cloudflare 当前默认 125 秒 Proxy Read Timeout，以及用户的 llama.cpp 499 日志，最一致的推断是中间代理先终止了长时间无响应字节的请求。HTTP 524/499 本身不能单独证明具体代理配置，且修复后的真实 Provider 链路尚未由自动化调用验证。

仅把 LLMBenchLab 的 read timeout 从 120 秒提高到 300 或 600 秒不能改写中间代理限制。另一方面，把 8192 tokens 当作 300 秒总时限也不正确：按用户报告的约 8.7 tok/s，单纯生成就可能接近 16 分钟。

## Decision drivers

- 慢生成期间必须持续消费 Provider 实际发送的 token 或 heartbeat，而不是“流式下载一个最终 JSON”。
- HTTP read timeout 必须保留 HTTPX 的相邻数据块空闲语义，不能暗中变成整题 wall-clock 上限。
- 部分、畸形、超大或缺少协议终止标记的流不得被当作成功答案。
- 当前 Key 即使跨多个 delta 被拆分，也不得进入 Response、报告、日志或错误证据。
- `llmbenchlab-protocol-v1` 已冻结 transport 异常的有限重试；本修复不能静默改变该语义。
- 自动测试不得调用用户真实 Provider。

## Decision

### 1. 请求与成功响应

- Chat Completions 请求显式发送 `stream:true`、`stream_options.include_usage:true`、`Accept: text/event-stream` 和 `Accept-Encoding: identity`。
- `Content-Type: text/event-stream` 的 2xx 响应按字节增量解析，支持任意网络分块、UTF-8 跨块、LF/CRLF/CR、comment heartbeat、多行 `data:`、role-only delta、`delta.content`、finish、可选 usage-only 尾块和 `[DONE]`。
- finish 到达后不得提前返回；若 Provider 发送 usage-only 尾块则继续消费，只有收到 `[DONE]` 才完成。usage 可以缺失并保持 `null`，但干净 EOF 缺 `[DONE]` 必须记为 `incomplete_provider_stream`。
- `delta.reasoning_content` 和 llama.cpp `timings` 可以出现但不进入评测答案；现有评测语义仍只使用最终可见的 `content`。
- 忽略 `stream:true` 并返回普通 2xx JSON 的兼容 Provider 继续走既有 JSON fallback。压缩响应继续 fail closed。

### 2. 资源与秘密边界

- 普通 JSON success body：4 MiB。
- 非 2xx error body：64 KiB。
- SSE wire 总量：64 MiB；单个 SSE event：1 MiB；最终聚合 `delta.content`：4 MiB。
- comment、reasoning、timings 与 framing 虽不持久化，仍计入 wire/event 上限。原始 SSE 行和事件不得记录到日志或错误证据。
- 所有 content delta 先聚合，再做当前 Key 的精确替换，避免逐块脱敏漏掉跨 delta 的 Key；usage、request ID、returned model、fingerprint 与 finish reason 延续现有递归/字段脱敏。
- 非法 UTF-8/JSON/字段类型和冲突元数据统一为 `invalid_provider_stream`；流内 error 为 `provider_stream_error`；缺终止标记为 `incomplete_provider_stream`；任何上限超出为 `provider_response_too_large`。部分 content 不保存为成功答案。

### 3. 超时、重试与代理

- `read_timeout_seconds` 是 HTTPX 等待下一段响应字节的最大空闲时间，不是总生成时限。正常 token 或 comment 到达会刷新该客户端窗口；LLMBenchLab 不增加另一层整题 wall-clock timeout。
- 为保持 `llmbenchlab-protocol-v1`，真正的 `httpx.TransportError` 仍按 Run 快照执行有限重试，即使前一 attempt 已收到部分 SSE；前一 attempt 的部分 content/usage/ID 全部丢弃。该 at-least-once 行为可能重复上游生成和费用，不提供 exactly-once。正常 EOF 缺 `[DONE]` 是协议错误，不自动 HTTP 重试。
- SSE 只能在 Provider 与 Worker 之间每一层实际尽早发送正确 Content-Type 并持续 flush 时刷新代理窗口。Cloudflare、Caddy 或其他 Gateway 的 buffering、首字节、空闲和绝对总时长仍须独立配置；本决定不声称已经检查或修改用户代理，也不保证绕过绝对时长限制。

## Alternatives

### 只把总 timeout 提到 300/600 秒

- 未选择：现有 Run 已是 600 秒客户端 read timeout，仍在约 126 秒收到代理 524；而 8192 tokens 的真实生成时间可能远超 300 秒。

### 收到 finish 或干净 EOF 即接受部分流

- 未选择：会提前错过 usage 尾块，也会把被截断但碰巧带正文的响应误判成功。当前 OpenAI/llama.cpp 兼容契约以 `[DONE]` 为完整终止证据。

### 部分 SSE 后禁用 transport retry

- 暂不选择：这会改变 protocol-v1 已冻结的重试语义和历史 Run 可比性。当前保留有限重试并明确重复费用风险；未来调整必须升级协议版本或另立决定。

### 不限制 SSE wire

- 未选择：无限 heartbeat、reasoning、timings 或 framing 可造成内存/带宽资源耗尽。wire/event/content 使用独立上限。

## Consequences

### Positive

- Provider 实际流式发送时，Worker 会及时消费 token/heartbeat，避免应用自身等待一个最终 JSON 才读取。
- 长生成可以超过单次 read timeout，只要相邻数据块间隔未超限。
- 完整性、资源、秘密和 JSON fallback 边界有稳定测试与错误分类。

### Negative

- `stream_options.include_usage` 或 SSE framing 不兼容的 Provider 可能明确返回 4xx/stream error；需要按该 Provider 文档处理。
- SSE framing 把 wire 安全上限从统一 4 MiB 扩到 64 MiB，单请求最大网络/解析工作量上升。
- transport 断线后的 protocol-v1 重试可能重复生成和计费。
- 上游 buffering 或绝对总时长仍可能造成 524/499；应用代码无法替代代理配置。

## Validation

- 使用 MockTransport/自定义异步字节流覆盖 llama.cpp 形态的 heartbeat、任意分块、UTF-8、多行 data、finish、可选 usage、`[DONE]`、JSON fallback、断流重试、严格 EOF、流内错误、大小边界、并发隔离与跨 delta Key 脱敏。
- 通过 preflight 和 Web stored-key Worker/report 纵向测试确认流式 payload、timeout snapshot、聚合结果与持久化脱敏。
- 继续运行完整 lint/test/build/smoke、Alembic、lock、Compose config、diff 与秘密扫描；全部自动化只使用 Mock/stub，不调用真实 Provider。
- 修复后的用户 llama.cpp/代理链路必须由用户显式真实评测验证，不能由 Mock 门禁替代。

## Security and privacy impact

SSE 增大了可接受的 wire 数据量，因此必须同时实施 wire/event/content 三层上限、identity-only 编码、严格终止、原始事件不记录和聚合后 Key 脱敏。它不增加新的凭据存储位置，也不扩大可信 loopback/Web write-only 边界。代理 access log、浏览器扩展、同机进程和目标 Provider 仍在既有信任边界之外。

## Rollback or migration

无数据库迁移。回滚 Adapter 会恢复非流式最终 JSON 行为，并重新暴露慢生成在中间代理首字节/空闲窗口内无响应的风险。回滚前应停止 active Run/Worker，避免同一个 Run 在两种 transport 行为间继续；不得自动删除已有 Response 或用户评测证据。

## References

- [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/)
- [Cloudflare Error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/)
- [Caddy reverse_proxy streaming](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming)
- [llama.cpp server schema](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-schema.cpp)
- [llama.cpp server task defaults](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-task.h)

## Change history

| 日期 | 变化 | 原因 |
|---|---|---|
| 2026-08-27 | Accepted | 用户真实长生成中断报告要求把伪流式下载修正为有界真 SSE，并明确空闲超时、代理和 protocol-v1 重试边界 |
