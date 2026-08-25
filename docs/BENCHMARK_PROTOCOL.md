# Benchmark 评测协议

本文定义 LLMBenchLab 客观题评测的可复现语义。实现、测试、API 和排行榜都必须遵守本协议；若任何会影响模型输入、答案解析、计分分母或结果筛选的语义发生变化，必须发布新的 `protocol_version`，不得覆盖旧 Run。

## 协议标识

当前版本：

```text
llmbenchlab-protocol-v1
```

版本字符串是持久化快照的一部分。`v1` 内允许不改变结果语义的修复，例如文案、额外诊断字段和性能优化；以下变化属于破坏性协议变化，至少升级为新的协议版本：

- Prompt 渲染或默认 system prompt 改变；
- 默认生成参数、重试语义或并发语义改变；
- 答案提取、规范化、tolerance 或错误题计分改变；
- `score`、`completion_rate`、`answered_accuracy` 的分母改变；
- 数据集规范化与 Hash 算法改变；
- 排行榜纳入 Run 的条件改变。

## 一次 Run 的冻结输入

创建 Run 时完成所有外键校验，并保存以下不可变快照：

| 类别 | 必须快照的字段 |
| --- | --- |
| 协议 | `protocol_version` |
| Benchmark | ID、name、version、Dataset SHA-256、计划题数 |
| Evaluator | 每种题型的 Evaluator 名称与版本、逐题 `evaluator_config` |
| Prompt | `prompt_template`、最终 system prompt；choices 的确定性渲染规则 |
| 生成 | temperature、top_p、max_tokens、seed |
| 模型 | 展示名称、`remote_model_name`、adapter 类型、有效模型参数 |
| 执行 | concurrency、连接/读取超时、最大尝试次数、退避策略 |
| 成本 | 输入/输出每百万 Token 单价及币种假设 |
| 代码 | 创建 Run 时的 Git commit SHA；无法取得时为 `null` |
| 时间 | Run 创建、开始和结束的 UTC 时间 |

模型记录或 Benchmark 后续被编辑，不得改变已经创建的 Run。真实密钥值绝不进入快照；只允许保存 `api_key_env` 的变量名。`reference_answer_snapshot` 必须在每条 EvaluationResponse 中保存，使后续查看不依赖可变关联。

## 默认公平配置

除非用户在创建 Run 时显式覆盖，v1 使用：

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| `temperature` | `0` | 尽量减少采样随机性 |
| `top_p` | `1` | 不额外截断概率质量 |
| `max_tokens` | `256` | 对 Demo 客观短答案足够，实际值写入快照 |
| `seed` | `42` | 按请求发送；平台记录请求值，但无法证明 Provider 实际应用 |
| `concurrency` | `1` | 默认串行，避免速率和调度差异 |
| 最大尝试次数 | `3` | 首次请求加最多 2 次重试 |
| 指数退避 | `0.25s, 0.5s` | base 为 0.25 秒、cap 为 2 秒；当前 2 次重试不会到达 cap |

Model 可为 `temperature`、`top_p`、`max_tokens`、`seed` 保存经过同等范围校验的默认值；这属于操作者配置的显式预设。Run 请求中出现的字段优先，省略字段才回退到 Model 默认，Model 也未配置时使用上表协议默认。最终有效值写入 `generation` 快照；直接比较仍要求这些值完全相同。

重试仅适用于 429、部分 5xx、连接中断、连接超时和读取超时等暂时性错误。认证失败、无效模型名、参数错误等明确 4xx 不重试。`latency_ms` 以第一次尝试前到最终成功或失败后的墙钟时间计算，包含重试和退避；因此比较延迟时必须使用相同重试配置。

即使 temperature 为 0 且 seed 相同，上游实现、模型权重或基础设施仍可能变化。LLMBenchLab 记录“请求了什么”，不能证明供应商实际应用 seed，也不能保证逐 token 确定性。

## Prompt 构造

manifest 的 `prompt_template` 包含 `system` 和 `user`。`user` 必须含 `{prompt}`，可含 `{choices}`：

