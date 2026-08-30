# 2026-08-30：准备小型模型评测数据集

## 元数据

- 日期：2026-08-30（Asia/Shanghai）
- 分支：`codex/complete-evaluation-workflow`
- 当前阶段：Phase 3 标准 Benchmark 的个人本地小样本补充
- 关联计划：不适用；本次只生成并导入本地第三方数据 ZIP，不改变架构、协议、Schema 或公共 API
- 初始工作区：与 `origin/codex/complete-evaluation-workflow` 同步，开始时无未提交改动

## 目标与背景

当前本地正式 Benchmark 为 GPQA-Diamond 198 题及两份 MMLU-Pro 12,032 题版本，运行成本和等待时间不适合频繁横向比较模型。用户希望补充若干每套不超过 100 题的小型数据集，用当前内置客观评分器快速测试不同模型。

## 范围

- 从官方或维护者发布位置固定来源 revision，选择可转换为 multiple-choice 或 numeric 的公开评测集。
- 对超过 100 题的来源使用固定 seed 和稳定散列排序，从有公开标签的评测 split/文件确定性抽取 100 题；恰好 100 题的 XCOPA 中文 validation 使用全量并只平衡选项呈现位置。
- 生成并完整校验 `llmbenchlab-dataset-v1` ZIP，保存在 Git 忽略的 `artifacts/benchmarks/`。
- 通过现有 Benchmark 导入 API 加载到当前默认本地数据库，并核对 API/数据库题数与 Hash。
- 记录来源、许可、抽样、Hash、导入和验证证据，不记录题目正文。

## 非目标

- 本任务不新建模型评测、不主动调用 Provider 或产生额外模型费用；用户既有活动 Run 继续执行，不在本任务范围内停止。
- 不修改 Dataset Loader、Evaluator、Benchmark 协议、数据库 Schema、API、前端或生产依赖。
- 不加入需要 LLM Judge、代码执行沙箱或 IFEval 专用验证器的题目。
- 不把第三方原始数据、转换后的题目或数据库备份提交到 Git。
- 小样本结果不冒充原 Benchmark 全量榜单结果，也不宣称模型差异具有统计显著性。

## 验收标准

- 至少 5 个能力维度，包含英文和中文；每个新 Benchmark 恰好 100 题且可由当前内置评分器自动判分。
- 每个来源 revision、源文件 SHA-256、split、抽样 seed/算法、prompt profile 和许可均可追溯。
- 每个 ZIP 由当前 Loader 完整校验，题目 ID 唯一、答案合法，且不输出题目正文。
- 新 Benchmark 经正式 API 导入后可查询，API/manifest/数据库逐集题数和 Dataset Hash 一致。
- 导入前保留一致性数据库备份；导入后 SQLite `quick_check=ok`、外键错误为 0；本任务不创建、停止、重置或修改任何 Run。导入完成检查点的 Model/Run 数量应相对备份保持，Response 只允许由既有活动 Run 增长；更晚的并发客户端变更必须由独立 Run/audit 时间线解释。

## 假设

- “一种数据集不超过 100 道题”解释为每个可独立选择的 Benchmark 版本最多 100 道计分题；本次统一为 100 题，便于分数直接理解为答对题数。
- 用户希望数据准备后可直接在当前 Web/API 中选择，而不只是获得候选链接。
- 选择有公开标签的 test/validation/dev split 或官方 benchmark 文件；不使用隐藏标签测试集，不从训练 split 抽题。TruthfulQA 固定 CSV 没有官方 split。
- 相同 100 题、相同 prompt 和 evaluator 用于所有模型；生成参数仍需由用户在 Run 时保持一致。

## 风险与控制

- 许可证或来源边界：只选有明确许可的官方/维护者来源，并在 manifest 与记录中披露；受非商业限制的数据默认不选入。
- 公共 Benchmark 污染：明确披露高分可能受训练污染影响，小样本只用于快速筛查。
- 抽样偏差和高方差：固定 seed 42、源记录标识，以及 `sha256-sort-v1`、`sha256-stratified-v1` 或 `full-split+sha256-balanced-position-v1` 的逐集算法，保证复现；不与全量结果混排。
- 答案格式不兼容：只使用 multiple-choice/numeric，prompt 明确要求最终字母或数字，导入前逐题做结构与参考答案检查。
- 本地数据库写入中断或与既有 Run 并发：导入前使用 SQLite online backup，逐 ZIP 单事务导入，导入后核对完整性、Benchmark/Question 增量和原有业务计数；不把既有 Run 正常新增的 Response 误归因于导入。
- 第三方题目泄漏：源缓存、ZIP、备份和转换清单留在 Git 忽略目录并使用 `0600` 权限；日志只记录元数据与 Hash。

