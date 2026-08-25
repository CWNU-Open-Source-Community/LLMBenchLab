# [任务名称] 执行计划

> 使用说明：复杂任务应遵循仓库根目录 `PLANS.md`。复制本模板到任务约定位置，填写所有字段；不适用项要说明理由。实施期间持续更新状态与证据。

- Owner: [负责人/代理]
- Status: draft | active | blocked | completed
- Created: YYYY-MM-DD
- Updated: YYYY-MM-DD
- Related requirements: [如 FR-RUN-01]
- Related phase: [相对链接]
- Worklog: [相对链接]
- ADRs: [相关 ADR；无则写“无”]

## Context

[描述当前真实状态、触发任务的原因、已有行为和必须保留的用户改动。]

## Objective

[用一句话描述可验证结果；补充完成后用户能执行的操作。]

## Scope

- [包含的模块、接口、数据和用户行为]
- [允许修改的文件或边界]

## Non-goals

- [明确不做的能力；说明将在哪个阶段处理或为何排除]

## Assumptions

| 假设 | 依据 | 验证方法 | 不成立时的处理 |
|---|---|---|---|
| [假设] | [来源] | [检查] | [调整/停止条件] |

## Requirements

- [ ] [需求编号]：[可判定的要求]
- [ ] [安全、兼容或性能约束]

## Implementation steps

1. [pending] **[步骤名称]**
   - 修改范围：[模块/文件]
   - 操作：[足够让接手者执行的具体说明]
   - 完成判据：[命令或可观察结果]
2. [pending] **[步骤名称]**
   - 修改范围：[模块/文件]
   - 操作：[具体说明]
   - 完成判据：[具体证据]

状态只使用 `pending`、`in_progress`、`completed`，同一时刻最多一个 `in_progress`。

## Risks

| 风险 | 可能性 | 影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|---|
| [风险] | 低/中/高 | 低/中/高 | [措施] | [处置] |

## Validation

| 验收项 | 命令/检查 | 预期结果 | 实际结果与证据 |
|---|---|---|---|
| [行为] | `[命令]` | [退出码/断言] | 待执行 |
| 秘密与无关改动检查 | `git diff --check`、`git status --short` 及敏感词检查 | 无格式错误、无密钥、范围正确 | 待执行 |

## Rollback

[说明功能、配置、依赖和数据迁移如何回退；写明备份点和不可逆风险。没有持久化变化时说明“不涉及数据回滚”。禁止使用会丢失用户工作的命令。]

## Documentation updates

- [ ] 相关 README / API / Architecture / Protocol / Dataset / Security / Testing / Deployment
- [ ] 必要 ADR 与迁移说明
- [ ] `CHANGELOG.md`
- [ ] `docs/PROJECT_STATUS.md` 与当前 Phase 文档
- [ ] `docs/NEXT_TASK.md` 与本次工作日志

## Completion evidence

- 修改文件：[最终清单]
- 实际命令：[命令、退出码、测试数/失败数]
- 验收对应：[每个 Requirements 条目到证据的映射]
- 未运行：[命令、原因、用户后续命令]
- 已知问题：[具体限制及影响]

## Decision and discovery log

| 日期时间 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| YYYY-MM-DD HH:MM TZ | decision / discovery / deviation | [事实与原因] | [计划调整或 ADR] |