1. 对所有题型，把 `{prompt}` 替换为题目原文。
2. 对 multiple choice，把 choices 按键的字典序渲染为每行 `A. 文本` 的形式，并替换 `{choices}`。
3. 非 multiple choice 使用 `{choices}` 时替换为空字符串；建议模板不在这些题型中引用它。
4. 不对 Benchmark 内容执行模板代码、表达式或任意文件引用。
5. 最终 system prompt、模板原文和生成参数一并快照。

若后续支持复杂模板引擎，必须使用受限沙箱并升级协议版本。

## 逐题执行

每道计划题恰好产生一条最终 EvaluationResponse。过程如下：

1. 使用 Run 快照构造消息和 generation config。
2. 调用选定 ModelAdapter；临时错误按快照策略有限重试。
3. 成功时保存未经答案规范化的 `raw_response`、input/output Token 和总延迟；Adapter 结果还携带 request ID/raw usage 供调用期诊断，但 Phase 1 的 Response Schema 不持久化这两个扩展字段。
4. 根据 manifest 的题型映射选择版本化 Evaluator。
5. 保存 `parsed_answer`、标准答案快照、0/1 分、Evaluator 名称和解析元数据。
6. 失败时保存稳定的 `error_type` 和经脱敏、截断的 `error_message`；继续处理下一题。
7. 无论成功或失败，提交本题 Response 后才增加 `completed_questions`。

不得因为解析器能够猜到“可能答案”而掩盖歧义。空回答、请求失败、解析失败和 Evaluator 内部错误均计 0 分。

## Evaluator 规则

### Exact Match v1

默认规范化顺序：

1. 把 CRLF 和 CR 统一为 LF。
2. 去除首尾空白。
3. 当 `normalize_whitespace=true`（默认）时，把连续 Unicode 空白折叠为一个 ASCII 空格。
4. 当 `case_sensitive=false`（默认）时使用 Unicode `casefold` 比较；为 true 时保留大小写。

规范化后的字符串必须完全相等才得 1 分。不做编辑距离、同义词、包含关系或语义匹配。

### Multiple Choice v1

允许选择键为大写英文字母。解析优先级：

1. 明确的最终答案表达，如 `最终答案：A`、`答案是 A`、`The answer is A`、`选择 A`；
2. 整个去空白回答为 `A`、`A.` 或 `(A)`；
3. 仅当其他文本不存在歧义时，接受位于独立答案行的单一选项键。

解析器不扫描普通单词中的字母，也不把推理段落里随机出现的 A/B/C/D 当作答案。如果同一优先级出现不同候选，或最终答案表达互相冲突，返回 `ambiguous_choice`，得 0 分。解析出的键还必须存在于该题 `choices` 中。

### Numeric v1

支持普通整数、小数、科学计数法、单层 `\boxed{...}` 和明确的最终答案表达，例如 `最终答案是 42`。解析器只能接受一个唯一的有限数值，禁止 `eval`，拒绝 NaN、Infinity、表达式和单位换算猜测。

设模型值为 \(x\)，参考值为 \(r\)，绝对容差为 \(a\)，相对容差为 \(p\)。满足下式时得 1 分：

\[
|x-r| \leq \max(a, p\times |r|)
\]

`absolute_tolerance` 和 `relative_tolerance` 均为非负有限数，默认值为 0。相对容差以参考值绝对值为基准；当参考值为 0 时相对项为 0，需依赖绝对容差。数值解析与比较不能把 NaN 的特殊比较行为当作正确。

## 指标定义

令：

- \(N\)：Run 创建时快照的计划题数 `total_questions`；
- \(g_i\)：第 i 题成功获得非空模型响应且没有最终请求错误时为 1，否则为 0；
- \(a_i\)：第 i 题被唯一解析且 Evaluator 完成客观评分时为 1，否则为 0；
- \(s_i\)：第 i 题正确时为 1，其余情况为 0；未处理、请求错误、空回答、解析失败均为 0。

### 严格总分

\[
score = 100 \times \frac{\sum_{i=1}^{N}s_i}{N}
\]

