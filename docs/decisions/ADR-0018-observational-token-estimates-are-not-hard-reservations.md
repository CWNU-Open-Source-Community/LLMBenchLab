# ADR-0018：观测 Token 估算不得作为治理硬预留

- **Status**: Accepted
- **Date**: 2026-08-30
- **Deciders**: LLMBenchLab maintainers
- **Scope**: managed Run 的 Provider attempt reservation、overdraw 派生语义与历史物化修复
- **Amends**: [ADR-0009](ADR-0009-database-governance-audit-fair-scheduling.md) 第 3、4 节的 input reservation / overdraw 语义
- **Also amends**: [ADR-0015](ADR-0015-observability-worker-progress-audit-retention.md) 的 archive-v1 compatible-head allowlist，以及 [ADR-0016](ADR-0016-postgresql-keyring-recovery-and-redis-rebuild.md) 的 P2-07 exact recovery head
- **Preserves**: never-delete attempt ledger、Provider actual usage、显式 hard Token/cost fail-closed、四层 scope、`llmbenchlab-protocol-v1`

## Context

Runner 在 Run 未提供 `input_token_reservation` 时，用渲染消息 UTF-8 字节数除以四得到观测估算。该函数明确声明结果不是 hard bound，但实现仍把估算写入 `provider_call_reservations.reserved_input_tokens`，并在 Provider actual input usage 大于估算时把 global/provider/model/run 四层 scope 全部标记为 `overdrawn`。

真实 OpenCode Go `hy3` Run 暴露了该冲突：冻结 policy 和 Run override 没有任何 hard request/Token/cost 限制，第七个成功 attempt 的 input estimate/reservation 为 59，Provider actual 为 75。账本正确保存为 `settled_actual`，但下一题仍因共享 global scope 的 `overdrawn` 永久终止。前端又把所有 `overdrawn` 统一描述为“保守结算超额”，进一步误导为 Provider 额度或 conservative settlement 问题。

直接修改 actual usage、删除 ledger 或仅清除 materialized scope flag 都会破坏事实或在下一次 ledger 重算时失败；仅切换 policy 或新建 Run 也不能解除共享 global scope。

## Decision

1. `input_token_reservation` 只有在 Run 明确提供该字段时才是 hard reservation。没有显式值时，UTF-8/tokenizer 估算最多用于观测，不写入 attempt 的 `reserved_input_tokens`，也不参与 reserved cost 或 overdraw 裁决。
2. Provider actual input usage 始终原样保存。只有显式 input reservation 非空且 actual input 超过它时，input 维度才派生 `overdrawn`。由 input reservation 参与计算的 cost overdraw 同样要求显式 input reservation；显式 `max_tokens` 形成的 output reservation 继续独立参与 output overdraw。
3. 新增数据 revision `20260830_0007`。它不改表结构或 never-delete ledger，只按修正后的派生规则重算 `governance_scopes.overdrawn`；downgrade 按旧规则重算，使旧应用不会遇到 materialized/ledger 漂移。upgrade/downgrade 都拒绝 active Provider reservation，要求唯一 migration owner 在停止 API/Worker 后执行。
4. Runtime 的 scope ledger 重算使用同一规则：历史 managed reservation 通过关联 `evaluation_runs.input_token_reservation` 判断 input/cost reservation 是否显式；没有 Run 的内部 synthetic reservation 仍把调用者提供的 reservation 视为显式。
5. 不增加数据库列或改写历史 reservation 数值。历史 estimate 59 与 actual 75 继续保留为审计事实，只是不再被解释成 hard overdraw。
6. 前端 `overdrawn` 文案改为“实际用量曾被判定超过预留”，不再声称一定发生 conservative settlement；中性历史措辞也不会把升级前保留的误判终态描述成显式 hard reservation。
7. `20260830_0007` 不改变 audit event/archive schema，因此加入 archive-v1 compatible-head allowlist；P2-07 尚未实施，其 exact recovery head 从 `0006` 修订为 `0007`。

## Consequences

- 无限 policy 下不同 tokenizer/chat template 的正常估算偏差不再永久锁死 managed Provider 流量。
- 启用 hard TPM/Token/cost 时仍要求显式 input reservation、有限 `max_tokens` 和适用价格；Provider actual 超过这些可证明边界时仍 fail closed。
- 迁移保留所有历史 Response、actual usage、reservation、audit 与 Run 终态；已失败 Run 不改写为 completed，修复后需要新建 Run。
- 当前数据库必须先停止 API/Worker、执行受支持的 migration 入口，再重启。直接 `UPDATE governance_scopes`、删除 reservation 或伪造 actual usage 均不受支持。

## Rejected alternatives

- **提高 UTF-8 估算倍率**：任意通用倍率仍不是 Provider tokenizer/chat template 的可证明上界，只会延后同类误判。
- **新建 Run 时设置更大 reservation 即可**：既有 global scope 已 overdrawn，新 admission 会先被共享 scope 拒绝。
- **直接清除 scope flag**：materialized flag 会在下一次 ledger 校验时与旧派生规则冲突，且没有解释历史事实。
- **删除或修改历史 ledger/actual usage**：破坏账单与审计证据，违反 never-delete 合同。
- **增加 reservation provenance 列**：可以表达来源，但本次可由 Run 冻结的显式 override 无歧义推导；新增 schema 列会扩大迁移、importer 和 API 维护面。

## Validation

- Unit：无显式 input reservation 时 actual 大于估算不 overdraw，账本 input reservation 与 reserved cost 为 `null`；显式 input/output reservation 超额，以及由完整显式上界与价格计算的 reserved cost 超额，仍 overdraw 并阻止后续 attempt。
- Migration：SQLite 与 PostgreSQL 的 `0006 -> 0007` 对历史 observational input overdraw 清除 materialized flag但保留 ledger/actual；显式 reservation overdraw 保留；downgrade 恢复旧派生结果；active reservation 在更新前拒绝。
- Runtime：scope materialization 从 never-delete ledger 按新规则重算且无 drift；现有本地数据库迁移后四层 overdrawn 清零，原 Run/7 Responses/407+599 Token 事实不变。
- Frontend：Run detail 对 `*_overdrawn` 显示准确的新文案。
- 全部自动化只使用 Mock/fixture，不调用真实 Provider。
