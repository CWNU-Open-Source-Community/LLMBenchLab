# ADR-0004：数据库仅保存密钥环境变量名

- **Date**: 2026-08-24
- **Deciders**: LLMBenchLab maintainers
- **Scope**: MVP 模型凭据与日志边界

## Status

Accepted

## Context

OpenAI-compatible provider 通常需要 Bearer API Key。LLMBenchLab 会把 Model 记录、Run 快照和错误信息持久化，并通过 API/前端展示；若直接保存密钥，数据库备份、日志、截图或 API 响应都可能造成泄漏。开源仓库、CI 和 Mock Smoke Test 也不能依赖真实付费凭据。

MVP 是单用户本地应用，没有成熟的租户级密钥服务、KMS、审计或轮换系统。必须选择一个简单且明确的秘密边界，同时承认用户提供任意 `base_url` 带来的 SSRF 和凭据外送风险。

## Decision

Model 的 `api_key_env` 字段只保存环境变量名称，例如 `OPENAI_API_KEY`；真实值只存在于启动后端进程的环境中，并在发起请求前即时读取。

具体规则：

- `api_key_env` 必须匹配环境变量名语法 `[A-Za-z_][A-Za-z0-9_]*`；`mock` 必须为空，`openai_compatible` 必须提供变量名。
- 创建/更新 Model 时不读取或回显密钥值；API 可返回变量名和“是否已配置”的布尔状态，但不得返回环境变量值、Authorization 或构造后的请求头。
- OpenAICompatibleAdapter 在每次执行前解析环境变量。变量缺失或空值时立即返回稳定的 `missing_api_key` 配置错误，不向上游发请求，也不无限重试。
- 真实 `.env` 必须被 Git 忽略；`.env.example` 只包含占位符和变量名示例。Mock、测试和 CI 不要求任何 API Key，也不得调用真实模型。
- 日志过滤 Authorization、Cookie、常见 key/token/secret 字段和已解析密钥值。上游错误、异常 repr 和 httpx 调试日志在持久化或返回 API 前必须截断、脱敏。
- Run 快照只保存 `api_key_env` 名称，不保存值。密钥轮换不修改 Model；重跑会使用当时进程环境中的新值。
- 密钥只发送到该 Model 配置的目标。MVP 不把任意 `base_url` 视为安全：仅供可信本地操作者使用，不直接暴露公网。
- GitHub Actions 只运行 Mock 路径，不配置真实仓库 Secret。未来确需集成测试时使用专用低权限 Secret、受保护环境和显式手动触发，不能进入 PR from fork。

不得把前端明文输入框作为密钥入口。浏览器只提交环境变量名称，真正的 Secret 从不经过前端或 REST API。

## Alternatives

### 把明文密钥保存在数据库

最容易操作，但数据库、备份、API 序列化和日志都扩大泄漏面，且不符合开源 MVP 的最低安全要求，因此拒绝。

### 应用内加密后保存数据库

静态加密优于明文，但仍需要安全管理主密钥、轮换、备份恢复和多用户授权。把主密钥放在同一 `.env` 中只部分转移问题，Phase 1 暂不承担复杂度。

### 操作系统 Keychain/Secret Service

本地安全体验较好，但 macOS、Linux、Windows 和容器的实现不同，会显著增加安装与测试矩阵。可作为未来个人部署的可选 provider。

### 外部 Secret Manager/KMS

适合生产和多用户，但引入云依赖、权限与成本，不符合离线优先 MVP。公共部署前应重新评估。

### 由前端在每次 Run 临时提交密钥

避免数据库持久化，却让秘密经过浏览器状态、网络请求和调试工具，且难以安全支持后台任务，因此拒绝。

## Consequences

### Positive

- 数据库、Run 快照和常规 API 响应不包含真实 API Key。
- 密钥可通过修改进程环境轮换，不需要迁移 Model 数据。
- Mock Demo、测试和 CI 完全离线，避免意外费用。

### Negative

- 用户必须在启动后端前配置正确的环境变量；变量缺失只能在显式检查或运行时发现。
- 数据库记录本身不能把凭据安全迁移到另一台机器，部署者需独立配置环境。
- 具有进程环境读取权限的本地攻击者仍可获得密钥；本 ADR 不解决被攻陷主机。
- 恶意或误配 `base_url` 可能把 Authorization 发送给攻击者，并可触发 SSRF。环境变量方案本身不能消除此风险。

### Follow-up

- 公网/多用户版本必须引入身份认证、租户隔离、审计、加密秘密存储和轮换策略。
- 加入 URL allowlist、仅 HTTPS、DNS/IP 校验、重绑定防护和出站网络策略后，才允许不受信任用户配置 `base_url`。
- 安全测试持续验证 Model API 不泄漏秘密、日志脱敏有效、CI 不发起真实 provider 请求。