这是排行榜默认指标，范围为 0 到 100。错误题始终留在分母中，避免模型通过大量请求失败获得虚高成绩。`correct_questions = Σs_i`。

### 完成率

\[
completion\_rate = 100 \times \frac{\sum_{i=1}^{N}g_i}{N}
\]

存储与 API 值范围为 0 到 100，UI 直接按百分比展示。响应成功但答案无法解析时计入完成率，因为模型调用已经完成；请求失败和空回答不计入。完成率不代表正确率。

### 已回答准确率

\[
answered\_accuracy = 100 \times \frac{\sum_{i=1}^{N}s_i}{\sum_{i=1}^{N}a_i}
\]

存储与 API 值范围为 0 到 100，UI 直接按百分比展示。只有可唯一解析且已完成评分的题进入分母；当 `Σa_i = 0` 时值为 `null`，界面显示“不适用”，不得伪装成 0% 或 100%。

### 错误数与处理进度

- `completed_questions`：已经持久化最终 EvaluationResponse 的题数，包括错误题。
- `error_questions`：请求失败、空回答、解析失败或 Evaluator 异常的题数；可解析但答错不属于系统错误。
- `total_questions`：创建 Run 时冻结，后续导入或编辑不会改变。

`pending`/`running` 状态可展示临时指标，但必须标注“运行中”。`failed` 和 `cancelled` 可展示诊断性部分结果，其未处理题按严格口径为 0；这些 Run 不进入正式排行榜。只有 `completed` Run 参与排行榜。

### 延迟、Token 与成本

- `average_latency_ms` 是具有延迟记录的题目从首次尝试到最终结果的算术平均数，包含重试退避；样本口径需在 API 中保持一致。
- Run 的 input/output tokens 是各题上游已报告 usage 的和，不得自行猜测缺失 usage。只有所有已持久化 Response 都报告对应 Token 时才聚合；任一缺失则 Run 字段为 `null`，逐题已知值仍保留。
- 单题估算成本为 `input_tokens × input_price_per_million / 1,000,000 + output_tokens × output_price_per_million / 1,000,000`。
- 任一必要 usage 或价格缺失时，该题成本为 `null`；Run 成本只有在所有已完成模型调用均可计算时才给出总值。Mock 的明确零单价可计算为 0。
- 价格必须在 Run 创建时快照。估算成本不等同供应商账单，不包含缓存、阶梯价、税费或重试计费差异。

## 错误题处理矩阵

| 情况 | Response | `g_i` | `a_i` | `s_i` | 是否 `error_questions` |
| --- | --- | ---: | ---: | ---: | --- |
| 正确且可解析 | 保存完整结果 | 1 | 1 | 1 | 否 |
| 错误但可解析 | 保存完整结果 | 1 | 1 | 0 | 否 |
| 响应非空但解析失败 | 保存 raw 与 parse_error | 1 | 0 | 0 | 是 |
| 空回答 | 保存 `empty_response` | 0 | 0 | 0 | 是 |
| 超时/网络/最终 429 | 保存脱敏后的最终错误类型与信息 | 0 | 0 | 0 | 是 |
| Evaluator 异常 | 保存脱敏错误 | 1 | 0 | 0 | 是 |
| 取消前未处理 | 无伪造 Response | 0 | 0 | 0 | 不计已处理错误；Run 为 cancelled |

单题异常不得使 Run 直接失败；数据库不可用、Runner 不变量破坏等 Run 级异常除外。

## 数据集版本规则

- `schema_version` 描述文件格式，v1 为 `llmbenchlab-dataset-v1`。
- Benchmark `id` 是跨版本稳定标识，`version` 标识内容发布版本。
- 题目、顺序、答案、Prompt、Evaluator 配置、元数据或 manifest 中任何参与 Hash 的内容改变，都必须发布新的 Benchmark version。
- 相同 `id + version + hash` 是同一不可变数据集，可幂等导入。
- 相同 `id + version` 但 Hash 不同是版本冲突，必须拒绝；不得静默替换。
- 数据库中已被 Run 引用的 Benchmark/Question 不做原位修改。

