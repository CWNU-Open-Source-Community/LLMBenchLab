# 2026-08-30：加载已下载的标准评测集

## 元数据

- 日期：2026-08-30（Asia/Shanghai）
- 分支：`codex/complete-evaluation-workflow`
- 当前阶段：Phase 3 标准 Benchmark 的既有可信本地切片
- 关联计划：不适用；这是一次单一、可回滚的本地数据导入，不改变架构、协议、Schema 或公共 API
- 初始工作区：与 `origin/codex/complete-evaluation-workflow` 同步，开始时无未提交改动

## 目标与背景

用户已经在仓库的 Git 忽略目录中下载并转换了标准评测集，希望把这些现有评测集加载到默认本地数据库，而不是重新下载。当前默认 SQLite 仅包含恢复后的 Demo Benchmark。

## 范围

- 只读盘点并校验 `artifacts/benchmarks/` 中现有的 dataset-v1 ZIP。
- 通过项目已有的 Benchmark 导入入口，将全部唯一且有效的 ZIP 导入默认本地 SQLite。
- 导入前创建一致性备份；导入后验证数据库、API 与逐 Benchmark 题数。
- 记录本地数据变更、验证证据和回滚位置。

## 非目标

- 不重新下载或转换第三方数据。
- 不运行任何评测，不调用真实 Provider，不产生模型费用。
- 不修改数据格式、协议、数据库 Schema、API 或产品代码。
- 不实现 IFEval、插件 SDK、代码沙箱或 Phase 3 其余 Roadmap 内容。

## 验收标准

- 三个现有 ZIP 都通过当前 Loader 校验，且不输出题目正文。
- `gpqa-diamond`、`mmlu-pro-direct`、`mmlu-pro-official-cot` 均可由默认 API 查询。
- 导入后共 4 个 Benchmark、24,277 道题；原有 1 个 Model、1 个 completed Run 和 15 条 Response 不变。
- SQLite `quick_check` 为 `ok`、外键错误为 0、Alembic revision 为当前 head。
- 导入前备份保留，未触发 Provider 请求，未留下新增活动 Run。

## 假设

- 用户所说“目录里下载的评测集”指 `artifacts/benchmarks/` 中三个已生成的 dataset-v1 ZIP；`artifacts/dataset-cache/` 是转换器源缓存，不是可直接导入包。
- 两份 MMLU-Pro ZIP 虽来自相同题目源，但 prompt/profile 不同，应作为两个独立 Benchmark 保留。

## 风险与控制

- 大批量写入中断：导入前使用 SQLite 在线 backup API 创建唯一备份；每个 ZIP 由 API 单独事务提交。
- 重复或冲突版本：导入前比较 slug/version/hash；现有持久化语义对同 hash 幂等、异 hash 冲突拒绝。
- 第三方题目敏感性：只记录文件名、大小、Hash 和计数，不打印或提交题目正文；数据与备份继续留在 Git 忽略目录。
- 运行中服务并发：只使用公开导入 API，不直接改写被服务占用的数据库；导入后重新核对原有 Run/Response 计数。

## 实施步骤

1. 完成仓库、阶段、格式/API/安全/测试文档和工作区只读检查。
2. 校验三个 ZIP 的 archive SHA-256、dataset hash、slug/version 和题数，并核对数据库不存在同版本。
3. 创建并验证默认 SQLite 的导入前备份。
4. 通过现有 `/api/v1/benchmarks/import` 顺序导入三个 ZIP。
5. 验证 API 列表、逐集题数、数据库完整性、迁移 head 及原有业务计数。
6. 运行相关离线测试，更新状态文档，复核 diff 和秘密边界。
7. 独立提交并普通 push，等待精确 SHA 的必需 GitHub Actions 全绿。

## 实际结果

### 盘点与导入

