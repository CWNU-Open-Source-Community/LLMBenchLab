# ADR-0019：显式 Provider API 协议与独立适配器

- Status: Accepted
- Date: 2026-08-30
- Deciders: LLMBenchLab maintainers（用户明确要求兼容 OpenCode Go 的三类 API 端点）
- Scope: Model 类型、Provider Adapter、可信本地预检、Run 快照、Web 模型配置、迁移与安全边界
- Related requirements: FR-MOD-02–11、FR-RUN-02、FR-REP-01–04、NFR-REL-02、NFR-SEC-01–05
- Supersedes: 部分扩展 [ADR-0006](ADR-0006-local-real-provider-evaluation.md) 与 [ADR-0008](ADR-0008-openai-compatible-sse-transport.md) 的 Chat-only Provider 范围；既有 Chat 语义继续有效
- Superseded by: 无

## Context

OpenCode Go 在同一 API 根地址下按模型暴露三种不兼容协议：OpenAI-compatible Chat Completions、OpenAI Responses 和 Anthropic Messages。现有 `openai_compatible` Adapter 只实现 Chat Completions：它固定追加 `/chat/completions`、发送 Chat payload，并按 `choices[0].message.content` 或 `choices[].delta.content + [DONE]` 解析。把完整 `/responses` 或 `/messages` 地址填入 `base_url` 会生成错误的嵌套路径；即使只修正路径，请求、认证、JSON、SSE、usage 与终止语义仍不兼容。

`provider_type` 在当前系统中实际承担“选择 Adapter”的职责，并已冻结进 Run 的 `adapter_type` 快照。因此，本次不新增可漂移的第二个协议列，而是扩展这个封闭 Adapter 类型集合。已有数据库行和 API 请求必须无修改地继续表示 Chat Completions。

## Decision drivers

- 协议必须显式选择，不能依赖 URL 猜测或错误后的隐式 fallback；一次 Run 的请求/解析语义必须可从快照复现。
- 既有 `openai_compatible` 数据、API 客户端、CLI 默认值与 Chat SSE 行为必须向后兼容。
- 三种协议必须复用相同的 HTTPS/loopback、redirect、identity encoding、正文上限、秘密脱敏、有限 retry 和逐 attempt ledger 边界。
- 自动化只能使用 Mock、MockTransport 或自定义内存字节流，不得调用真实 Provider。
- 适配器必须把不同协议的文本、usage、finish reason、request id 和返回模型归一化为 `ModelGenerationResult`，而不能让协议细节泄漏到 Evaluator。

## Decision

扩展 `ProviderType`/Adapter 类型为：

- `openai_compatible`：保留现有 OpenAI-compatible Chat Completions 行为和默认值；
- `openai_responses`：OpenAI Responses 请求、JSON 与 typed SSE；
- `anthropic_messages`：Anthropic Messages 请求、JSON 与 typed SSE。

三类远程 Adapter 都接受兼容根地址或与所选协议匹配的完整 endpoint。根地址分别追加 `/chat/completions`、`/responses`、`/messages`；完整 endpoint 必须与显式类型一致，其他已知协议后缀在 Model 校验/Adapter 构造阶段被拒绝，不发送网络请求。`GET /models` 从三个已知后缀推导同级 `/models`，并按显式协议鉴权：Chat/Responses 使用 `Authorization: Bearer`，Messages 使用 `x-api-key` 与 `anthropic-version`。Messages discovery 对 `has_more/last_id` 使用有界 `after_id` 分页，受累计 100 页、60 秒 wall-clock、10,000 个模型 ID、2 MiB 与缺失/重复 cursor 门禁约束。

`openai_responses` 把渲染后的消息映射到 `input`，把 `max_tokens` 映射为 `max_output_tokens`，解析普通 JSON 的 `output` 文本项和 `input_tokens/output_tokens`，并以 `response.completed` 为成功流终止证据。失败、incomplete 或干净 EOF 缺终止事件不得保存部分答案。

`anthropic_messages` 把初始 system instruction 放入顶层 `system`，正文放入 `messages`，使用 Messages 所需认证/version headers，解析普通 JSON 的 `content[].text`、`stop_reason` 与 `input_tokens/output_tokens`，并以 `message_stop` 为成功流终止证据。`message_start`/`message_delta` usage 合并但不重复求和。

现有 Run 配置字段保留通用名称。Chat 可转发 `temperature/top_p/max_tokens/seed`。为保持旧客户端兼容，请求 Schema 继续暴露 Chat 的 `temperature=0/top_p=1/seed=42` 默认；Responses 与 Messages 在请求和 Model 默认都未显式提供采样字段时，将 `temperature/top_p/seed` 归一化为 `null` 并从 Provider payload 省略，避免某些模型拒绝不支持的采样字段。两类新协议都不支持当前项目的非空 `seed`；Messages 的非空 `temperature` 限制为 `0..1`，并且需要有限 `max_tokens`，显式 Provider 托管的 `null` 在外发前稳定拒绝。前端按 Adapter 类型留空/禁用不支持的字段并解释原因，REST/CLI 仍由后端做最终校验。

### 约束与不变量

