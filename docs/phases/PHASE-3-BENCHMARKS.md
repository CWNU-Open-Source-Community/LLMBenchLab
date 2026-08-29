# Phase 3：标准 Benchmark、代码评测与数据集插件

- 状态：`in_progress`（仅可信本地客观 Benchmark 提前切片）
- 前置阶段：[Phase 2 — Reliability](PHASE-2-RELIABILITY.md)（须 `completed`）
- 后续阶段：[Phase 4 — Judge & Arena](PHASE-4-JUDGE-ARENA.md)

## 阶段目标

在来源、许可、版本和 Hash 可追溯的前提下接入 MMLU-Pro、GPQA、IFEval 等代表性 Benchmark，并用安全隔离的沙箱评测代码能力，形成可扩展数据集插件体系。

## 功能范围

- 数据集插件接口、来源/版本/License/引用元数据和缓存管理。
- MMLU-Pro、GPQA、IFEval 的许可安全导入插件与数据卡。
- 固定版本、Hash 清单、去重、分片、导入诊断和污染风险说明。
- 代码题 Schema、测试用例、编译/执行结果及分组指标。
- 无网络、最小权限、CPU/内存/时间/进程/磁盘/输出受限的代码沙箱。
- Benchmark 维度、语言、任务和子集筛选/报表。

## 非目标

- 不把禁止再分发或需要用户授权的数据直接提交到仓库。
- 不在 API 或普通 Worker 宿主环境直接执行不可信代码。
- 不实现 LLM Judge、个人 Arena、Agent 或公共多用户平台。

## 依赖

- Phase 2 的可靠 Worker、取消、超时、预算、审计和 PostgreSQL。
- 可用的容器/沙箱设施及数据集许可审查流程。
- 对代码评测协议和 Dataset Plugin 接口的 ADR。

用户在 2026-08-27 明确要求优先获得可真实模型运行的完整客观评测流程；
[ADR-0006](../decisions/ADR-0006-local-real-provider-evaluation.md) 批准在 Phase 2 尚未完成时提前交付
MMLU-Pro/GPQA 的可信本地切片。该决定不满足代码沙箱、全局预算或完整 Phase 3 的前置依赖。

## 任务拆分

| ID | 任务 | 输出 |
| --- | --- | --- |
| P3-01 | 插件与协议设计 | Dataset Plugin SDK、协议版本、ADR |
| P3-02 | 标准数据集接入 | MMLU-Pro/GPQA/IFEval 插件和数据卡 |
| P3-03 | 数据供应链 | 获取、缓存、固定版本、Hash、合规检查 |
| P3-04 | 代码题模型 | Schema、测试用例、结果与错误分类 |
| P3-05 | 安全沙箱 | 隔离执行、资源限制、默认断网、清理 |
| P3-06 | 指标与 UI | 分组聚合、子集筛选、可比性提示 |
| P3-07 | 安全和复现验证 | 沙箱红队、稳定 Hash、端到端复现 |

当前进度：P3-01 为本地转换器/协议边界的部分实现；P3-02 已接 MMLU-Pro 与 GPQA-Diamond、
尚无 IFEval；P3-03 的固定下载、缓存、源 SHA 与可复现 ZIP 已完成首个切片。P3-06 现可在 Web
选择标准 Benchmark、按数据集建议输出预算/读取超时、从主导航找回全部状态 Run，并以每页 100 条
浏览逐题证据；CLI 仍提供分组报告，但 Web 尚无分组聚合/子集指标 UI。P3-04/P3-05/P3-07 的代码与
沙箱范围未开始。该 UI 切片的完整本地门禁已通过，功能提交 `467d0243b4fb081c2d637b20ee0958c3bd6ee6d1` 已 push；
分支无 PR且精确 SHA 未触发仅监听 PR/main 的 workflow，不能称为远程绿色。

2026-08-30 的个人本地实例已把 `artifacts/benchmarks/` 中现有的三份可复现 ZIP 经正式 API 加载：
GPQA-Diamond `198` 题，以及 MMLU-Pro Direct/Official-CoT 各 `12,032` 题；默认库连同 Demo 共
`4` 个 Benchmark、`24,277` 题。第三方题目和导入前备份继续位于 Git 忽略目录，本次没有重新下载、
运行评测或调用 Provider；记录 commit `0163b67c00eb59ae59db5f3adb679ad85c799142` 的精确 SHA CI
run `33266167547` 已 4/4 成功。这仍不代表 IFEval、Plugin SDK、沙箱、分组 UI 或本阶段验收已经完成。

## 验收标准

- [ ] 每个标准 Benchmark 可从固定来源/版本重复导入并得到相同 Hash。
- [ ] License、引用、数据卡、获取步骤、分发限制和污染风险可见。
- [ ] 插件隔离清晰，坏数据可定位到文件/记录且不会破坏已导入版本。
- [ ] 代码在专用隔离环境执行，默认无网络且强制全部资源上限。
- [ ] 超时、fork bomb、磁盘/输出耗尽、恶意系统调用等路径有验证。
- [ ] 代码结果保留编译、stdout/stderr、测试状态和沙箱错误的受限快照。
- [ ] 分组指标计算正确，不同协议/数据版本不会无提示混排。
- [ ] 核心 Mock 离线回归测试继续通过。

## 风险

| 风险 | 应对 |
| --- | --- |
| 许可或来源条款变化 | 运行时获取、保留来源证明，上线前复核，不打包受限数据 |
| 训练数据污染 | 数据卡披露、版本/时间戳、后续私有与 Live Benchmark |
| 沙箱逃逸 | 专用隔离边界、最小权限、断网、资源限制和安全测试 |
| 大数据集占用存储/带宽 | 分片、缓存配额、校验后复用和显式清理 |
| 长推理输出不足或 Provider 较慢 | 按 Benchmark 给出可调输出/读取超时建议，保留 `output_truncated` 证据；Provider 默认不是无限且仍需费用控制 |
| 指标聚合掩盖子群差异 | 默认展示总体与分组结果及样本数 |

## 交付物

- Dataset Plugin SDK 与 MMLU-Pro、GPQA、IFEval 插件。
- 数据卡、License/引用说明、固定版本与 Hash 清单。
- 代码评测协议、沙箱执行器、安全测试和操作手册。
- 分组指标 API/UI 及更新后的协议、安全、部署文档。

## 状态

`in_progress`。仓库不再只有计划：MMLU-Pro 与 GPQA-Diamond 的固定来源转换、可信本地运行和
报告切片已经实现，但不会提交第三方题目；Web 已有标准数据选择、建议配置、Run 列表和分页证据的
已 push 的当前切片。IFEval、通用 Plugin SDK、代码题/沙箱、分组/子集完整 UI 与红队验收仍缺失；
本轮精确 SHA 没有 workflow run，且其余范围仍缺失，故本阶段不得标为 `completed`。
