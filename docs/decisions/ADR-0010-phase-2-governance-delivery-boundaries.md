# ADR-0010：Phase 2 治理交付边界修正

- **Date**: 2026-08-28
- **Deciders**: LLMBenchLab maintainers
- **Scope**: ADR-0009 的可信本地 CLI、Provider metadata 审计、历史延迟来源与 credential 审计载荷
- **Related requirements**: `docs/NEXT_TASK.md` P2-05、P2-06、P2-07
- **Amends**: [ADR-0009](ADR-0009-database-governance-audit-fair-scheduling.md)；未在本文修改的事实来源、锁序、账本、背压、公平、恢复、保留与回滚决定继续有效

## Status

Accepted。

## Context

ADR-0009 在实现前把四项相邻能力写入同一决定：可信本地 CLI 的 discovery/canary synthetic ledger、Provider metadata 被拒时的独立 redaction event、基于 typed events 的全部历史延迟，以及 credential audit 中的 opaque origin。实现和破坏性终审确认，这四项若按原文直接交付，会扩大当前切片的状态机或持久化面，且没有必要才能满足已实现的核心数据库治理与无秘密审计边界。accepted ADR 不能在代码落地后被静默改写，因此用本决定显式收紧实际支持合同。

## Decision

### 1. 可信本地 CLI 保持 `legacy_unmanaged`

Web/API 在 0004 后创建的 Run 必须冻结 active policy 并成为 `managed`。现有 `llmbenchlab-evaluate` 的 discovery、canary 与 Run 创建仍是操作者独占数据库的可信本地提前切片，保持 `legacy_unmanaged`，不伪造 synthetic operation ledger，也不宣称受 P2-05 预算保护。

把 CLI 纳入治理需要另一个垂直切片同时定义：Run 创建前 synthetic operation 的持久 owner、取消/重放、累计 global/provider/model 账本、CLI 确认文本、历史兼容和真实故障测试。在这些条件交付前，CLI 文档必须继续要求停止常规 API/Worker，并明确没有全局 RPM/TPM/USD 硬上限。

### 2. 不新增 Provider metadata redaction event

Provider request ID、returned model、system fingerprint 与 finish reason 只在通过固定字符、长度和常见凭据形态检查后进入 Response/API/report；不安全值归一化为 `null`。当前不为每个被拒字段写独立审计事件，因为 Adapter 返回合同没有携带原始字段的安全分类，额外事件会增加高基数且不能在不触碰原值的前提下证明是哪一字段。

现有 `question_evidence_persisted`、attempt ledger/audit 与 Response 的 `null` 是支持的非秘密事实。未来若需要 redaction counters，必须从 Adapter 输出显式的固定 bitset/enum，不能把 Provider 控制字符串或当前 Key 传入审计层。

### 3. Run 延迟以数据库 Run 时间戳为事实

`/tasks/history` 的 event counters 继续只聚合 allowlisted、唯一 `event_key` 的 typed audit events。queue、execution 与 end-to-end p50/p95/p99 改为直接读取同一数据库中的 `EvaluationRun.created_at/started_at/finished_at`；这些时间均由数据库时钟产生，并以观测时间落入半开 UTC 窗口。

不从 `AuditEvent.duration_ms` 重建这三类延迟，也不把缺少完整起止事实的事件推断为零。每类最多稳定读取最早 10,000 个样本，并显式返回 `sample_count/truncated`。Provider-attempt latency 与 lease-recovery duration 暂不对外提供；只有在开始/结束/删失语义和索引合同完成后才能新增。

### 4. Credential audit 不保存 origin

credential create/replace/source-switch/rejection/decrypt-failure 只允许固定 action/reason、`credential_source`、Model ID 和非秘密 key ID。origin 已由 Model 当前字段、active-Run 门禁和 opaque provider scope 参与业务裁决，但不复制到审计 payload。

这样可以避免把 Provider endpoint 作为新的长期审计数据面。若未来需要 origin 关联，只能增加基于已规范化 origin 的 opaque digest，并先定义轮换、碰撞域和迁移合同；不得写 URL 原文。

## Consequences

### Positive

- 文档、代码和验收不再把未治理的 CLI 请求描述为已受预算保护。
- Provider 控制 metadata 与 endpoint 不会为了诊断而扩大审计泄露面。
- 历史 Run 延迟使用直接、可索引、数据库时钟统一的事实，不依赖稀疏或可重放的 duration event。
- ADR-0009 的核心 per-attempt ledger、四层 admission、保守结算、公平调度和 typed counters 决定不变。

### Negative

- 可信本地 CLI 仍需要操作者独占数据库，且没有全局成本硬边界。
- 不安全 Provider metadata 只有 `null` 结果，没有按字段 redaction counter。
- 当前历史 API 不提供 Provider-attempt 或 lease-recovery latency 分布。
- Credential audit 不能直接按 endpoint origin 查询。

## Validation

- CLI/README/Architecture/Security 明确 `legacy_unmanaged` 与没有全局 RPM/TPM/USD 边界；API 新 Run 仍冻结 policy。
- Provider metadata marker Key、URL、超长/控制字符回归为 `null`，API/report/log 不反射原值。
- 主机时钟偏移测试证明 Run 与 credential audit 的持久时间来自数据库；history 测试证明巨大或损坏的 `AuditEvent.duration_ms` 不影响 Run 延迟。
- Credential 生命周期审计测试确认 payload 不含 origin、Key、ciphertext、nonce 或 keyring material。

## Security and privacy impact

本决定收窄持久化数据面，不降低 API/Worker managed Run 的 fail-closed 治理。CLI 仍只能在可信本地由明确操作者运行；它的非治理状态必须在确认、报告和运维文档中可见，不能被误用为公网或无人值守成本控制。

## Rollback or migration

无新增数据库 schema。回滚本文只改变文档会重新造成实现/承诺漂移，因此不得单独回滚；若未来交付被暂缓的能力，应新增 ADR 和迁移/兼容测试，而不是删除本决定。

## Change history

| 日期 | 变化 | 原因 |
|---|---|---|
| 2026-08-28 | Accepted | 实现终审发现 ADR-0009 四项相邻能力与已验证的最小安全合同不一致，需要显式修正而非静默改写 |
