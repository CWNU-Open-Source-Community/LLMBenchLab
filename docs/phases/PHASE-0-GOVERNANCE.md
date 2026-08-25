# Phase 0：项目治理和架构

- 状态：`completed`
- 完成日期：2026-08-24
- 前置阶段：无
- 后续阶段：[Phase 1 — MVP](PHASE-1-MVP.md)

## 阶段目标

把产品愿景转化为可执行、可验收、可持续维护的工程基线。任何后续开发者都应能从治理文档中判断“做什么、不做什么、如何验证、何时算完成”。

## 功能范围

- 产品边界：Charter、用户、核心价值、成功指标和非目标。
- 需求基线：功能、非功能、数据、复现、性能、安全和 MVP 验收条件。
- 架构基线：模块、数据流、Run 生命周期、Adapter/Evaluator 扩展点和已知限制。
- 评测基线：`llmbenchlab-protocol-v1`、公平参数、评分、Hash、版本和可比性。
- 工程治理：`AGENTS.md`、`PLANS.md`、ADR、模板、Roadmap、Project Status、Next Task 和工作日志。
- 开源基线：测试、部署、安全、贡献与 GitHub 工作流文档。

## 非目标

- 不交付 Phase 1 的后端、前端或评测运行能力。
- 不设计生产级分布式架构、多用户、商业计费或公开托管。
- 不导入大型第三方数据集，不调用真实付费模型。

## 依赖

- 用户提供的完整 MVP 规格。
- 仓库初始勘察与 Git 状态记录。
- 对第三方依赖、数据集许可和公开部署风险的保守假设。

## 任务拆分

| ID | 任务 | 完成证据 |
| --- | --- | --- |
| P0-01 | 记录初始仓库状态、范围、假设与风险 | Bootstrap 工作日志 |
| P0-02 | 定义 Charter、Requirements 与 MVP 验收 | 对应文档可审阅 |
| P0-03 | 定义系统架构、数据流与扩展接口 | Architecture 与 Mermaid 图 |
| P0-04 | 定义评测协议、数据格式与 API 契约 | Protocol、Dataset、API 文档 |
| P0-05 | 记录技术栈、SQLite、协议、密钥管理决定 | ADR-0001 至 ADR-0004 |
| P0-06 | 建立 Roadmap、Phase 0–6 和阶段门槛 | Roadmap 与本目录文档 |
| P0-07 | 固化持续开发与 Definition of Done | `AGENTS.md`、`PLANS.md`、模板 |
| P0-08 | 建立状态、变更、下一任务和日志追踪 | Status、Changelog、Next Task、Worklog |

## 验收标准

- [x] 项目目标、目标用户、核心价值、边界和非目标明确。
- [x] Requirements 覆盖功能、非功能、数据、复现、性能和安全。
- [x] Architecture 覆盖上下文、模块、数据流、Run 生命周期、持久化、错误和扩展方式。
- [x] Benchmark Protocol 定义版本、公平参数、严格总分、完成率、回答准确率、Hash 和可比性。
- [x] Roadmap 完整，Phase 0–6 每阶段均包含九个必需部分。
- [x] `AGENTS.md` 固化任务前、中、后的流程和 Definition of Done。
- [x] `PLANS.md` 定义复杂任务执行计划及完成证据。
- [x] 主要架构决定由 ADR 记录，状态和后果明确。
- [x] Project Status 反映真实状态并指向工作日志和下一任务。
- [x] 文档不把 Phase 1 计划描述为已交付能力。

## 风险

| 风险 | 应对 |
| --- | --- |
| 文档与实现逐渐漂移 | 行为变化必须同步文档；阶段结束前执行状态一致性检查 |
| 架构过早冻结 | ADR 允许被后续 ADR 取代；优先稳定接口语义而非实现细节 |
| 计划被误读为完成 | 只按可复核证据更新状态；未验证项保持未完成 |
| Roadmap 诱发 MVP 过度设计 | 后续能力明确放入 Phase 2–6，Phase 1 只做垂直切片 |

## 交付物

- 产品与架构：`PROJECT_CHARTER.md`、`REQUIREMENTS.md`、`ARCHITECTURE.md`。
- 协议与契约：`BENCHMARK_PROTOCOL.md`、`DATASET_FORMAT.md`、`API.md`。
- 工程规范：根目录 `AGENTS.md`、`PLANS.md`，以及 `docs/templates/`。
- 决策记录：`docs/decisions/ADR-0001` 至 `ADR-0004`。
- 追踪体系：`ROADMAP.md`、`PROJECT_STATUS.md`、`NEXT_TASK.md`、`CHANGELOG.md` 和工作日志。

## 状态

`completed`。Phase 0 文档体系于 2026-08-24 建立完成。后续若实现与基线不一致，须在相关任务中更新 ADR 和文档，但不能用文档状态替代实现验证。