详细 Schema 见 [`DATASET_FORMAT.md`](./DATASET_FORMAT.md)。

## Dataset SHA-256 规则

v1 Hash 输入由经过 Schema 校验的 JSON 值生成，采用项目定义的 **JCS 风格**规范化；它不是对 RFC 8785 的完整实现：

1. manifest 只保留源格式允许的字段，移除 `dataset_hash`、导入时间、数据库 ID、绝对路径等导入派生字段。
2. manifest 与每道题分别使用等价于 Python `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` 的方式递归排序对象键并编码为紧凑 JSON。
3. 数组顺序不变；questions 按 JSONL 原始行顺序规范化，题目顺序是数据集身份的一部分。
4. 载荷为 `canonical_manifest + "\n" + canonical_question_1 + "\n" + ... + canonical_question_n + "\n"`。
5. 对载荷的 UTF-8 字节计算 SHA-256，输出 64 位小写十六进制字符串。

因此源文件的缩进、对象键顺序、CRLF/LF 和 JSON 中非 ASCII 字符的转义写法不影响 Hash；题目行顺序、数组顺序、数值的解析后表示和任何语义字段会影响 Hash。Hash 用于身份和漂移检测，不是发布者真实性签名。

## 结果可比性

两个 Run 只有在以下条件同时满足时，才可标注为“直接可比”：

- 都是 `completed`；
- `protocol_version` 完全相同；
- Benchmark ID、version 和 Dataset SHA-256 完全相同；
- Prompt template、system prompt、Evaluator 名称/版本/配置完全相同；
- temperature、top_p、max_tokens、seed、并发、超时与重试策略相同；
- 题目全集相同，没有抽样或过滤差异。

模型名称和 Adapter 是被比较的变量，应显示而不是要求相同。若供应商不支持 seed、未返回 usage，或远端模型可能在同名下滚动更新，结果仍可展示，但必须显示相应限制。Git SHA 不同不自动判定不可比；若协议或执行代码变化却未升级版本，则属于实现缺陷，应通过审计 SHA 识别。

排行榜必须按 `protocol_version + benchmark_id + benchmark_version + dataset_hash` 分区或过滤。Web 排行榜要求先选择一个具体 Benchmark 记录，并显示 version 与 Hash，再在该分区内编号；API 未传 `benchmark_id` 时返回的是跨分区结果集合，调用方不得把集合顺序解释为统一名次。默认排序使用严格总分，其后可用完成率和延迟作为展示信息，不应用 answered accuracy 掩盖失败率。

## 污染与解释风险

- 公共题目可能进入模型训练语料；高分可能反映记忆而非泛化。
- Demo Benchmark 题目简单且公开，只验证平台链路，**不代表正式模型能力**，不得与正式榜单混合。
- Prompt 或答案泄漏、重复题、翻译差异和模型滚动更新都会影响结论。
- Dataset Hash 证明输入一致，不证明题目未污染、来源合法或执行环境完全等价。
- 小样本分数具有高方差。MVP 不提供置信区间，用户不得据此宣称统计显著优势。

发布结果时应同时披露协议版本、完整数据集标识、Run 快照、时间、样本量、失败率和已知污染风险。

## Public、Private 与 Live Benchmark 规划

### Public Benchmark

Phase 3 引入有明确许可证和来源的公共数据集插件，记录原始 revision、转换器版本与派生 Hash。不得在仓库中重新分发许可证不允许的题目。

### Private Benchmark

Phase 5 支持仅用户可见的数据集、访问控制、静态加密和审计。私有并不等于未污染；仍需轮换题目、最小化泄漏，并禁止把题目正文发送给非预期供应商。

### Live Benchmark

Phase 5 支持定期生成或轮换题目、时间窗口和冻结快照。每次 Run 仍必须引用一个不可变 revision/Hash；“Live” 不能成为运行中改变题目的理由。跨窗口成绩默认不可直接比较。

在这些能力完成前，MVP 只提供本地静态 Benchmark 和 Demo 标识。
