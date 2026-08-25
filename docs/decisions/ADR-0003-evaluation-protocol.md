# ADR-0003：采用版本化、严格分母的客观评测协议

- **Date**: 2026-08-24
- **Deciders**: LLMBenchLab maintainers
- **Scope**: `llmbenchlab-protocol-v1`

## Status

Accepted

## Context

模型调用可能超时、返回空文本或生成无法唯一解析的答案。如果只在成功回答的问题上计分，失败率高的模型会获得虚高成绩。与此同时，用户需要区分“模型请求是否完成”“可评答案是否正确”和“所有计划题的总体表现”。

可复现比较还要求固定数据集身份、Prompt、Evaluator、生成参数和执行策略。不同数据版本或评分逻辑的结果若被无提示混合，会让排行榜失去意义。MVP 只实现 exact match、multiple choice 和 numeric 等可确定客观评分，不具备可靠的 LLM Judge 或代码沙箱。

## Decision

发布 `llmbenchlab-protocol-v1`，并做出以下规范性决定：

1. 每题基础分只能是 0 或 1。
2. 严格总分 `score = 100 × 正确题数 / 所有计划题数`。请求错误、空回答、解析失败和未完成题均为 0。
3. `completion_rate = 成功获得非空模型响应的题数 / 所有计划题数 × 100`，存储为 0–100。
4. `answered_accuracy = 正确题数 / 可唯一解析并完成评分的题数 × 100`，存储为 0–100；分母为 0 时返回 `null`。
5. 排行榜只纳入 `completed` Run，默认按严格总分排序；Demo 结果显著标记，不代表正式能力。
6. Exact Match v1 只做声明的空白/大小写规范化；Multiple Choice v1 优先明确最终答案并拒绝冲突；Numeric v1 安全解析有限数值，使用绝对/相对 tolerance，禁止 `eval`。
7. 每个 Run 快照 protocol、Benchmark ID/version/hash、Evaluator 名称/版本/配置、Prompt/system prompt、生成参数、模型/adapter、Git SHA、并发、超时、重试、时间与价格。
8. Dataset Hash 使用项目定义的 JCS 风格紧凑 JSON、UTF-8、LF 和原始题序计算 SHA-256；完整算法由 `BENCHMARK_PROTOCOL.md` 与 `DATASET_FORMAT.md` 固化。
9. 只有协议版本、数据集身份、Prompt、Evaluator 和执行配置相同的 Run 才标记为直接可比。排行榜按协议和数据集 Hash 分区，不无提示混合。

解析答案与原始回答分开保存，标准答案按 Response 快照。单题错误不终止 Run，但必须保存分类与脱敏原因。

## Alternatives

### 只报告成功答案准确率

对供应商暂时故障较宽容，但会奖励低完成率，无法代表用户实际得到的端到端能力。v1 保留 answered accuracy 作为诊断指标，不把它作为默认榜单分数。

### 对请求失败从分母中剔除

能减少网络波动影响，却让不同 Run 的有效题集不同，且可被失败选择性扭曲。严格分母更容易解释和复现。

### 使用部分分或模糊语义匹配

可能更符合开放式问题，但阈值和模型 Judge 引入额外主观性、成本和不确定性。MVP 只接受确定性 0/1 Evaluator。

### 立即引入 LLM-as-a-Judge

可覆盖开放回答，但需要评审模型、Judge Prompt、位置偏差、重复采样和成本控制，超出 Phase 1，也违背 CI 不调用真实模型的约束。

### 允许跨协议统一排行榜

界面更简单，但同名指标可能具有不同语义，会产生误导性排序，因此拒绝。

## Consequences

### Positive

- 严格总分把可靠性纳入成绩，completion rate 和 answered accuracy 又能定位失败来源。
- 完整快照与 Dataset Hash 支持事后审计、重复运行和漂移识别。
- 确定性 Evaluator 可离线测试，CI 不需要付费 API。

### Negative

- 短暂供应商故障会降低严格总分；用户需要重跑并比较失败率，而不是把错误题排除。
- 公共题目仍存在训练污染，Hash 只能证明输入一致，不能证明模型未见过题目。
- 上游可能忽略 seed 或在相同模型名下滚动更新，因此协议快照不能保证完全确定性。
- 协议或 Hash 算法修复可能产生新的比较分区，旧成绩不能自动合并。

### Follow-up

- Phase 3 为代码和更复杂公共 Benchmark 定义独立、版本化协议。
- Phase 4 的 LLM/Pairwise Judge 必须另写 ADR，记录 Judge 模型、Prompt、顺序偏差、重复采样和成本。
- Phase 5 的 Private/Live Benchmark 必须保持每次 Run 引用不可变 revision 与 Hash。
