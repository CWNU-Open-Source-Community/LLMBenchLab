# ADR-0007：Web 只写输入与应用层加密 Provider 凭据

- **Status**: Accepted
- **Date**: 2026-08-27
- **Deciders**: LLMBenchLab maintainers（用户明确要求 Web 用户直接输入 API Key）
- **Scope**: Model API、Web 模型表单、Provider 凭据存储、Worker 解密、部署秘密
- **Related requirements**: FR-MOD-05–10、NFR-SEC-01–05
- **Supersedes**: [ADR-0004](ADR-0004-secret-management.md) 中“REST/前端不得接收明文密钥”和“数据库只能保存环境变量名”的绝对限制
- **Superseded by**: 无

## Context

当前 Web 表单只让用户填写 `api_key_env` 环境变量名，真实 Key 必须在启动 API 与独立 Worker 前另行注入。用户在 2026-08-27 明确要求最终用户直接在 Web 端输入 API Key。API 与 Worker 是独立进程，任务又必须支持重启、租约恢复和 SQLite/PostgreSQL，因此 API 进程内临时内存、浏览器每次重传或 Redis 临时转发都不能可靠满足执行语义。

直接把明文 Key 放入 `models`、Run snapshot 或队列会让数据库备份、报告、API、浏览器开发工具、SQL 日志和 SQLite→PostgreSQL 导入链全部扩大泄漏面。另一方面，持久凭据新增了 credential-forwarding 风险：若 PATCH 允许在保留旧 Key 的同时把 Base URL 改成攻击者地址，后续 Run 会把旧 Key 发送到新地址。

## Decision drivers

- Web 用户只需输入 Provider URL、模型名和 API Key，即可让独立 Worker 完成真实评测。
- API Key 必须是只写字段；系统不能把凭据数据流中的值或 Provider 对它的回显复制进读取 API、Run model snapshot、报告、队列、日志或错误响应。
- 存储必须兼容 SQLite/PostgreSQL、多进程、重启、租约恢复和凭据轮换。
- 部署主密钥不能与密文一起进入数据库；普通 Mock 流程在主密钥缺失时仍可工作。
- 现有 `api_key_env` 配置和可信本地 CLI 必须保持兼容。

## Decision

新增一模型一行的 `model_credentials` 表，以 `model_id` 作为主键/外键，并用 AES-256-GCM 应用层加密保存 Provider Key。Web/API 请求使用 8–8192-byte visible-ASCII write-only `api_key`；业务层立即从 `SecretStr` 取值并加密。`models` 只保存 `credential_source=none|environment|stored`，Run 继续使用已有 `model_id`，不增加 credential reference。公开 Model schema 返回非秘密的 `credential_source`、`has_api_key` 和 legacy `api_key_env` 名称，但不返回密文、nonce、key id 或明文。

API 与 Worker 从同一部署级 keyring 读取主密钥。严格 JSON keyring 具有一个 `active_key_id` 和最多 32 个 32-byte base64url key；active key 用于新写入，全部已列出的 key 可用于解密旧记录。可信本地 `make setup` 生成 Git 忽略、`0600` 且经严格校验的 keyring 文件；Compose 以只读 secret 同时挂载给 API 与 Worker。主密钥缺失、格式错误、未知 key id、认证标签错误或文件不可读时 fail closed，并只暴露稳定的非敏感错误码。Mock 和旧环境变量模式不读取该文件。

每次加密使用独立 96-bit 随机 nonce。AEAD additional authenticated data 绑定算法版本、`model_id` 与规范化 Provider origin；密文记录不能被静默复制到另一模型或另一 Provider origin。公开 Run snapshot 的 `model` 子投影只冻结已有 `model_id`、Provider endpoint 和 `credential_source=stored`，不含 Key 或加密材料。Worker 必须用 Run 的 `model_id` 和冻结 Base URL 解密该模型当前的一行 credential。创建 Run 与修改模型都使用同一方言锁：PostgreSQL 对 Model 行执行 `SELECT ... FOR UPDATE`，SQLite 在读取前执行 `BEGIN IMMEDIATE`。只要存在 `pending`/`running` Run，就禁止端点或凭据变化，因此一次执行及其重试不会漂移到另一个 Key。SQLite 锁竞争可能短暂阻塞请求，仍只适合低并发本地模式；生产或并发评测推荐 PostgreSQL。

Model 更新遵循以下规则：

- `api_key` 省略表示保留现有凭据；8–8192-byte visible-ASCII 值表示创建或替换加密凭据；空值不表示清除。
- create/PATCH 会扫描精确 `ModelRead` 全字段和 Run snapshot 的 `model` 子投影，拒绝把新 Key 或保存旧 Key 从凭据流复制进这些公开表面。Provider 返回证据则递归扫描对象键与 JSON 标量，并在持久化前替换当前 Key 的精确回显；这不是对无关 Benchmark/Question 内容的全局字面扫描。
- 若 stored row 缺失，或旧 envelope 因未知/旧 `key_id`、损坏而不可读，可在 active keyring 可用时通过隔离 PATCH 用显式新 Key 覆盖，或只切换 Mock/legacy environment 清理；夹带无关公开更新返回 422，保留 `stored` 且没有新 Key 时返回 503，两者都保持事务不变。
- 规范化 Provider origin 改变时必须在同一请求重新提交 `api_key`，不得无提示沿用旧 Key。
- 存在 `pending` 或 `running` Run 时，不允许修改 Provider 类型、Base URL、远端模型名或凭据。
- 切换为 Mock 会清空远端/冲突字段并删除 encrypted credential row；切换为旧环境变量来源会保留 Base URL 与远端模型名、保存变量名称并删除 encrypted credential row。历史终态 Run 只保留不可恢复秘密的配置快照与评测证据。
- 旧 `api_key_env` 模型继续可运行；新 Web 默认只提交 `api_key`。新 Run 明确冻结使用 `stored` 或 `environment` 来源，不把值写进 snapshot。