## 实施步骤

1. 核对仓库状态、当前数据、格式/协议限制和候选数据的官方来源与许可。
2. 固定来源 revision，下载并校验原始文件；保存只含元数据的来源与转换清单。
3. 以 seed 42 对超过 100 题的公开标签评测 split/文件做稳定散列抽样，对 XCOPA 中文 validation 使用完整 100 题，生成 dataset-v1 ZIP。
4. 用当前 Loader 校验全部 ZIP，并检查题型、ID、答案、题数、语言和元数据分布。
5. 创建并校验导入前数据库备份，通过正式 API 逐个导入。
6. 对账 API、数据库、manifest、Hash、完整性和原有 Run/Response，确认本任务未新建 Run 或触发 Provider；单独记录既有活动 Run 的并发推进。
7. 更新本日志与状态文档，复核 tracked diff、秘密边界和 Git 忽略范围。

## 实际结果

### 数据集与来源

共准备并导入 6 套、600 道计分题，覆盖英文/中文、数学推理、常识续写、代词消歧、真实性常识与因果推理。所有来源均固定到不可变 revision 或官方发布归档；下表 Hash 为下载到本地的原始源文件 SHA-256。

| Benchmark slug | 能力 / 语言 / 题型 | 来源文件 / split（原规模） | 固定 revision / 发布物 | 源 SHA-256 | 许可 |
| --- | --- | --- | --- | --- | --- |
| `gsm8k-mini-100` | 数学推理 / 英文 / numeric | GSM8K `test`（1,319） | `openai/grade-school-math@3101c7d5072418e28b9008a6636bde82a006892c` | `3730d312f6e3440559ace48831e51066acaca737f6eabec99bccb9e4b3c39d14` | MIT |
| `mgsm-zh-mini-100` | 数学推理 / 中文 / numeric | `mgsm/mgsm_zh.tsv`（benchmark/test set，250） | `google-research/url-nlp@452a21ad3dae5668c06ceeac21ff073e1e40f9be` | `b2fa63151022370a0de1f4211c8c284eae74b0f5a3b003b1d5982c0d4a73f661` | CC BY 4.0（数据子目录） |
| `hellaswag-mini-100` | 常识续写 / 英文 / 四选一 | HellaSwag `validation`（10,042） | `rowanz/hellaswag@a29ff8e9a04bba4bd6588223785ce105328adc57` | `0aa3b88843990f3f10a97b9575c94d7b71fb2205240ba04ae4884d9e9c992588` | MIT |
| `winogrande-mini-100` | 代词消歧 / 英文 / 二选一 | WinoGrande v1.1 `dev`（1,267） | 官方 `winogrande_1.1.zip`；仓库指针 `727e837f77521ef38bcc56df3b275c8da43f45af`，归档字节由右侧 SHA 固定 | `3619ab104d8be2977b25c90ff420cb42d491707dcc75362a1e5d22bc082b7318` | CC BY 2.0（数据） |
| `truthfulqa-binary-mini-100` | 真实性常识 / 英文 / 二选一 | `TruthfulQA.csv`（无官方 split，790） | `sylinrl/TruthfulQA@d71c110897f5d31c5d7f309e7bc316c152f6f031` | `b8d8ef1e12f98b4f2a9f47abc9765da0640b182b6c5d9b92f0c1a1f2f1e02e5c` | Apache-2.0（仓库） |
| `xcopa-zh-validation-100` | 因果常识 / 中文 / 二选一 | `data/zh/val.zh.jsonl`（100，全量） | `cambridgeltl/xcopa@e2e9d7f105a758ee869cd13e0fe251ef93a29840` | `8f638466c196342104bdbe9276e7d91fe0abcc044e9a9ad715f30c8ad618bcdd` | CC BY 4.0（仓库） |

### 可复现转换与产物

