# Phase 1：MVP 垂直链路

- 状态：`completed`
- 完成日期：2026-08-25
- 协议基线：`llmbenchlab-protocol-v1`
- 前置阶段：[Phase 0 — Governance](PHASE-0-GOVERNANCE.md)（`completed`）
- 后续阶段：[Phase 2 — Reliability](PHASE-2-RELIABILITY.md)

## 阶段目标

交付默认完全离线可验收的个人 LLM Benchmark MVP：注册 Mock 模型、载入 Demo Benchmark、创建后台 Run、逐题生成和客观评分、持久化结果，并从前端查看进度、详情与排行榜。

## 功能范围

- FastAPI、SQLAlchemy 2.x、Alembic、SQLite 和 `/api/v1`。
- Model、Benchmark、Question、EvaluationRun、EvaluationResponse 实体及 UTC 时间。
- Mock Adapter 与 OpenAI-compatible Adapter；密钥只保存环境变量名。
- exact match、multiple choice、numeric Evaluator 及解析错误记录。
- 版本化 manifest/JSONL 校验、稳定 SHA-256 和原创 `demo-general`。
- 受控进程内 Runner：低并发、单题错误隔离、取消标志、进度与汇总。
- Dashboard、Models、Benchmarks、New Run、Run Detail、Leaderboard。
- 后端/前端测试、离线 Smoke Test、CI、Compose、Makefile 与开源文档。

## 非目标

- 不使用 PostgreSQL、Redis、独立 Worker、Kubernetes 或微服务。
- 不下载完整 MMLU-Pro、GPQA、HumanEval 等大型数据集。
- 不执行不可信代码，不实现 Judge、Arena、Agent、长上下文或多用户。
- 不将本 MVP 直接用于公网生产，不进行真实付费 API 自动测试。

## 依赖

- Phase 0 完成且文档/ADR 可作为实现基线。
- 本地 Python 与 Node.js 工具链；Docker 为可选验证环境。
- OpenAI-compatible 联机能力为可选手工路径，不阻塞离线验收。

## 任务拆分

| ID | 工作包 | 关键结果 |
| --- | --- | --- |
| P1-01 | 后端与数据库骨架 | 配置、日志脱敏、模型、Schema、迁移、健康检查 |
| P1-02 | 数据集能力 | Loader、Validator、错误定位、稳定 Hash、Demo 数据 |
| P1-03 | 评测核心 | Adapter、三类 Evaluator、Runner、快照和汇总指标 |
| P1-04 | REST API | 模型、Benchmark、Run、Responses、Leaderboard、Metrics |
| P1-05 | 前端垂直链路 | 六个页面、轮询、筛选、响应式与错误/空状态 |
| P1-06 | 自动化验证 | 后端单元/集成、前端测试、纯离线 Smoke Test |
| P1-07 | 开发与发布配置 | Makefile、脚本、Compose、CI、环境变量示例 |
| P1-08 | 文档与收尾 | README/API/Testing/Deployment/Security/Status/Worklog |

## 验收标准

以下 20 项已经由 2026-08-24 至 2026-08-25 的最终验证确认：

- [x] 后端可以启动。
- [x] 前端可以 production build。
- [x] Mock 模型可以注册。
- [x] Demo Benchmark 可以载入，并显著标注“Demo 数据，不代表正式模型能力”。
- [x] 可以创建 Run，创建请求立即返回 Run ID。
- [x] Run 可以在受控后台任务中完成。
- [x] 每题均可产生或记录 EvaluationResponse，单题错误不终止 Run。
- [x] ExactMatch、MultipleChoice、Numeric Evaluator 正常工作。
- [x] 严格总分按全部计划题目计算，错误/空答/解析失败计 0。
- [x] `completion_rate` 计算正确。
- [x] `answered_accuracy` 只基于成功获得可评答案的问题计算。
- [x] 前端能显示 Run 状态、进度、指标与配置快照。
- [x] 前端能显示逐题 raw/parsed/reference/score/error。
- [x] Leaderboard 能显示结果、协议、版本、延迟、Token、成本和时间。
- [x] 完全离线 Smoke Test 通过。
- [x] 规格要求的关键单元与集成测试通过。
- [x] CI 覆盖后端 lint/test 和前端 lint/test/build，且不调用真实 API。
- [x] README 有可复现的本地、Mock、OpenAI-compatible 和 Docker 说明。
- [x] 仓库不包含真实密钥，响应与日志不泄漏 Authorization 或密钥值。
- [x] Roadmap、Project Status、Next Task、Changelog 与工作日志和实际结果一致。

## 风险

| 风险 | MVP 应对 | 后续归属 |
| --- | --- | --- |
| 进程重启丢失后台任务 | 启动时将遗留 `running` 标为失败/中断并记录原因 | Phase 2 持久队列与恢复 |
| SQLite 写并发有限 | 默认小并发、短事务、避免重复启动 | Phase 2 PostgreSQL |
| 任意 `base_url` 引发 SSRF | 文档警告、限制信任边界、日志脱敏 | Phase 6 公网防护 |
| 上游限流/网络异常 | 有限指数退避、错误分类、单题隔离 | Phase 2 Provider 限流 |
| 输出解析歧义 | 明确最终答案模式；无法唯一解析时计零并保留 raw response | 持续测试改进 |
| Demo 分数被误当正式能力 | 页面、API/文档显著标记 Demo | Phase 3 正式数据集 |

## 交付物

- `backend/`：API、领域模型、迁移、Adapter、Evaluator、Runner 与测试。
- `frontend/`：六个可用页面、集中 API Client 与测试。
- `benchmarks/demo-general/`：manifest、12–20 道原创题和稳定 Hash。
- `scripts/`、`Makefile`、`compose.yaml`、`.env.example` 和 GitHub Actions。
- README、API、协议、数据格式、测试、部署、安全、状态和工作日志文档。

## 状态

`completed`（2026-08-25）。后端 130 项测试、前端 13 项测试、离线 Smoke、lint、typecheck、production build、Alembic 往返以及 Compose 双服务健康检查均已实际通过；setup 的未版本化旧库冲突已通过严格 schema 识别、一致性备份和无损迁移修复。详细证据见 [Bootstrap MVP 工作日志](../worklogs/2026-08-24-bootstrap-mvp.md) 与 [setup 修复工作日志](../worklogs/2026-08-25-fix-setup-alembic-conflict.md)。