| ZIP | archive SHA-256 | dataset hash | 题数 | API |
| --- | --- | --- | ---: | --- |
| `gpqa-diamond-1.0.0-dc4dc6fd5089.zip` | `68623d1840f14ad6c18163c5e87e47e78943961c5f8bf5d70fb9258ba074b62c` | `38e196d2993f7d1f59ad077c93ac3c128ad8765264c897bae9a3b324f6caae53` | 198 | `201` |
| `mmlu-pro-direct-1.0.0-44a485f6edcb.zip` | `e62a1f45cba71d18f4096256b08bbd4b182d1dd9e1a4d81e6de007db715c27a6` | `cf7872819f2be58373e8181706aa366b23008ac664f3fc551aa82145a758500b` | 12,032 | `201` |
| `mmlu-pro-official-cot-1.0.0-8b7fc4ca4a06.zip` | `f7bc8773a3fa4937cd482cc0bf65a42ec818b3d386320cb23299114acde329bf` | `81e348f25f217baab178df9217909438da1a00707473f683cac087152911e33c` | 12,032 | `201` |

- 三个 ZIP 都是 `0600` 普通文件，并由当前 `load_dataset_zip_bytes` 完整校验；没有输出题目正文。
- 导入时现有 `make dev` 正常运行，Worker 扫描但无 `pending/running` Run。按正式入口顺序执行三个 multipart 请求，没有直接写被服务占用的 SQLite。
- 导入前备份：`backend/data/llmbenchlab.db.pre-benchmark-import-20260829T173032Z.bak`，SHA-256 `d8c71c44b0ee364030d0053788f34bd985a443ba30a9cd17f1069a9737d10206`，权限 `0600`；备份自身 `quick_check=ok`、外键错误 `0`、head=`20260829_0006`，业务计数为 `1/1/15/1/15`（Model/Benchmark/Question/Run/Response）。

### 导入后验证

- 默认数据库：`quick_check=ok`、外键错误 `0`、Alembic `20260829_0006`。
- 总计：`1` Model、`4` Benchmarks、`24,277` Questions、`1` completed Run、`15` Responses、`0` active Runs。
- DB 逐集 `COUNT(questions)` 与 manifest/API 均一致：Demo `15`、GPQA `198`、MMLU-Pro Direct `12,032`、MMLU-Pro Official-CoT `12,032`。
- API `/benchmarks` 返回 total `4` 且三个 slug/version/dataset hash 精确匹配；三个 `/questions?limit=1` 响应 total 分别为 `198/12032/12032`，只投影计数用于记录。
- API、Worker 与 Vite 保持用户启动时的运行状态；没有创建 Run、访问 Provider 或产生费用。

### 实际命令与结果

| 命令/检查 | 结果 |
| --- | --- |
| Loader 盘点三个 `artifacts/benchmarks/*.zip` | 3/3 合法；原始 cache 未作为导入包 |
| `sqlite3 ... .backup ...pre-benchmark-import...bak` | 成功；备份校验与业务计数通过 |
| 三次 `curl -F archive=@... /api/v1/benchmarks/import` | `201/201/201` |
| SQLite quick/FK/head/count/逐集对账 | 通过；`4` Benchmarks / `24,277` Questions |
| API Benchmark 列表与逐集 total 对账 | 通过 |
| `uv run pytest -q tests/test_dataset_loader.py tests/test_standard_datasets.py` | `40 passed`；只有既有上游弃用 warning |

### 已知边界与未运行项

- 本次没有产品代码、Schema、API、协议或前端行为变更，因此本地未重复全量 `make test`、lint、frontend build、Smoke 或 Compose；目标 Loader/转换器测试和真实本地导入/数据库/API 对账是本次最小充分验证，远程 CI 将执行仓库常规门禁。
- 两种 MMLU-Pro profile 共享源题但 prompt 配置不同，结果不得互相或与其他 profile/hash 无提示混合比较。
- 本地 SQLite 与第三方题目/备份不进入 Git；备份 hash 是完整性标识，不是签名、加密或访问控制。
- 当前状态：本地数据加载完成；文档提交、普通 push 与精确 SHA GitHub Actions 待执行，所以任务暂保持 `in_progress`。
