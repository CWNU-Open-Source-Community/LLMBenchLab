# ADR-0011：明确 pre-send release 不消耗 Provider retry

- **Status**: Accepted
- **Date**: 2026-08-28
- **Deciders**: LLMBenchLab maintainers
- **Scope**: managed Run 的 Provider attempt ordinal、question execution generation 与 `released_pre_send`
- **Related requirements**: `docs/NEXT_TASK.md` P2-05
- **Amends**: [ADR-0009](ADR-0009-database-governance-audit-fair-scheduling.md) 第 3 节的 generation 规则；其他 ledger、fencing、结算与 cooperative yield 决定不变

## Context

ADR-0009 要求每个实际 Provider HTTP attempt 都消耗有限 retry ordinal，同时规定已 `reserved` 但尚未成功提交 `send_started` 的调用可以 `released_pre_send`。实现终审复现了两条规则之间的冲突：`reserve` 为保证唯一 ledger 先递增 ordinal；若 fixed-window 在 `mark_send_started` 时刚好饱和，Adapter 会确认没有进入 HTTP transport 并释放 reservation，但持久 cursor 已前进。`max_retries=0` 时，一次零 HTTP 的本地 defer 就能让题目在恢复时被错误判定为 retry exhausted。

终态 ledger 不能删除或改回 active，也不能在同一 `(run, question, generation, ordinal)` 上创建第二条 reservation。因此，单纯把 cursor ordinal 减一会与唯一键和 never-delete 证据冲突。

## Decision

- `provider_attempt` 只统计已经成功提交 `send_started`、或其 send-start 结果无法确认的 Provider HTTP attempt。
- 当且仅当 controller 已确认 reservation 仍为 `reserved`，并成功提交 `released_pre_send` 时，同一事务把该题的 `QuestionExecution` 开始一个新的本地 ledger generation：`execution_generation += 1`，`next_provider_attempt` 保持为本次未发送的 ordinal。此前已经 `send_started` 的较小 ordinal 仍被消耗；若本次是 attempt 1，则恢复后仍为 attempt 1。首次实际 attempt 与 retry-not-before 游标保持不变。
- 原 `released_pre_send` ledger 保持终态、保留旧 generation/ordinal，不删除、不重用；新 generation 使下一次 reservation 取得新的逻辑唯一键，但仍从 HTTP attempt 1 开始。
- 该 generation 变化不是 Run 失败，不增加 `failed_attempt_count`，也不改变 protocol-v1 的 `max_retries + 1` 实际 HTTP 上限。
- lease-loss reconciler 释放旧 token 的 `reserved` row 时不再次推进 generation，因为 lease takeover 已按 ADR-0009 开始新的 execution generation；重复终态调用保持幂等。
- 普通 cooperative question-quantum yield、尚未 reserve 的 rate/concurrency defer，以及已 `send_started` 的 actual/conservative settlement 继续沿用 ADR-0009 原 generation/ordinal 规则。若旧 Worker 先完成 pre-send release、随后 Run lease takeover 又开始恢复 generation，generation 可以再单调前进一次；ordinal 仍只反映实际 HTTP 消耗，不要求 generation 连续或只增一次。

## Consequences

### Positive

- 零 HTTP 的本地 admission/window 竞争不会耗尽 Provider retry。
- never-delete ledger 与逻辑唯一键保持完整，仍能解释每次 reserve/release。
- 实际 HTTP retry 次数和 protocol-v1 配置重新一一对应。

### Negative

- `execution_generation` 同时表达 Run 失败恢复和已确认 pre-send abandon；诊断时必须结合旧 ledger 的 `released_pre_send` outcome 区分原因，且不能把 generation 数直接当作 HTTP retry 数。
- 高频边界竞争可能产生多个只含 released row 的 generation；这是保留安全证据的有意成本。

## Validation

- `max_retries=0`：第一次 reserve 后在 mark-send 前释放，恢复 context 必须为新 generation 的 attempt 1，且下一次 reserve 成功。
- 已实际发送 attempt 1 后，attempt 2 在 mark-send 前释放：恢复 context 必须为新 generation 的 attempt 2，不能把实际 HTTP retry 预算重置到 attempt 1。
- released row 保持终态且唯一；重复 finish 不再次推进 generation。
- `send_started` 后失败继续消耗 ordinal并保守结算，下一 retry 使用同 generation 的下一 ordinal。
- lease takeover 对旧 `reserved` row 的 reconciliation 保持幂等；generation 允许因 pre-send release 与随后 Run 恢复分别单调前进，但 HTTP ordinal 不回退到已发送的 attempt。

## Security and privacy impact

没有新增 payload、URL、Provider metadata 或 credential 字段。generation/ordinal 仍是内部非秘密关联事实。该决定收紧成本治理，不把未发送请求误算为 Provider 消费，也不放宽任何 `send_started` 后的保守结算。

## Rollback or migration

无 schema 变化。旧环境中已经错误消耗 ordinal 的 released row 不自动改写；升级后下一次明确 pre-send release 使用新规则。回滚代码会重新引入零 HTTP exhaustion，因此不得在保留 managed Run 执行的情况下单独回滚。
