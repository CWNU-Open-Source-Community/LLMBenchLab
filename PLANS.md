# LLMBenchLab 执行计划规范

复杂任务必须使用可持续更新的执行计划。计划不是一次性的愿望清单，而是让不了解上下文的后续协作者能够安全继续工作的事实记录。

## 何时必须建立计划

满足任一条件时使用本规范：跨后端与前端、涉及数据库迁移、改变公共 API 或 Benchmark 协议、引入生产依赖、修改安全边界、包含三个以上可独立验证步骤，或预计跨越多个工作时段。单文件低风险修订可以只在工作日志中列步骤。

计划应保存在任务约定的位置；没有约定时可放入工作日志的“执行计划”章节。所有计划遵守 `AGENTS.md`。

## 维护规则

- 开始实施前填写 Context 到 Validation，避免只有标题的空壳。
- 步骤状态只使用 `pending`、`in_progress`、`completed`；同一时刻最多一个步骤为 `in_progress`。
- 每完成一步，写入结果和证据；遇到偏差立即更新 Assumptions、Risks 或 Implementation steps。
- 新发现、失败尝试和范围调整要保留原因，不要改写历史制造“从未偏离”的假象。
- 架构决定放入 ADR，并在计划中链接；计划本身不替代 ADR。
- 完成声明必须引用命令输出、测试报告、API 响应、截图或可定位文件等证据。

## 执行计划模板

复制以下结构并替换方括号内容。无法适用的字段写明“不适用”及理由，不能直接删除。

```markdown
# [任务名称] 执行计划

- Owner: [负责人或代理]
- Status: draft | active | completed | blocked
- Created: YYYY-MM-DD
- Updated: YYYY-MM-DD
- Related phase: [阶段文档链接]
- Worklog: [工作日志链接]
- ADRs: [相关 ADR；无则写“无”]

## Context

[当前系统状态、触发原因、相关历史决定和需要保留的已有行为。]

## Objective

[一句可验证的结果，以及完成后用户能做什么。]

## Scope

- [本任务明确包含的模块、接口和行为]

## Non-goals

- [明确不做的内容及边界]

## Assumptions

- [假设、来源、若假设不成立的影响和验证办法]

## Requirements

- [关联 REQUIREMENTS.md 的编号及本任务补充约束]

## Implementation steps

1. [pending] [步骤与预期产物]
   - Files/modules: [位置]
   - Validation: [本步完成判据]
2. [pending] [步骤与预期产物]
   - Files/modules: [位置]
   - Validation: [本步完成判据]

## Risks

| 风险 | 可能性/影响 | 预防措施 | 触发后的处理 |
|---|---|---|---|
| [风险] | [低/中/高] | [措施] | [处置] |

## Validation

| 验收项 | 命令或检查 | 预期结果 | 实际结果 |
|---|---|---|---|
| [要求] | `[命令]` | [可判定结果] | 待执行 |

## Rollback

[说明如何停止新行为、回退数据/迁移/配置，以及如何保护已有数据。若不可逆必须突出说明。]

## Documentation updates

- [ ] README / 用户操作说明
- [ ] API / 数据格式 / Benchmark 协议（按适用项）
- [ ] Architecture / Security / ADR（按适用项）
- [ ] CHANGELOG、PROJECT_STATUS、阶段文档、NEXT_TASK、工作日志

## Completion evidence

- Changed files: [最终文件清单]
- Commands run: [命令、退出码、测试数]
- Acceptance evidence: [逐项对应验收标准]
- Not run: [未运行项目、原因、后续命令]
- Known issues: [遗留问题]

## Decision and discovery log

| 日期 | 类型 | 记录 | 影响/后续 |
|---|---|---|---|
| YYYY-MM-DD | decision / discovery / deviation | [事实] | [影响] |
```

## 完成判定

只有 Objective 和 Scope 已兑现、Validation 有实际结果、Rollback 与文档联动已处理、Completion evidence 可复核时，计划状态才能改为 `completed`。外部条件阻塞时保留已完成证据，将状态设为 `blocked` 并精确写明恢复条件；不能以“代码已写”代替交付完成。

