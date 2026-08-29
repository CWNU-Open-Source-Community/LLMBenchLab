# ADR-0017：以 schema-equivalent 前向 revision 修复早期治理索引缺口

- **Status**: Accepted
- **Date**: 2026-08-30
- **Deciders**: LLMBenchLab maintainers
- **Scope**: 早期 `20260827_0004` 本地数据库兼容修复、audit archive head 兼容与 P2-07 recovery head
- **Amends**: [ADR-0015](ADR-0015-observability-worker-progress-audit-retention.md) 的 archive-v1 compatible-head allowlist，以及 [ADR-0016](ADR-0016-postgresql-keyring-recovery-and-redis-rebuild.md) 的 P2-07 exact recovery head
- **Amended by**: [ADR-0018](ADR-0018-observational-token-estimates-are-not-hard-reservations.md) 将 data-only `0007` 加入 archive allowlist，并把尚未实施的 P2-07 exact recovery head 更新为 `20260830_0007`
- **Preserves**: canonical 数据模型、13 表/importer、audit archive v1 字段语义、`llmbenchlab-protocol-v1`、未知 drift fail-closed 与数据库外 keyring

## Context

一个重建前 SQLite 备份完整性与外键均正常，Alembic marker 为 `20260827_0004`，但精确缺少三个后来出现在 canonical `0004` 文件中的索引：

- `evaluation_runs.ix_evaluation_runs_started_at_id`；
- `evaluation_runs.ix_evaluation_runs_finished_at_id`；
- `governance_policies.uq_governance_policies_single_active`。

因此 migration preflight 正确识别到 revision/schema 不一致，并在 `0005` 有机会运行前拒绝。删库重建会绕过而不是解决兼容问题；修改已经发布的 `0004/0005` 又会继续制造同 revision 多种含义。现有 ORM metadata、13 张应用表、archive event schema 和恢复摘要字段都不需要变化。

[ADR-0016](ADR-0016-postgresql-keyring-recovery-and-redis-rebuild.md) 已把尚未实施的 recovery-manifest-v1 精确绑定到当时的 head `0005`，并要求未来 head 通过显式决定更新。本 ADR 是该显式 amendment。

## Decision

1. 新增前向 Alembic revision `20260829_0006`，`down_revision=20260828_0005`。它不增加业务表、字段或约束，只在缺失时补齐上述三个 canonical `0004` 索引。migration 会拒绝 reflection-visible 列序、唯一性或双方言 partial predicate 不匹配的同名索引；SQLite `DESC`/`COLLATE` 等反射不完整的 DDL modifier 继续由标准入口中先执行的 deep preflight 拒绝。
2. SQLite migration preflight 只对白名单 versioned `0004/0005` fingerprint 放行：schema 可以是 canonical，或仅缺这三个索引的任意非空子集。这样 SQLite 非事务 DDL 在 `0006` 内中断后可从 `0005` marker 重入；完整 schema、DDL modifiers、integrity、FK、已存在 repair index 的精确定义和其他索引仍严格匹配，任何额外 drift 都拒绝。
3. 新近成为 historical 的 PostgreSQL `0005` 在 preflight 中必须重新执行 metadata diff；只接受 canonical schema 或同一 repair-index 缺失子集，不能因 revision 已知而绕过 drift 校验。SQLite preflight 与 `0006` migration 都在首条 repair DDL 前确认 active policy 不超过一条；双方言直接执行 migration 也受同一数据 guard 保护。工具绝不自动挑选或停用 policy；重复 active 数据必须由操作者核对后另行处理。
4. `0006 -> 0005` 只回退 revision marker并保留三个索引，因为它们本来就属于 canonical `0004/0005` schema。`0005 -> 0004` 的 Worker facts guard 与 `0004 -> 0003` 的 governance/audit guard 不变。
5. Audit archive v1 compatible-head allowlist 同时保留 `0005` 并加入 schema-equivalent `0006`；冻结的 `0005` archive fixtures 与 digest 不改写。`0006` 没有改变任何 archive event、retention 或 restore 语义。
6. P2-07 尚未实施，因此 recovery-manifest-v1 的 exact source/target head 从 `0005` 显式修订为当前 `0006`，不建立多 head 恢复 allowlist。历史勘察中“当时 head 为 `0005`”仍作为事实保留。

## Consequences

- 受影响旧库可由 `make migrate` 在一致性备份后无损前进，不再要求删库或 blind stamp；标准 `0004/0005` 数据库在 `0006` 中为 schema no-op。
- 当前应用启动仍只接受唯一 Alembic head。升级代码后必须先由唯一 migration owner 运行 `make migrate`；API/Worker 不自行迁移。
- 支持的 migration owner 入口始终先执行 preflight，再执行 Alembic。migration 内的 active-policy 与 reflection-visible 同名索引门禁是纵深保护，不替代 SQLite preflight 对 `DESC`/`COLLATE` 等深层 DDL modifier 的检查；裸 `alembic upgrade` 绕过 preflight 不属于受支持的旧库修复入口。
- 这不是新的产品 schema、API、Benchmark、评分协议、数据表或安全授权模型，也不推进 P2-07 功能实现。
- 本 ADR 的 `0006` 索引修复事实与回滚边界保持不变；当前唯一 head 已由 ADR-0018 前进为 `0007`，不能把本 ADR 中“当前 `0006`”的历史措辞当作新的运行门禁。

## Rejected alternatives

- **删除并重建数据库**：会丢失或放弃旧事实，且无法修复下一个同类环境。
- **修改既有 `0004` 或 `0005`**：继续让一个 revision 代表多个历史结构，无法修复已经位于 `0005` 的缺口数据库。
- **在 preflight 中直接执行 repair DDL**：绕过 Alembic 的前向 schema owner，并且不能一致覆盖 PostgreSQL 或 migration 自身的纵深数据门禁。
- **放宽通用 fingerprint**：会把未知 drift 误当兼容结构，违反 fail-closed 边界。

## Validation

- migration 回归必须覆盖 `0004 gap -> 0005 -> 0006`、`0005` 中断重入、数据计数保持、partial unique enforcement、重复 active policy、同名错误索引、额外 drift、canonical no-op 与 downgrade marker 行为。
- 真实失败备份只在临时副本上执行 prepare/upgrade/check，并核对 revision、integrity、FK、三个索引和匿名表计数；原件不修改。
- 本地 SQLite、双方言 CI migration/check、完整测试、lint、Mock smoke、Compose config 和精确 SHA 远程 job 共同构成交付门禁。