- 不根据模型名称自动选择协议，也不在一次调用失败后切换协议或重复外发到另一 endpoint。
- `openai_compatible` 的 URL、payload、SSE `[DONE]`、JSON fallback、重试和元数据合同保持兼容。
- 新协议沿用现有 wire/event/content/error 上限、聚合后 Key 脱敏、非 2xx 分类和 attempt settlement；三协议的 malformed JSON/SSE 与 oversized 响应都生成无原始 Provider `__cause__`/`__context__` 的安全异常，原始 SSE/headers/bytes 不进入日志或持久化证据。
- 只有显式白名单中的 typed transient 错误可重试：Responses 的 rate-limit/server error，Messages 的 `rate_limit_error`、`api_error`、`overloaded_error`、`timeout_error`，以及 Messages HTTP `529`；普通 JSON 和 SSE 使用同一分类，未知流内错误仍 fail closed，每次重试继续独立进入 attempt ledger。
- Provider 类型、endpoint、远端模型或凭据在 active Run 期间继续不可修改；Run 快照的 `adapter_type` 是恢复时的权威选择。
- 不把 OpenCode Go 的当前模型清单硬编码成后端 allowlist；模型与 endpoint 对应关系可能变化，操作者按官方文档选择显式协议。

## Alternatives

### 方案 A：任意完整 URL 原样 POST

- 优点：改动最小。
- 缺点：Chat payload/headers/parser 仍会让 Responses/Messages 失败，并把错误从 404 推迟到 400 或响应解析阶段。
- 未选择原因：不能形成真实协议兼容，且容易误发付费请求。

### 方案 B：根据 URL 后缀自动推断协议

- 优点：UI 少一个选择项。
- 缺点：根地址无法可靠推断，路径别名/网关会产生歧义，历史 Run 也没有显式协议快照。
- 未选择原因：不满足可复现和 fail-closed 要求。

### 方案 C：新增独立 `api_protocol` 数据库列

- 优点：概念上可把供应商与协议分开。
- 缺点：当前 `provider_type` 本来就是 Adapter registry key；保留两个可表达同一事实的字段会引入非法组合与迁移复杂度。
- 未选择原因：当前阶段没有独立 vendor 抽象，扩展既有封闭 Adapter 类型更小且不丢语义。

## Consequences

### Positive

- OpenCode Go 的 Responses 与 Messages 模型可以在同一评测链路中显式、安全地配置。
- 旧 Chat 模型和历史 Run 不需数据重写，默认行为不变。
- 失败会在协议边界得到稳定分类，不再构造 `/responses/chat/completions` 等错误 URL。

### Negative

- Adapter/测试矩阵扩大，协议上游扩展字段仍可能需要后续兼容。
- Responses/Anthropic 的采样参数与 Chat 不完全相同；调用方必须接受新协议的采样字段默认省略、seed 不可用、Messages `temperature<=1` 及有限输出上限。
- Mock 门禁不能证明 OpenCode Go 当日网关实现、模型可用性或真实账单行为。

### Neutral / follow-up

- 本决定不改变 `llmbenchlab-protocol-v1` 的题目、评分、分母或排行榜隔离；它只扩展 transport Adapter。
- 未来新增 Gemini 等协议时继续增加显式 Adapter 类型并另行记录合同，不扩展成任意代理。

## Validation

- 用 MockTransport 分别断言三类 endpoint、生成与 discovery headers、Messages bounded pagination、payload、JSON 成功、typed SSE 成功、usage/metadata、typed transient retry/529、非 2xx、EOF/终止、超限与当前 Key 脱敏。
- 用 API/Runner/credential 测试断言迁移后的类型校验、active-Run 门禁、Run snapshot 与 Adapter registry。
- 用前端组件测试断言类型选择、协议说明、字段禁用/清空和提交 payload。
- 运行完整 lint/test、双方言 migration、Mock smoke、frontend build 和 Compose config；真实 Provider 有意不运行。

## Security and privacy impact

三类协议都向用户选择的 Provider 外发相同评测内容，沿用既有 SSRF、数据外发和费用风险。Messages 会在进程内同时构造 `x-api-key` 与版本 header；这些 header 与 Chat/Responses 的 Authorization 一样不得记录、回显或进入异常对象。新增解析器必须在聚合完成后执行同一当前-Key 递归脱敏，并保持 identity-only、禁 redirect 和有界读取。

## Rollback or migration

迁移 `20260830_0008` 将 `models.provider_type` 从 `VARCHAR(17)` 扩为 `VARCHAR(18)`，并同时替换 Provider 类型 check 与远程配置 check；它不改写既有 `mock`/`openai_compatible` 值。回退前若存在 `openai_responses` 或 `anthropic_messages` Model，downgrade 必须在 DDL 前拒绝，要求操作者先在无 active Run 时显式删除或转换这些配置；随后 downgrade 恢复 `VARCHAR(17)` 与两个旧 check，历史 Run/Response 不自动删除。应用回滚期间应停止 API/Worker，避免新类型已提交而旧代码无法加载。由于 `0008` 不改 audit archive event/field 语义，archive-v1 compatible-head allowlist 显式加入 `0008`；P2-07 尚未实施的 recovery-manifest-v1 exact head 也从 `0007` 前进到 `0008`。

## References

- [OpenCode Go API 端点](https://opencode.ai/docs/zh-cn/go#api-%E7%AB%AF%E7%82%B9)（访问 2026-08-30）
- [OpenAI Responses API quickstart](https://platform.openai.com/docs/quickstart/make-your-first-api-request)（访问 2026-08-30）
- [Anthropic Messages API examples](https://platform.claude.com/docs/en/build-with-claude/working-with-messages)（访问 2026-08-30）
- [ADR-0007 — Web Provider 凭据](ADR-0007-web-provider-credentials.md)
- [ADR-0008 — OpenAI-compatible SSE](ADR-0008-openai-compatible-sse-transport.md)

## Change history

| 日期 | 变化 | 原因 |
|---|---|---|
| 2026-08-30 | Accepted | 用户确认直接实现 OpenCode Go 三类端点兼容 |