- 本地转换器为 Git 忽略的 `artifacts/tools/prepare_small_benchmarks.py`；统一 seed 为 `42`，使用带数据集命名空间的确定性 SHA-256 排序。GSM8K test 与 MGSM benchmark/test 文件各取 100 题；TruthfulQA 从固定、无官方 split 的 CSV 取 100 题；HellaSwag 按正确选项分层为 `25/25/25/25`；WinoGrande 为 `50/50`；XCOPA 使用恰好 100 题的完整 `data/zh/val.zh.jsonl`，并确定性平衡呈现后的正确选项为 `50/50`。
- TruthfulQA 转换成当前 Evaluator 可评分的 binary MC：每题使用 `Best Answer` 与 `Best Incorrect Answer`，并确定性平衡正确答案位置。它不是官方 MC1/MC2 全选项成绩，名称显式保留 `binary-mini`，不得与官方全量分数混用。
- numeric prompt 要求最后给出不带千位分隔符的数字；multiple-choice prompt 要求最终选项字母。没有加入答案解析或链式思维参考正文。
- 元数据清单为 Git 忽略的 `artifacts/benchmarks/small-benchmarks-provenance-v1.json`，记录源 URL/revision/Hash、所选源行、许可、题型/答案分布、archive Hash 与 Dataset Hash；题目正文和答案均未写入 tracked 文档。
- 转换器重复运行后六个 ZIP 与 Dataset Hash 均未变化。产物、源缓存、转换器和清单权限均为 `0600`，且由 Git ignore 覆盖。

| Benchmark slug | 版本 | Dataset Hash | ZIP SHA-256 |
| --- | --- | --- | --- |
| `gsm8k-mini-100` | `1.0.0-b4d4154c57b3` | `16c0cd13616024a3a4f3f4f4533552ce6d1552c7569e9e721c8e255932115a95` | `93b7221ca25cc37044c697cf98424a3847eae0981be6e395edd25a2e2187f8d8` |
| `mgsm-zh-mini-100` | `1.0.0-4bd34714ce14` | `a79b41e07a4e15f454d13eceacdca76780f8fdc6fdc679374184be25207bcafc` | `272a1ce194a39959112d1429f3b51fc20a3f70ef4680bf9939ba53c650b63a87` |
| `hellaswag-mini-100` | `1.0.0-89e902992f2d` | `37311f31662e3f9f11882cc7ec5f3bd9ee34647b241ab37b991ac76c150ff0c8` | `51c77744d33fb8550de8f40836bb5f9c6210f3ad23ff91be1c3f6da9bf99f642` |
| `winogrande-mini-100` | `1.0.0-cff50d08ab54` | `2e0b597263b16d39d6e4bd4d5bc9f8eb39632e6e29123e57e3a1aefbde4fd77d` | `440e2fc2fe5ff28842927b4078aa15022535576113b9f7864df4ddd3f9593a3c` |
| `truthfulqa-binary-mini-100` | `1.0.0-ead5ea285da8` | `909b44c76c6b0bbe39baa1a82aabb0a14a967866d6b4bce6f364b2d86c4fdef6` | `b1933adcf5c74907e5751c6bee424474ec5dc7f7e89250d4d2340b5ad125cf31` |
| `xcopa-zh-validation-100` | `1.0.0-34d05db11a88` | `ffacf866600eacf9023e944db31f2cc17412927c3ffb9f4e32aac8eae2a6da9d` | `624dd7174d1eaddf0f5129ac093b387ebc2ae71b39bccd7452a30781b318b4d6` |

### 数据库备份与正式导入