### 约束与不变量

- 读取 schema 不得暴露凭据字段、密文、nonce 或 keyring 内容；OpenAPI 仅在 Model 写请求 schema 中声明 `api_key` 为 `writeOnly`。防泄漏承诺限定于不从凭据流或 Provider 回显复制 Key，不承诺无关用户数据永远没有相同文本。
- SQLAlchemy 必须隐藏 bind parameters；异常、日志和验证响应不得反射请求值或加密记录。
- Key 只短暂存在于浏览器受控 password input、API 进程内存和 Worker 请求内存；不得写 local/sessionStorage、URL、console、argv 或队列。
- CI、单元测试与 Smoke 只使用明显的假 Key、固定测试 keyring 和 MockTransport，不进行真实 Provider 请求。
- 该能力仍只面向 loopback 的可信本地部署。它不引入身份认证、租户授权或生产 KMS，不能据此把 UI 暴露公网。

## Alternatives

### 方案 A：API 进程内临时保存

- 优点：不落盘，实现小。
- 缺点：独立 Worker 无法读取，API 重启、任务延迟和恢复都会丢失。
- 未选择原因：不满足当前 durable Worker 架构。

### 方案 B：Redis/队列传递明文

- 优点：API 与 Worker 可跨进程传递。
- 缺点：Redis AOF、队列消息、重放和运维工具会持久化或展示秘密；Redis 又是可选通知层。
- 未选择原因：违反数据库为事实源及队列不承载秘密的边界。

### 方案 C：操作系统 Keychain/Secret Service

- 优点：本机秘密托管体验较好。
- 缺点：macOS/Linux/Windows/容器行为不同，独立 Worker 未必共享登录会话。
- 未选择原因：不适合作为当前跨平台默认；可作为后续可插拔 backend。

### 方案 D：外部 KMS/Secret Manager

- 优点：权限、审计与主密钥轮换最成熟。
- 缺点：引入云依赖、账号、费用与部署复杂度。
- 未选择原因：超出可信本地 MVP；公共多用户部署前必须重新评估。

## Consequences

### Positive

- 用户可以直接在 Web 输入 Key，独立 Worker 在重启和恢复后仍能执行。
- 数据库不含明文 Key，公开 API、Run snapshot 与报告不承载秘密材料。
- key id 列表允许先加入新主密钥、再逐步重写凭据，避免一次性停机轮换。
- origin 绑定与更新约束降低旧 Key 被无提示转发到新端点的风险。

### Negative

- 应用主密钥成为新的部署秘密；主密钥与数据库同时泄漏时，Provider Key 仍可能被解密。
- 浏览器、API 与 Worker 运行时内存仍短暂持有明文；被攻陷主机不在本 ADR 的防护范围内。
- 新增表、外键、迁移、加密依赖、配置与备份恢复要求。

### Neutral / follow-up

- 公网或多用户版本仍需认证、授权、租户隔离、审计、CSRF/SSRF 防护与外部 KMS。
- 后续可增加批量重加密、keyring 健康检查与外部 KMS；本次先交付 Web 创建/替换、切换来源时删除和 Worker 使用。

## Validation

- API create/PATCH、422/409/500、日志和 ORM repr 使用 marker 假 Key，断言都不反射秘密。
- 原始数据库断言无明文、同一假 Key重复加密得到不同 nonce/ciphertext；错误主密钥稳定失败。
- Worker 通过 MockTransport 解密并形成正确 Authorization，同时 Provider 回显的对象键与 JSON 标量递归脱敏。
- GET/list、Run API/snapshot、queue、report 三文件和 SQLite→PostgreSQL 摘要不包含秘密材料。
- 前端断言 password input、编辑留空省略字段、提交后清空 state、无浏览器持久化。

## Security and privacy impact

数据库只获得带认证的密文，但其保密性依赖独立 keyring。备份数据库时不能假定其中没有敏感材料；keyring 必须单独备份、限制权限并与数据库分开保存。用户配置的任意 HTTPS Base URL 仍可能是恶意目标，origin 变更重输 Key 只降低误转发风险，不替代 allowlist、DNS 重绑定防护和出站隔离。

## Rollback or migration

迁移为现有 Model 增加非空 `credential_source`：既有 OpenAI-compatible 行回填为 `environment`，Mock 回填为 `none`；同时创建以 `model_id` 为主键的 `model_credentials`，Run 不增加列。只要凭据表存在任意行，downgrade 就在任何 DDL 前硬失败；用户必须先切换到环境变量/Mock 或保留新版本，避免静默丢失 Key。移除功能不会删除已有历史 Run/Response。

## References

- [ADR-0004 — 数据库仅保存密钥环境变量名](ADR-0004-secret-management.md)
- [ADR-0005 — Durable task execution](ADR-0005-durable-task-execution.md)
- [ADR-0006 — 可信本地正式数据集与真实 Provider 评测入口](ADR-0006-local-real-provider-evaluation.md)

## Change history

| 日期 | 变化 | 原因 |
|---|---|---|
| 2026-08-27 | Accepted | 用户明确要求 Web 用户直接输入 API Key，并接受由新 ADR 改变旧 UI 凭据边界 |