- 导入前通过 SQLite online backup 创建 `backend/data/llmbenchlab.db.pre-small-benchmark-import-20260830T054837Z.bak`，权限 `0600`、大小 `109,219,840` bytes、SHA-256 `5456688d871c3040c6d092df744399a315d48feddf37db7062fc37844cf04d2c`。冻结快照为 Models/Benchmarks/Questions/Runs/Responses=`2/4/24,277/5/1,000`，唯一活动 Run `ab72c8ea-1d64-42d9-9946-e9cb4f1f23cb` 为 `765/12,032`、`error_questions=0`；`quick_check=ok`、外键错误 0、head=`20260830_0007`。
- 六个 ZIP 均通过正式 `POST /api/v1/benchmarks/import` 导入，HTTP 结果为 `201` × 6。API、manifest 与数据库逐集均为 100 题且 Dataset Hash 一致；默认库因此从 4 增至 10 个 Benchmarks、从 24,277 增至 24,877 道 Questions，Models 与 Runs 数量保持 `2/5`。
- 既有 12,032 题 Run 在准备和导入期间继续执行；本任务没有调用 Run 创建/取消/重置接口。取消请求之前的只读复核返回 `running`、`789/12,032`、`error_questions=0`，全库 Runs/Responses=`5/1,024`；相对备份新增的 24 条 Response 正好来自该活动 Run，不能归因于 Benchmark 导入。该检查与 Writer 并发，因此不把客户端命令开始时间冒充数据库行的精确 `created_at` 截止点。
- 导入完成后的另一个并发客户端时间线在 `2026-08-30T05:53:55Z` 创建了一条 MGSM mini Run；数据库 audit 又在 `05:54:49Z` 记录原大 Run 的 `run_cancel_requested`，并于 `05:54:52Z` 记录 terminal `cancelled`，最终 `798/12,032`、正确 `723`、`error_questions=1`，reconcile payload 为 `released_reservations=0`、`conservative_settlements=1`。随后还创建了另一条 MGSM mini Run。`05:55:48Z` 的动态库快照为 Runs/Responses=`7/1,041`。这些后续状态变化没有由本任务的工具调用发起，也不能归因于 Benchmark 导入；它们反而说明新 Benchmark 已可被其他客户端直接选择。本任务没有直接发起 Provider 请求，但不能把整台服务描述为“期间无 Provider 流量”。
- 导入后 `quick_check=ok`、外键错误 0、Alembic head=`20260830_0007`。没有修改 Schema、API、协议、Evaluator 或产品代码。

### 验证结果

- 六个 ZIP 均由当前 Dataset Loader 完整加载；600 个题目 ID 在各自数据集内唯一，题型/答案合法，manifest、archive 与持久化 Hash 对账一致。
- provenance 的六个 `source.rows` 已完整记录为 `1,319/250/10,042/1,267/790/100`；首次独立复核发现三个非 JSONL 源为 `null` 后，已修正本地转换器并重复生成 metadata-only 清单，六个 ZIP/archive/Dataset Hash 均保持不变。
- 以每题金标 reference answer 走当前内置 Evaluator 做格式自检，`600/600` 均获接受；这不是任何模型答对 600/600 的评测成绩。HellaSwag 正确选项为 `A/B/C/D=25/25/25/25`，WinoGrande、TruthfulQA binary 与 XCOPA 均为 `A/B=50/50`。
- `uv run pytest -q tests/test_dataset_loader.py tests/test_standard_datasets.py`：`40 passed`，只有既有上游弃用 warning。
- 转换脚本通过 Python 编译检查；重复生成 Hash 不变。首次从 `backend/` 工作目录误用 `backend/.venv/bin/python`，因相对路径重复而失败；随后改用 `.venv/bin/python` 重跑同一 reference-answer 检查并得到 `600/600`。该纠正过程如实保留，不把首次失败记成产品回归。
- 最终 archive Hash shell 复核的首次循环把 `path` 用作变量名；zsh 会把它与 `PATH` 绑定，导致循环内 `shasum`/`awk` 无法查找并报出伪 mismatch。改名为 `archive_file` 并使用绝对工具路径后，六个 archive Hash 全部通过；同时把转换器权限从默认创建的 `0644` 收紧为 `0600`。这是本地校验命令/权限修正，不是数据内容变化。
- staged 文档措辞检索首次把含 Markdown 反引号的模式放入双引号，zsh 因而尝试执行无害的 `zh` 并报告 command not found；改为单引号后同一只读检索正常完成，未产生文件或外部状态变化。
- 本次不改实现代码、Schema、公共合同或依赖，因此没有重复运行不相关的完整 Compose/capacity 门禁；目标 Loader/标准数据集测试和真实本地数据库/API 对账与风险相称。

### 结论与边界

验收标准全部满足。六套数据可直接在当前 Benchmarks/New Run 页面选择，且每套恰好 100 题。它们适合低成本、同配置的模型初筛和逐题配对比较；公开 Benchmark 可能被训练数据污染，100 题准确率的标准误差在最不利的 50% 正确率附近约为 5 个百分点，因此不应把小分差解释为稳定排名，也不应将 mini/binary 子集成绩冒充官方全量成绩。
