# LLMBenchLab REST API

本文档描述当前 MVP 的实际 HTTP 接口。API 前缀为 `/api/v1`，本地默认地址为
`http://127.0.0.1:8000/api/v1`。交互式 OpenAPI 文档位于 `/docs`，ReDoc 位于
`/redoc`，原始规范位于 `/openapi.json`。

Phase 2 保持了 `/api/v1` 和 `llmbenchlab-protocol-v1` 评分含义，将任务执行放入数据库租约驱动的独立 Worker，并增加数据库权威的本地 admission 治理、类型化审计与有界历史查询。Phase 2 仍为 `in_progress`；这些接口不代表公网安全、生产 HA、Provider 账单真值、崩溃后真实在途请求硬上限或密码学不可篡改审计。

> MVP 没有身份认证或权限控制，只适合受信任的本机环境。不要把服务直接暴露到公网。

## 1. 通用约定

- 除 Benchmark ZIP 上传外，请求与响应均使用 `application/json`。
- 所有持久化时间以 UTC 产生；示例使用 ISO 8601 的 `Z` 形式。客户端显示时应明确时区。
- 资源 ID 是 36 字符 UUID 字符串。下面 UUID、模型名和地址为示例值；`demo-general` 的内容与 Dataset Hash 使用仓库内置数据的真实 canonical 值。
- Model 写接口接受 Web 使用的 write-only `api_key`，也保留 `api_key_env` 环境变量兼容模式；Model 读响应只公开 `credential_source`、`has_api_key` 和兼容模式的变量名称，绝不返回 Key、密文、nonce、加密 key id 或部署 keyring 内容。
- `score`、`completion_rate` 和 `answered_accuracy` 的单位均为百分比 `0..100`；逐题
  `score` 为 `0..1`。
- Token usage 或费用无法从上游取得时为 `null`，不能解释为零。
- 服务为每个请求始终生成全新的 server-side UUID，并在所有响应（包括通用 500）的 `X-Request-ID` header 中回传。客户端传入的同名 header 会被忽略，且 CORS 不允许它作为请求 header；浏览器仍可读取响应中暴露的 `X-Request-ID`。它用于诊断关联，不是请求幂等键。治理 policy 限制 Run backlog 与 Provider attempt admission，不是面向 HTTP 客户端的认证或 per-client API rate limit。

### 1.1 分页

以下列表接口采用 offset pagination：

- `GET /models`
- `GET /benchmarks`
- `GET /benchmarks/{benchmark_id}/questions`
- `GET /runs`
- `GET /runs/{run_id}/responses`
- `GET /runs/{run_id}/audit`
- `GET /leaderboard`

| 参数 | 默认值 | 约束 | 含义 |
| --- | ---: | --- | --- |
| `offset` | `0` | 整数，`>= 0` | 跳过的记录数 |
| `limit` | `20` | 整数，`1..100` | 本页最多记录数 |

统一响应包络：

```json
{
  "items": [],
  "total": 0,
  "offset": 0,
  "limit": 20
}
```

`total` 是应用筛选条件后的总数，而不是本页数量。MVP 不提供游标分页或 `next` 链接。

### 1.2 错误格式

业务错误由 `detail` 包裹：

```json
{
  "detail": {
    "code": "model_not_found",
    "message": "Model was not found"
  }
}
```

请求 Schema 或查询参数校验失败使用 FastAPI 的 `422` 格式；`loc` 指向错误位置：

```json
{
  "detail": [
    {
      "type": "less_than_equal",
      "loc": ["query", "limit"],
      "msg": "Input should be less than or equal to 100"
    }
  ]
}
```

应用主动从 422 响应中省略 Pydantic 的 `input` 与 `ctx`，并把未知字段位置折叠为安全占位符，避免反射可能敏感的原始输入。Web/API 调用方只能在 Model 写请求定义的 `api_key` 字段传真实 Key；不得把它放进未知字段、URL、query、模型名、Base URL、默认参数或其他公开字段。

Benchmark 校验错误还会提供文件、行、列或 JSON Pointer：

```json
{
  "detail": {
    "code": "dataset_validation_error",
    "message": "Benchmark dataset validation failed",
    "issues": [
      {
        "file": "questions.jsonl",
        "code": "invalid_json",
        "message": "Expecting value",
        "line": 3,
        "column": 24
      }
    ]
  }
}
```

未捕获的服务端异常返回不包含异常文本或内部细节的 `500`：

```json
{
  "detail": {
    "code": "internal_server_error",
    "message": "An internal server error occurred"
  }
}
```

响应 header 中包含对应 `X-Request-ID`。调用方不应依赖英文 `message` 做分支，应优先使用稳定的业务 `code` 与 HTTP 状态码。

## 2. 接口总览

| 方法 | 路径 | 成功状态 | 说明 |
| --- | --- | ---: | --- |
| GET | `/live` | 200 | API 进程存活，不访问外部依赖 |
| GET | `/health` | 200 | 本地 API/数据库健康检查 |
| GET | `/ready` | 200/503 | 数据库、Alembic head 和 Redis 组件就绪状态 |
| GET | `/tasks/metrics` | 200 | 数据库当前任务 gauges |
| GET | `/tasks/history` | 200 | 有界 UTC 窗口内的类型化事件 counters 与 Run 延迟分位数 |
| GET | `/metrics/prometheus` | 200 | 固定低基数 Prometheus text exposition；不接受查询参数 |
| GET | `/info` | 200 | 服务、协议和能力信息 |
| GET | `/governance/policy` | 200/404 | 只读查看当前治理策略；未初始化时不产生写入 |
| PUT | `/governance/policy` | 200 | 原子应用一份完整、版本化的治理策略 |
| GET | `/models` | 200 | 分页列出模型 |
| POST | `/models` | 201 | 注册模型 |
| GET | `/models/{model_id}` | 200 | 获取模型 |
| PATCH | `/models/{model_id}` | 200 | 部分更新模型 |
| DELETE | `/models/{model_id}` | 204 | 删除未被 Run 引用的模型 |
| GET | `/benchmarks` | 200 | 分页列出 Benchmark |
| GET | `/benchmarks/{benchmark_id}` | 200 | 获取 Benchmark |
| GET | `/benchmarks/{benchmark_id}/questions` | 200 | 分页读取题目 |
| POST | `/benchmarks/import` | 201 | 上传并导入 Benchmark ZIP |
| POST | `/benchmarks/reload-demo` | 200 | 幂等载入内置 Demo |
| GET | `/runs` | 200 | 分页列出 Run |
| POST | `/runs` | 202 | 持久化 Run 并 best-effort 发送 Worker 通知 |
| GET | `/runs/{run_id}` | 200 | 轮询 Run 状态与汇总 |
| POST | `/runs/{run_id}/cancel` | 200 | 请求协作式取消 |
| GET | `/runs/{run_id}/responses` | 200 | 分页读取逐题证据 |
| GET | `/runs/{run_id}/progress` | 200 | 读取固定 512 题 block 索引与同快照 live metrics |
| GET | `/runs/{run_id}/progress/blocks/{block_index}` | 200 | 读取一个 block 的轻量 absolute-position cells |
| GET | `/runs/{run_id}/audit` | 200 | 按稳定时间顺序分页读取保留期内的类型化审计事件 |
| GET | `/leaderboard` | 200 | 已完成 Run 的严格总分榜 |
| GET | `/metrics/summary` | 200 | Dashboard 汇总 |

## 3. 系统接口

### 3.1 `GET /live`

纯 API 进程存活检查，只读已缓存的配置，不访问数据库、Redis 或 Provider。

```bash
curl -sS http://127.0.0.1:8000/api/v1/live
```

`200 OK`：

```json
{
  "status": "live",
  "version": "0.1.0",
  "timestamp": "2026-08-25T06:00:00Z"
}
```

### 3.2 `GET /health`

只检查 API 和本地数据库，不访问任何模型 Provider，因此不需要 API Key，也不会产生模型费用。

请求：

```bash
curl -sS http://127.0.0.1:8000/api/v1/health
```

`200 OK`：

```json
{
  "status": "ok",
  "database": "ok",
  "version": "0.1.0",
  "timestamp": "2026-08-24T08:00:00Z"
}
```

数据库不可用时返回 `503 Service Unavailable`：

```json
{
  "detail": {
    "code": "database_unavailable",
    "message": "Database health check failed"
  }
}
```

### 3.3 `GET /ready`

就绪检查并行验证数据库连接、当前 Alembic heads 与 Redis ping，不访问 Provider，也不等待任何 Run 完成。

```bash
curl -sS http://127.0.0.1:8000/api/v1/ready
```

数据库在 head 且 Redis 可用时返回 `200 OK`：

```json
{
  "status": "ready",
  "database": "ok",
  "schema": "ok",
  "queue": "ok",
  "accepting_runs": true,
  "database_reconciliation": "available",
  "errors": [],
  "version": "0.1.0",
  "timestamp": "2026-08-25T06:00:00Z"
}
```

Redis 不可用而数据库/head 可用时返回 `503 Service Unavailable`，但语义是可恢复的队列降级：

```json
{
  "status": "degraded",
  "database": "ok",
  "schema": "ok",
  "queue": "unavailable",
  "accepting_runs": true,
  "database_reconciliation": "available",
  "errors": ["queue_unavailable"],
  "version": "0.1.0",
  "timestamp": "2026-08-25T06:00:00Z"
}
```

此时 `POST /runs` 仍可先持久化并返回 `202`，独立 Worker 可仅靠数据库对账执行。数据库不可用或 schema 不在 head 时返回 `503`/`not_ready`，`accepting_runs=false`、`database_reconciliation=unavailable`。Redis URL 未配置的本地 DB-only 模式显示 `queue=disabled`，数据库/head 正常即为 `ready`。

数据库检查通过 `asyncio.to_thread` 运行，readiness timeout 只限制 HTTP 等待时间，不能取消已进入线程的同步驱动调用。实际连接/资源上界仍由数据库 driver 与 pool timeout 约束。

### 3.4 `GET /tasks/metrics`

返回数据库当前任务事实派生的 gauges：

```bash
curl -sS http://127.0.0.1:8000/api/v1/tasks/metrics
```

```json
{
  "pending": 2,
  "due_pending": 1,
  "running": 1,
  "expired_running": 0,
  "active_cancellation_requests": 0,
  "retry_scheduled": 1,
  "dead_lettered": 0,
  "runs_with_queue_notification_error": 1,
  "managed_backlog": 3,
  "governance_delayed": 1,
  "governance_exhausted": 0,
  "active_provider_attempts": 2,
  "overdrawn_governance_scopes": 0,
  "total_attempts": 3,
  "total_failed_attempts": 1,
  "total_dispatches": 4,
  "worker_expected_processes": 2,
  "worker_registered_processes": 2,
  "worker_live_processes": 2,
  "worker_stalled_processes": 0,
  "worker_shortfall_processes": 0,
  "worker_stale_after_seconds": 60.0,
  "worker_last_seen_at": "2026-08-25T05:59:59Z",
  "worker_last_scan_at": "2026-08-25T05:59:58Z",
  "worker_last_claim_at": "2026-08-25T05:59:50Z",
  "worker_last_progress_at": "2026-08-25T05:59:55Z",
  "worker_last_lease_heartbeat_at": "2026-08-25T05:59:57Z",
  "timestamp": "2026-08-25T06:00:00Z"
}
```

- `pending`：所有 pending Run；`due_pending` 还要求 `next_attempt_at` 与 `governance_not_before` 各自为空或已到期。
- `running` 与 `expired_running`：当前 running 以及按数据库时间已过租约的子集。
- `active_cancellation_requests`：尚在 pending/running 且已请求取消的 Run。
- `retry_scheduled`、`dead_lettered`：已安排后续 attempt 和已进入权威 dead-letter 终态的 Run。
- `runs_with_queue_notification_error`：`last_error=queue_notification_unavailable` 的当前 Run 数。
- `managed_backlog`：当前 `pending/running` 且已冻结治理 policy 的 Run；`governance_delayed` 与 `governance_exhausted` 分别是当前延迟和治理耗尽的 Run 数。
- `active_provider_attempts`：attempt ledger 中仍为 `reserved/send_started` 的行数；它是本地数据库 admission 事实，不承诺 Worker 崩溃后的 Provider 幽灵请求已经停止。
- `overdrawn_governance_scopes`：实际 usage 超过**显式 hard reservation** 的治理 scope 数，不是超额 attempt 数。无显式 `input_token_reservation` 时，输入估算不构成 input/cost overdraw；显式 `max_tokens` 的 output reservation 仍独立生效。
- `total_attempts`、`total_failed_attempts`、`total_dispatches`：Run 表中 lease 取得次数、实际 Run 级失败次数与公平调度 dispatch 次数的总和。
- `worker_expected_processes` 是部署显式声明的最小进程数；`registered/live/stalled/shortfall` 从未停止的 `worker_processes` generation 与同一 DB UTC cutoff 派生，`last_seen_at == cutoff` 仍为 live。
- 五个 `worker_last_*` 字段对未停止 generation 取最近（`MAX`）事实；空集合或尚未发生该事件时为 `null`。响应不包含 generation/worker ID，dependency probe 也不会写入或伪造这些时间。

这些都是查询时点的 DB-derived gauges，不是完整事件 counters、历史延迟、审计记录或监控面板；它们绝不能覆盖数据库任务状态。保留期内的有界历史聚合见下一节 `/tasks/history`。

### 3.5 `GET /tasks/history`

按数据库当前时间返回最近一个显式有界 UTC 窗口中的历史 counters 与延迟分布。`window_hours` 默认为 `24`，范围为 `1..2160`（最多 90 天）；窗口固定为半开区间 `[window_start, window_end)`。

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/tasks/history?window_hours=24'
```

```json
{
  "window_start": "2026-08-26T08:00:00Z",
  "window_end": "2026-08-27T08:00:00Z",
  "window_hours": 24,
  "event_counts": {
    "total": 42,
    "governance_policy_bootstrapped": 0,
    "governance_policy_applied": 1,
    "run_admitted": 4,
    "run_claimed": 8,
    "run_cancel_requested": 0,
    "run_deferred": 2,
    "run_yielded": 3,
    "run_terminal": 4,
    "run_retry_scheduled": 1,
    "run_dead_lettered": 0,
    "run_lease_reconciled": 0,
    "provider_attempt_reserved": 5,
    "provider_attempt_send_started": 5,
    "provider_attempt_settled": 5,
    "question_evidence_persisted": 3,
    "queue_notification": 1,
    "governance_integrity_error": 0
  },
  "queue_latency": {
    "sample_count": 4,
    "truncated": false,
    "p50_ms": 120.5,
    "p95_ms": 220.25,
    "p99_ms": 228.05
  },
  "execution_latency": {
    "sample_count": 4,
    "truncated": false,
    "p50_ms": 804.5,
    "p95_ms": 1200.25,
    "p99_ms": 1232.05
  },
  "end_to_end_latency": {
    "sample_count": 4,
    "truncated": false,
    "p50_ms": 925.0,
    "p95_ms": 1400.5,
    "p99_ms": 1432.1
  },
  "latency_sample_limit": 10000,
  "timestamp": "2026-08-27T08:00:00Z"
}
```

`event_counts` 只聚合 retained、具有唯一 `event_key` 的固定任务/治理 `AuditEvent.event_type`；不读取应用日志，也不把空的 `duration_ms` 当作观测。每条候选事件在计数前重新校验 event contract、payload hash、identity、90/365-day retention 和数值边界；任一损坏行使整个请求 fail closed 为 `500 audit_event_integrity_error`，不返回部分 counter，也不反射损坏值。`total` 是响应中列出的这些类型之和，不包括 credential/security 事件。

三个延迟分布直接来自 `EvaluationRun` 的持久时间戳：queue 样本以 `started_at` 落入窗口为准并计算 `started_at-created_at`；execution 与 end-to-end 样本以 `finished_at` 落入窗口为准，分别计算 `finished_at-started_at` 与 `finished_at-created_at`。尚未开始或完成的 Run 不伪造零值，时间倒置的损坏行也不进入样本。p50/p95/p99 使用确定性线性插值。

窗口终点、audit 校验/counter 与三组 Run 延迟都在同一数据库读取快照内完成：PostgreSQL 使用 `REPEATABLE READ READ ONLY`，SQLite 显式开启读事务。因此同一响应不会把不同提交时点的 event 与 Run timestamp 混合；这仍不是跨请求的历史快照或 WORM 保证。每种延迟最多按观测时间、Run ID 的稳定顺序读取最早 10,000 个样本；`sample_count` 是实际参与计算的数量。若还有更多样本，`truncated=true`，分位数只描述这 10,000 个样本，不能当作完整窗口统计。audit counters 不受 latency 样本上限影响。`window_hours` 越界返回 `422`。

### 3.6 `GET /metrics/prometheus`

返回固定 Prometheus text format `0.0.4`：

```bash
curl -sS http://127.0.0.1:8000/api/v1/metrics/prometheus
```

成功状态为 `200`，`Content-Type: text/plain; version=0.0.4; charset=utf-8`、`Cache-Control: no-store`、LF 和唯一末尾换行。接口不接受任何 query parameter；存在参数时返回 `422 metrics_query_parameters_not_allowed`。同一 API 进程已有 collection 时立即返回 `429 metrics_scrape_in_progress`，不排队。

一次 collection 在同一数据库读快照和同一 DB UTC 下取得 current Run/governance gauges、固定 15 分钟 typed-audit window、固定 1 小时 Run latency 与 Worker aggregate。audit 最多读取 `50,001` 行；第 `50,001` 行使整个请求返回 `503 metrics_observation_limit_exceeded`，不截断 counter。每类 latency 最多读取 `10,001` 个样本，输出前 10,000 个的 p50/p95/p99 与 `truncated=1`。任一 retained audit 的 contract/hash/identity/retention/数值损坏返回 `500 audit_event_integrity_error`；数据库失败返回 `503 metrics_database_unavailable`；renderer 拒绝负数/非有限值并返回 `500 metrics_rendering_error`。所有失败都不返回部分 exposition或异常原文。

Redis ping 在数据库快照外，只影响 `llmbenchlab_queue_configured`/`llmbenchlab_queue_available`；Redis down 不会让数据库指标失败。所有 metric type 都是 `gauge`。labels 仅为固定的 `event_type`、latency `phase/quantile`、Worker `state/activity`；不输出 Run、Model、Provider、Worker、Question、policy、URL、hash 或错误文本。JSON `/tasks/metrics` 的 Worker 时间取 active generation 的 `MAX`；Prometheus `llmbenchlab_worker_activity_oldest_age_seconds` 取 `MIN` 后计算最老 age，二者语义不同。完整 family、示例 scrape 和八条规则见 [ADR-0015](decisions/ADR-0015-observability-worker-progress-audit-retention.md) 与 `deploy/observability/`。

### 3.7 `GET /info`

请求：

```bash
curl -sS http://127.0.0.1:8000/api/v1/info
```

`200 OK`：

```json
{
  "name": "LLMBenchLab",
  "version": "0.1.0",
  "api_version": "v1",
  "protocol_version": "llmbenchlab-protocol-v1",
  "environment": "development",
  "capabilities": {
    "providers": ["mock", "openai_compatible", "openai_responses", "anthropic_messages"],
    "question_types": ["exact_match", "multiple_choice", "numeric"],
    "runner": "independent_database_lease_worker"
  }
}
```

### 3.8 `GET/PUT /governance/policy`

该接口没有认证，只允许可信 loopback 运维调用。`GET` 是安全只读操作：数据库尚无 policy 时返回 `404 governance_policy_not_initialized`，不会顺带创建 policy、scope 或 audit 行。

`PUT` 必须发送全部 20 个 policy 字段；省略任何字段或发送额外字段都返回 `422`。可选 limit 中 `null` 表示关闭该维度，数值 `0` 表示拒绝新的对应 admission，正数表示上限；`backlog_limit` 必须是非负严格整数，`question_quantum` 必须是正严格整数。布尔值和浮点数不能冒充整数。并发/backlog/quantum 范围是 `0..2147483647`（quantum 从 `1` 开始）；请求/Token/累计计数范围是 `0..9223372036854775807`。两个 USD policy 字段接受 JSON number 或十进制字符串，为避免客户端浮点换算建议发送字符串；有效范围是 `0..10000000.00000000` USD，最多 8 位小数。

```bash
curl -sS -X PUT http://127.0.0.1:8000/api/v1/governance/policy \
  -H 'Content-Type: application/json' \
  -d '{
    "global_concurrency_limit":8,
    "provider_concurrency_limit":4,
    "model_concurrency_limit":2,
    "run_concurrency_limit":2,
    "global_requests_per_minute":120,
    "provider_requests_per_minute":60,
    "model_requests_per_minute":30,
    "run_requests_per_minute":20,
    "global_tokens_per_minute":200000,
    "provider_tokens_per_minute":100000,
    "model_tokens_per_minute":50000,
    "run_tokens_per_minute":25000,
    "global_lifetime_request_budget":100000,
    "global_lifetime_token_budget":100000000,
    "global_lifetime_cost_budget_usd":"1000.00000000",
    "run_lifetime_request_budget":1000,
    "run_lifetime_token_budget":1000000,
    "run_lifetime_cost_budget_usd":"10.00000000",
    "backlog_limit":1000,
    "question_quantum":25
  }'
```

首次 `PUT` 会在同一串行化事务中建立确定性的 unlimited v1 基线，再激活请求内容；新内容获得递增 version，相同内容幂等返回当前行，重用历史内容会重新激活其原 ID/version，而不是修改不可变 policy 内容。数据库 partial unique invariant 保证最多一个 active policy。响应包含 `id`、`version`、`policy_hash`、`is_active`、上述 20 个字段及 `activated_at/created_at`，并带 `Cache-Control: no-store`。所有 `Decimal` USD 响应值以 JSON string 返回（例如 `"1000.00000000"`），不以 JSON 浮点数破坏 8 位精度。PostgreSQL 使用精确 `NUMERIC(20,8)`；公开上限另限制为 1000 万 USD，使 SQLite 的 IEEE-754 numeric affinity 在该范围内的相邻间距仍小于半个 `1e-8` 存储量化单位，因而受理值能按同一 8 位小数往返。

所有适用的 global/provider/model/run 限制必须同时满足。`policy_hash` 是全部 20 个规范化限制字段的内容指纹；policy 读取和 attempt admission 都会重算校验。该策略裁决数据库中的 reservation、fixed-minute 与 lifetime 本地 admission；`0` 的 stop-the-world 语义不会撤销已经外发的 Provider 请求。每次 admission/结算/对账前，锁定的 scope 和 minute bucket 物化值会与 never-delete ledger 重算值比较；任何高或低漂移均 fail closed。对外 API/Worker 边界会返回/保留稳定 `governance_integrity_error`，并尽力通过独立短事务追加一个不含异常文本或损坏值的类型化事件。

## 4. Model

### 4.1 Model Schema

创建字段：

| 字段 | 类型/默认值 | 规则 |
| --- | --- | --- |
| `name` | string，必填 | 去首尾空白后 `1..160`，全库唯一 |
| `provider_type` | `mock`、`openai_compatible`、`openai_responses` 或 `anthropic_messages`，必填 | 显式 Adapter 封闭集合；旧 `openai_compatible` 继续表示 Chat Completions |
| `base_url` | string/null | 绝对 URL；远端只允许 HTTPS，明文 HTTP 仅允许 loopback；禁止 URL 内嵌账号密码、query 与 fragment |
| `remote_model_name` | string/null | 最长 256 |
| `api_key` | string/null | **仅写入**；8–8192 bytes、无首尾空白、只含可见 ASCII；OpenAPI 标记 `writeOnly`，所有响应均省略 |
| `api_key_env` | string/null | 兼容 CLI/旧客户端的环境变量名，不是密钥值；不能与 `api_key` 同时提供 |
| `enabled` | boolean，默认 `true` | 禁用模型不能创建 Run |
| `input_price_per_million` | number/null，默认 `null` | 有限非负数；Mock 未填时规范化为明确的 `0` |
| `output_price_per_million` | number/null，默认 `null` | 有限非负数；Mock 未填时规范化为明确的 `0` |
| `default_parameters` | object，默认 `{}` | 只允许 `temperature`、`top_p`、`max_tokens`、`seed`；其中 `max_tokens` 可为 `null` 或 `1..131072`，其余字段使用与 Run 相同的类型/范围约束 |

三个远程类型都必须同时提供 `base_url`、`remote_model_name`，并在 `api_key` 与 `api_key_env` 中恰好选择一个；`mock` 的四个远端连接/凭据字段必须为空。协议必须显式选择：`openai_compatible`、`openai_responses`、`anthropic_messages` 分别使用 `/chat/completions`、`/responses`、`/messages`。根地址会追加所选后缀；与所选类型一致的完整 endpoint 保持不变；其他已知协议后缀在任何网络请求前拒绝。Model Schema、Provider preflight 和 Adapter 都拒绝远端明文 HTTP，只有 `localhost` 或字面量 loopback IP 可使用 HTTP；HTTPS 私网、云元数据、DNS rebinding 和其他出站目标仍没有 allowlist，详见 [SECURITY.md](SECURITY.md)。

读响应额外包含两个派生字段：`credential_source` 为 `none | environment | stored`；`has_api_key` 只表示该 Model 当前拥有应用加密保存的 Web Key。环境变量模式即使 Worker 环境中已有值也仍返回 `has_api_key=false`。`stored` 模式在独立 `model_credentials` 行中以 `model_id` 为主键保存 AES-GCM envelope，Model/Run/Response Schema 均不映射其内部列。

本 API 的 `GET /models` 是 LLMBenchLab 本地模型注册表，不会代替操作者访问 Provider。可信本地 `llmbenchlab-evaluate` 才会调用上游 `/models` 与付费 canary：三个已知 endpoint 后缀都回到同级 `/models`，discovery 按显式协议鉴权（Chat/Responses 使用 `Authorization: Bearer`，Messages 使用 `x-api-key` 与 `anthropic-version`）；Messages 的 `has_more/last_id` 通过 `after_id` 分页，并受累计 100 页、60 秒 wall-clock、10,000 项、2 MiB 与缺失/重复 cursor 门禁保护。发现到的任一模型 ID 若包含当前 Key，预检立即失败；canary 成功体若明确返回不同于请求目标的模型名，也会失败。模型发现与正式请求声明 `Accept-Encoding: identity` 并拒绝其他响应编码。Chat、Responses、Messages 的内部流式传输分别以 `[DONE]`、`response.completed`、`message_stop` 为成功终止证据；这不是新的 LLMBenchLab 公开 SSE API。各协议普通 JSON 成功响应继续兼容。Provider 普通 JSON 成功体上限为 4 MiB，SSE 累计 wire 上限为 64 MiB、单事件上限为 1 MiB、最终聚合 content 上限为 4 MiB，非 2xx 错误体上限为 64 KiB。成功内容、raw usage 的对象键/所有 JSON 标量、token/status 数值、request ID、返回模型名、system fingerprint 与 finish reason 中出现的当前 Key 会在进入持久化边界前按精确值替换为 `[REDACTED]`。SSE content 先完整聚合再替换，因此 Key 横跨多个 delta 也不会因分块而跳过该精确匹配。

Model 默认参数只覆盖上述四个生成字段。创建 Run 时，显式请求值优先；某字段未出现在请求 JSON 中时才使用 Model 默认值。为了兼容旧客户端，通用请求 Schema 仍显示 Chat 的 `temperature=0`、`top_p=1`、`seed=42` 默认；但 Responses/Messages 在请求与 Model 默认都没有显式提供这些字段时，会把三者归一化为 `null` 并从 Provider payload 省略，避免向不支持采样字段的模型发送参数。Chat Completions 把 `max_tokens` 原样发送；Responses 把它映射为 `max_output_tokens`；二者的 `null` 都表示省略该字段，由 Provider 决定默认输出预算，这不表示无限输出。Messages 必须使用有限 `max_tokens`，且显式 `temperature` 只能为 `0..1`。Responses/Messages 都不接受当前项目的非空 `seed`；非法组合在外发前稳定拒绝，不会静默忽略。Run 的 `generation` 快照保存最终有效值和显式 Adapter 类型。

Model 响应示例（后续接口引用为 `ModelRead`）：

```json
{
  "id": "11111111-1111-4111-8111-111111111111",
  "name": "Offline Mock",
  "provider_type": "mock",
  "base_url": null,
  "remote_model_name": null,
  "api_key_env": null,
  "credential_source": "none",
  "has_api_key": false,
  "enabled": true,
  "input_price_per_million": 0.0,
  "output_price_per_million": 0.0,
  "default_parameters": {},
  "created_at": "2026-08-24T08:01:00Z",
  "updated_at": "2026-08-24T08:01:00Z"
}
```

### 4.2 `GET /models`

筛选参数：`provider_type=mock|openai_compatible|openai_responses|anthropic_messages`、`enabled=true|false`，并支持通用分页。

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/models?provider_type=mock&enabled=true&offset=0&limit=20'
```

`200 OK`（`items` 元素为 `ModelRead`）：

```json
{
  "items": [
    {
      "id": "11111111-1111-4111-8111-111111111111",
      "name": "Offline Mock",
      "provider_type": "mock",
      "base_url": null,
      "remote_model_name": null,
      "api_key_env": null,
      "credential_source": "none",
      "has_api_key": false,
      "enabled": true,
      "input_price_per_million": 0.0,
      "output_price_per_million": 0.0,
      "default_parameters": {},
      "created_at": "2026-08-24T08:01:00Z",
      "updated_at": "2026-08-24T08:01:00Z"
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 20
}
```

无效枚举或分页值返回 `422`（见通用校验错误）。

### 4.3 `POST /models`

注册完全离线 Mock：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/models \
  -H 'Content-Type: application/json' \
  -d '{"name":"Offline Mock","provider_type":"mock","enabled":true}'
```

注册供 Web 使用的远程配置。推荐在 Models 页面粘贴 Key；`api_key` 只出现在这次写请求中，成功响应不会返回它，数据库也不会保存其明文。下面是 Responses 类型的请求结构，不是可直接填入真实 Key 的 shell 命令：

```json
{
  "name": "Local Responses",
  "provider_type": "openai_responses",
  "base_url": "https://llm-gateway.invalid/v1",
  "remote_model_name": "example-responses-model",
  "api_key": "<write-only value entered in the Web form>",
  "enabled": true,
  "input_price_per_million": null,
  "output_price_per_million": null,
  "default_parameters": {}
}
```

不要把真实 Key 写入 `curl -d`、命令行参数、脚本源码或 shell history。若必须编写 API 客户端，应从隐藏提示或受保护的 secret source 读入进程内存，再把 JSON body 直接发送到 loopback API。

可信本地 CLI 与旧客户端仍可省略 `api_key` 并改传 `"api_key_env":"LOCAL_COMPAT_API_KEY"`。这只保存变量名称并令 `credential_source="environment"`；Web 表单默认使用 `api_key`，令 `credential_source="stored"`、`has_api_key=true` 和 `api_key_env=null`。

`201 Created` 返回 `ModelRead` 并带 `Cache-Control: no-store`。错误：

- `409 model_name_conflict`：名称已存在。
- `422`：字段缺失、额外字段、非法 URL/环境变量名、非法 Key、同时提供两种凭据、远端明文 HTTP，或 Provider 必需字段不完整；响应不反射原始 Key。
- `503 credential_store_unavailable`：请求使用 `api_key` 但部署 keyring 缺失/不可读/无效，或 PATCH 要保留的 stored envelope 无法解密且没有显式新 Key；事务不保存 Model 或凭据。

```json
{
  "detail": {
    "code": "model_name_conflict",
    "message": "A model with this name already exists"
  }
}
```

### 4.4 `GET /models/{model_id}`

```bash
curl -sS http://127.0.0.1:8000/api/v1/models/11111111-1111-4111-8111-111111111111
```

`200 OK` 返回 `ModelRead`。不存在时：

```json
{
  "detail": {"code": "model_not_found", "message": "Model was not found"}
}
```

状态码为 `404 Not Found`。

### 4.5 `PATCH /models/{model_id}`

请求体为部分字段；服务端把改动与现有记录合并后重新做完整 Provider 校验。

```bash
curl -sS -X PATCH http://127.0.0.1:8000/api/v1/models/11111111-1111-4111-8111-111111111111 \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false,"default_parameters":{"temperature":0}}'
```

省略 `api_key` 会保留现有 stored credential；显式传 `api_key:null` 会返回 `422`，避免把清除操作误当成“保持不变”。传入新 `api_key` 会替换并重新加密旧值。create/PATCH 会把新 Key 与精确 `ModelRead` 全字段投影、Run snapshot 的 `model` 子投影比较；保留 stored 时只为同一比较解密旧 Key，因此不能把凭据流中的 Key 复制到这些以后可读取的 Model 表面。该保证不扫描与 Model 无关的 Benchmark/Question 内容，也不排除无关用户数据恰好包含相同文本。Provider 的规范化 origin（scheme、host、非默认 port）发生变化时必须同时重输 `api_key`；仅改变同一 origin 下的路径不需要重输。切换为 `mock` 会清空远端字段并删除 encrypted row；改传 `api_key_env` 切到兼容环境变量模式时也会删除 encrypted row。

若 stored row 缺失，或旧 envelope 因未知/退役 `key_id`、损坏密文无法解密，PATCH 仍允许在 active keyring 可用时通过**只修改凭据**显式提交新的有效 `api_key`，或只切换至 `mock`/legacy `api_key_env` 来清理它。恢复请求若同时改名称、价格、默认参数或其他无关公开字段，会返回 `422 credential_recovery_requires_isolated_update`；若仍要保留 `stored` 且没有新 Key，则返回稳定、无秘密的 `503 credential_store_unavailable`。这些拒绝都保持 Model 与 credential 原样。

只要该 Model 仍有 `pending` 或 `running` Run，Provider 类型、endpoint、远端模型或凭据来源/值等敏感更新均返回 `409 model_has_active_runs`。Run 创建和 Model 更新使用同一方言锁：PostgreSQL 对 Model 行执行 `SELECT ... FOR UPDATE`，SQLite 在读取 Model 前先取得数据库级 `BEGIN IMMEDIATE`，避免检查、快照与提交之间的竞态。SQLite 的数据库级竞争会短暂阻塞请求，只定位为低并发本地模式；生产或并发评测推荐 PostgreSQL。名称、展示/价格和默认参数等非敏感更新仍由 Run 快照隔离。

`200 OK` 返回更新后的 `ModelRead` 并带 `Cache-Control: no-store`。`404 model_not_found`、`409 model_name_conflict`、`409 model_has_active_runs`、`422 api_key_required_for_origin_change`、通用 `422` 和 `503 credential_store_unavailable` 均使用稳定且不含 Key 的响应。

### 4.6 `DELETE /models/{model_id}`

```bash
curl -i -X DELETE http://127.0.0.1:8000/api/v1/models/11111111-1111-4111-8111-111111111111
```

未被历史 Run 引用时返回 `204 No Content`，无响应体。不存在返回 `404 model_not_found`。
为保留可复现证据，只要模型被任一 Run 引用就返回 `409`：

```json
{
  "detail": {
    "code": "model_has_runs",
    "message": "Model is referenced by historical runs and cannot be deleted"
  }
}
```

## 5. Benchmark

Benchmark 响应示例（后续接口引用为 `BenchmarkRead`）：

```json
{
  "id": "22222222-2222-4222-8222-222222222222",
  "slug": "demo-general",
  "name": "Demo General / 通用演示集",
  "version": "1.0.0",
  "description": "Demo 数据，不代表正式模型能力。Demo data only; results must not be presented as a formal measure of model capability. 本数据集仅用于验证 LLMBenchLab 的离线端到端链路。",
  "dimension": "general",
  "language": "mul",
  "license": "MIT",
  "source": "Original bilingual demonstration questions authored for LLMBenchLab",
  "evaluator_type": "builtin-objective",
  "evaluator_config": {
    "name": "builtin-objective",
    "version": "1.0",
    "mapping": {
      "exact_match": "exact_match_v1",
      "multiple_choice": "multiple_choice_v1",
      "numeric": "numeric_v1"
    }
  },
  "prompt_template": {
    "system": "你正在运行一个离线演示评测。Follow the requested answer format and return only the short final answer.",
    "user": "{prompt}\n{choices}"
  },
  "schema_version": "llmbenchlab-dataset-v1",
  "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
  "question_count": 15,
  "is_demo": true,
  "created_at": "2026-08-24T08:02:00Z"
}
```

完整文件格式和 Hash 规则见 [DATASET_FORMAT.md](DATASET_FORMAT.md)。

### 5.1 `GET /benchmarks`

筛选参数：`dimension`、`language`、`is_demo=true|false`，并支持通用分页。

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/benchmarks?dimension=general&is_demo=true&limit=20'
```

`200 OK`：

```json
{
  "items": [
    {
      "id": "22222222-2222-4222-8222-222222222222",
      "slug": "demo-general",
      "name": "Demo General / 通用演示集",
      "version": "1.0.0",
      "description": "Demo 数据，不代表正式模型能力。Demo data only; results must not be presented as a formal measure of model capability. 本数据集仅用于验证 LLMBenchLab 的离线端到端链路。",
      "dimension": "general",
      "language": "mul",
      "license": "MIT",
      "source": "Original bilingual demonstration questions authored for LLMBenchLab",
      "evaluator_type": "builtin-objective",
      "evaluator_config": {"name":"builtin-objective","version":"1.0","mapping":{"exact_match":"exact_match_v1","multiple_choice":"multiple_choice_v1","numeric":"numeric_v1"}},
      "prompt_template": {"system":"你正在运行一个离线演示评测。Follow the requested answer format and return only the short final answer.","user":"{prompt}\n{choices}"},
      "schema_version": "llmbenchlab-dataset-v1",
      "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
      "question_count": 15,
      "is_demo": true,
      "created_at": "2026-08-24T08:02:00Z"
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 20
}
```

分页值非法时返回 `422`。

### 5.2 `GET /benchmarks/{benchmark_id}`

```bash
curl -sS http://127.0.0.1:8000/api/v1/benchmarks/22222222-2222-4222-8222-222222222222
```

`200 OK` 返回 `BenchmarkRead`。不存在时返回 `404`：

```json
{
  "detail": {"code": "benchmark_not_found", "message": "Benchmark was not found"}
}
```

### 5.3 `GET /benchmarks/{benchmark_id}/questions`

支持 `question_type=exact_match|multiple_choice|numeric` 和通用分页。MVP 返回参考答案与
metadata，因此该接口只应对受信任的本地用户开放。

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/benchmarks/22222222-2222-4222-8222-222222222222/questions?question_type=multiple_choice&limit=1'
```

`200 OK`：

```json
{
  "items": [
    {
      "id": "33333333-3333-4333-8333-333333333333",
      "benchmark_id": "22222222-2222-4222-8222-222222222222",
      "external_id": "demo-choice-001",
      "position": 5,
      "question_type": "multiple_choice",
      "prompt": "Which option is the English translation of “月亮”? Reply with one option letter.",
      "choices": {"A": "river", "B": "moon", "C": "window", "D": "bread"},
      "reference_answer": "B",
      "evaluator_config": {},
      "metadata": {"topic": "bilingual-vocabulary", "language": "mul", "difficulty": "demo", "demo": true, "mock_response": "B"}
    }
  ],
  "total": 5,
  "offset": 0,
  "limit": 1
}
```

Benchmark 不存在返回 `404 benchmark_not_found`；分页非法返回 `422`。

### 5.4 `POST /benchmarks/import`

请求为 `multipart/form-data`，表单字段名必须为 `archive`。ZIP 必须恰好包含根目录下的
`manifest.json` 与 `questions.jsonl`，不得包含目录或额外文件。

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/benchmarks/import \
  -F 'archive=@./my-benchmark.zip;type=application/zip'
```

`201 Created` 返回 `BenchmarkRead`。同一 `slug + version + dataset_hash` 再次导入是幂等的，
当前仍返回 `201` 和既有记录；同一 `slug + version` 但 Hash 不同返回 `409`：

```json
{
  "detail": {
    "code": "benchmark_version_conflict",
    "message": "The benchmark slug/version already exists with a different dataset hash"
  }
}
```

其他错误：

- `413 archive_too_large`：上传体超过 130 MiB。解压后的 `questions.jsonl` 上限为
  128 MiB，题数上限为 20,000；逐行和压缩比限制仍会独立校验。
- `415 zip_required`：Content-Type 不是受支持的 ZIP 类型。
- `422 dataset_validation_error`：ZIP 路径/压缩比、文件大小、JSON、Schema 或跨字段校验失败。
- `422`：缺少 `archive` 表单字段。

### 5.5 `POST /benchmarks/reload-demo`

不需要请求体，幂等载入仓库内置 `benchmarks/demo-general`：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/benchmarks/reload-demo
```

`200 OK` 返回 `BenchmarkRead`，其中 `is_demo=true`。同版本同 Hash 已存在时返回既有记录；
同版本内容冲突返回 `409 demo_version_conflict`；内置文件损坏时返回
`422 dataset_validation_error`。

## 6. Evaluation Run

### 6.1 创建请求 Schema

| 字段 | 默认值 | 约束 |
| --- | ---: | --- |
| `model_id` | 必填 | 非空，最长 36 |
| `benchmark_id` | 必填 | 非空，最长 36 |
| `temperature` | `0.0` | `null` 或 `0..2`；Messages 非空值上限为 `1` |
| `top_p` | `1.0` | `null` 或 `>0` 且 `<=1` |
| `max_tokens` | `256` | `null` 或整数 `1..131072`；`null` 表示不发送该字段，由 Provider 决定默认值，并非无限输出 |
| `seed` | `42` | 32 位有符号整数或 `null`；Responses/Messages 只接受 `null` |
| `system_prompt` | `null` | 最长 4000；提供时覆盖 Benchmark system prompt |
| `concurrency` | `1` | `1..4`；快照值即实际执行并发度 |
| `input_token_reservation` | `null` | `null` 或严格整数 `1..10000000`；hard TPM/Token/费用启用时必须提供可证明的每题输入预留上界。`null` 时实现不会把 UTF-8/tokenizer 估算写成 hard reservation 或用它触发 input/cost overdraw |
| `lifetime_request_budget` | `null` | `null` 或严格整数 `0..1000000000`；覆盖该 Run 的累计请求预算，`0` 拒绝新 attempt |
| `lifetime_token_budget` | `null` | `null` 或严格整数 `0..10000000000000`；覆盖该 Run 的累计 Token 预算，`0` 拒绝新 attempt |
| `lifetime_cost_budget_usd` | `null` | `null` 或 `0..10000000.00000000` USD，最多 8 位小数；请求接受 JSON number/十进制 string，`RunRead` 的非空响应始终是 JSON string；费用硬边界还要求显式 Token 上界和冻结价格 |
| `read_timeout_seconds` | `60` | 有限数字 `1..1800`；冻结为等待 Provider 下一批响应字节的空闲读取超时，不是请求总墙钟时限 |

保留 `max_tokens=256` 以及表中 Chat 采样值作为通用 API/protocol-v1 Schema 默认是兼容要求。创建 Responses/Messages Run 时，省略且没有 Model 默认的 `temperature`、`top_p`、`seed` 会冻结为 `null` 并从上游 payload 省略；这和客户端显式发送采样值不同。Web 在新协议下把 `temperature`/`top_p` 留空，seed 禁用；当 Model 没有输出默认且用户尚未手动修改时，新建评测表单仍根据已知 Benchmark 预填更适合长推理的显式输出建议。它们是可编辑的客户端起点，不会改变省略字段时的 API 默认：

| Web Benchmark | 建议 `max_tokens` | 建议 `read_timeout_seconds` |
| --- | ---: | ---: |
| Demo | `256` | `60` |
| MMLU-Pro `direct` | `1024` | `180` |
| MMLU-Pro `official_cot` | `4000` | `300` |
| GPQA-Diamond | `8192` | `600` |

Chat/Responses 的 Web 表单还允许显式选择“由 Provider 决定”，此时提交 `max_tokens:null`；Messages 必须保留有限正整数输出上限。更高数字、Provider 托管或更长读取超时都不是 Token/金额预算，也不证明模型支持对应输出长度。

Run 响应示例（后续接口引用为 `RunRead`）：

```json
{
  "id": "44444444-4444-4444-8444-444444444444",
  "model_id": "11111111-1111-4111-8111-111111111111",
  "benchmark_id": "22222222-2222-4222-8222-222222222222",
  "status": "completed",
  "protocol_version": "llmbenchlab-protocol-v1",
  "model_parameters_snapshot": {
    "generation": {"temperature": 0.0, "top_p": 1.0, "max_tokens": 256, "seed": 42},
    "model": {"id": "11111111-1111-4111-8111-111111111111", "name": "Offline Mock", "remote_model_name": null, "adapter_type": "mock", "base_url": null, "credential_source": "none", "api_key_env": null, "input_price_per_million": "0", "output_price_per_million": "0", "currency_assumption": "USD", "default_parameters": {}},
    "benchmark": {"id": "22222222-2222-4222-8222-222222222222", "slug": "demo-general", "name": "Demo General / 通用演示集", "version": "1.0.0", "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe", "question_count": 15, "is_demo": true, "schema_version": "llmbenchlab-dataset-v1", "source": "Original bilingual demo authored for LLMBenchLab", "license": "MIT", "dimension": "general", "language": "zh-en"},
    "evaluator": {"name": "builtin-objective", "version": "1.0", "mapping": {"exact_match": "exact_match_v1", "multiple_choice": "multiple_choice_v1", "numeric": "numeric_v1"}},
    "execution": {"concurrency": 1, "timeouts_seconds": {"connect": 5.0, "read": 60.0, "write": 30.0, "pool": 5.0}, "retry_policy": {"name": "bounded_exponential_backoff", "max_retries": 2, "max_attempts": 3, "backoff_base_seconds": 0.25, "backoff_cap_seconds": 2.0, "retryable_status_codes": [408, 429, 500, 502, 503, 504]}, "task_delivery": "at_least_once", "task_max_attempts": 3, "restart_recovery": "database_lease_resume_missing_responses"},
    "governance": {
      "policy_id": "77777777-7777-4777-8777-777777777777",
      "policy_version": 1,
      "policy_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "global_concurrency_limit": null,
      "provider_concurrency_limit": null,
      "model_concurrency_limit": null,
      "run_concurrency_limit": null,
      "global_requests_per_minute": null,
      "provider_requests_per_minute": null,
      "model_requests_per_minute": null,
      "run_requests_per_minute": null,
      "global_tokens_per_minute": null,
      "provider_tokens_per_minute": null,
      "model_tokens_per_minute": null,
      "run_tokens_per_minute": null,
      "global_lifetime_request_budget": null,
      "global_lifetime_token_budget": null,
      "global_lifetime_cost_budget_usd": null,
      "run_lifetime_request_budget": null,
      "run_lifetime_token_budget": null,
      "run_lifetime_cost_budget_usd": null,
      "backlog_limit": 1000,
      "question_quantum": 25,
      "provider_scope_key": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "local_admission_only": true,
      "run_overrides": {
        "input_token_reservation": null,
        "lifetime_request_budget": null,
        "lifetime_token_budget": null,
        "lifetime_cost_budget_usd": null
      }
    }
  },
  "benchmark_hash_snapshot": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
  "prompt_template_snapshot": {"system": "你正在运行一个离线演示评测。Follow the requested answer format and return only the short final answer.", "user": "{prompt}\n{choices}"},
  "code_commit_sha": null,
  "total_questions": 15,
  "completed_questions": 15,
  "correct_questions": 15,
  "error_questions": 0,
  "score": 100.0,
  "completion_rate": 100.0,
  "answered_accuracy": 100.0,
  "average_latency_ms": 1.0,
  "input_tokens": 120,
  "output_tokens": 30,
  "estimated_cost": 0.0,
  "cancellation_requested": false,
  "attempt_count": 1,
  "max_attempts": 3,
  "failed_attempt_count": 0,
  "dispatch_count": 1,
  "last_scheduled_at": "2026-08-24T08:03:01Z",
  "governance_policy_id": "77777777-7777-4777-8777-777777777777",
  "governance_status": "managed",
  "governance_reason": null,
  "governance_not_before": null,
  "input_token_reservation": null,
  "lifetime_request_budget": null,
  "lifetime_token_budget": null,
  "lifetime_cost_budget_usd": null,
  "lease_owner": null,
  "lease_token": 1,
  "lease_expires_at": null,
  "heartbeat_at": null,
  "next_attempt_at": null,
  "last_enqueued_at": "2026-08-24T08:03:00Z",
  "last_error": null,
  "dead_lettered_at": null,
  "started_at": "2026-08-24T08:03:01Z",
  "finished_at": "2026-08-24T08:03:02Z",
  "created_at": "2026-08-24T08:03:00Z",
  "error_message": null
}
```

状态集合：`pending`、`running`、`completed`、`failed`、`cancelled`。

Run 表没有 `credential_id`、ciphertext、nonce 或 keyring 列，Run API 也不会返回这些值。`model_parameters_snapshot.model` 只冻结 Model ID、`credential_source`、远端模型和 endpoint；只有 `environment` 模式会同时冻结 `api_key_env` 名称。执行 stored Run 时，Worker 用 `run.model_id` 读取 `model_credentials`，再以 `run.model_id + Run snapshot base_url` 作为认证上下文解密；它不会用当前可编辑 Model 的 Base URL 替代快照目标。缺失 keyring、未知 key id、密文篡改或 AAD 不匹配都会在构造 Adapter/网络请求前失败。

可靠执行字段的语义：

| 字段 | 含义 |
| --- | --- |
| `attempt_count` | 已成功取得 Run 租约的次数；从 0 开始，不是逐题 Provider retry 次数 |
| `max_attempts` | Run 租约 attempt 上限；新 Run 从 Worker 配置冻结 |
| `failed_attempt_count` | 真实 Run 级失败/异常过期租约次数；公平让出和治理延迟不会增加它 |
| `dispatch_count` / `last_scheduled_at` | 已领取的公平 slice 数量及最近调度数据库时间 |
| `governance_policy_id` | admission 时冻结的 policy；`null` 只用于 0004 前的 `legacy_unmanaged` Run |
| `governance_status` | `legacy_unmanaged`、`managed`、`delayed` 或 `exhausted` |
| `governance_reason` / `governance_not_before` | 稳定治理原因与 rate/concurrency 延迟后的最早重新调度数据库时间 |
| `input_token_reservation` | 显式冻结的每题输入 Token 上界；估算值不能替代 hard Token/费用边界。该字段为 `null` 时 actual input 仍保存，但 attempt 的 input reservation/reserved cost 为空 |
| `lifetime_*_budget` | 可选 Run 级 request/Token/USD 累计覆盖值；与冻结 policy 中更严格的适用边界共同生效 |
| `lease_owner` | 当前 Worker ID；只在 `running` 且租约活动时非空 |
| `lease_token` | 单调递增的 fencing generation；租约释放后保留最后值，防止旧 Worker 写入 |
| `lease_expires_at` / `heartbeat_at` | 由数据库时间裁决的租约截止点和最近心跳；非 running 时为 `null` |
| `next_attempt_at` | 可重试失败后的最早再领取时间；只用于 pending |
| `last_enqueued_at` | 最近一次 Redis 通知成功的数据库时间；为空不代表 Run 不可恢复 |
| `last_error` | 最近一次执行/通知层的稳定脱敏错误码；与终态展示的 `error_message` 不同 |
| `dead_lettered_at` | attempt 耗尽且 Response 集不完整时的权威 dead-letter 时间；只用于 failed |

`model_parameters_snapshot.governance` 不只保存 policy ID/hash：它冻结全部 20 个 policy 字段、opaque provider scope 和恰好四个 `run_overrides`（`input_token_reservation`、`lifetime_request_budget`、`lifetime_token_budget`、`lifetime_cost_budget_usd`）。每次 Provider attempt reserve 都会将它们与指定 policy 的重算 hash 及 Run 列比较，任何 policy/override 漂移都在外发前 fail closed，不会回退到新 active policy。

`model_parameters_snapshot.execution.retry_policy` 是每题 Adapter 的有限重试；`task_delivery`、`task_max_attempts` 和 `restart_recovery` 是 Run/Worker 恢复语义。新 Run 固定为 `at_least_once` 和 `database_lease_resume_missing_responses`，恢复时跳过已有 Response。只有成功持久化 `send_started` 或无法确认 send-start 结果的调用才消耗 Provider HTTP ordinal。明确的 `released_pre_send` 保留旧 ledger 终态，将 question execution 推进到新 ledger generation，并保留当前未发送 ordinal：若 attempt 1 未发送，下次仍是 1；若 attempt 1 已发送而 attempt 2 未发送，下次仍是 2，绝不重置已消耗的 attempt 1。generation 因而不是 HTTP 重试数。

Runner 在取得租约并启动心跳后，通过工作线程加载/物化数据库快照，避免大型 Benchmark 的同步加载阻塞事件循环而饿死心跳。完成、取消、defer/exhaust 以及 attempt 耗尽进入 dead-letter 的 Run 状态转换会先提交，然后对该 lease 的 active ledger 做独立对账；如果后续完整性校验失败，已提交的 Run 状态不被伪回滚，调用向上失败并以独立短事务记录最小 `governance_integrity_error`。过期 lease takeover 是更严的边界：新 lease 提交后若旧 ledger 对账失败，立即撤销新 owner 并使 Run fail closed（已有取消意图则收敛为 cancelled），不允许新 Worker 外发。终态前都会从已持久化 Response 聚合 Run 字段。这不保证 Provider 调用或计费 exactly-once；若进程在 Provider 响应后、本地提交前崩溃，可能再次调用 Provider，但数据库仍只保留一条计分/费用 Response 证据。

### 6.2 `GET /runs`

筛选参数：`model_id`、`benchmark_id`、`run_status`、`protocol_version`。注意参数名是
`run_status`，不是 `status`。

Web 的“评测记录”页面使用本接口按 20 条分页，可按状态筛选，并对当前页中的 `pending`/`running` Run 定时刷新；每一行都链接到持久化 Run ID 的详情页。因此离开正在执行的详情页后，可从主导航重新找到该 Run。

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/runs?run_status=completed&protocol_version=llmbenchlab-protocol-v1&limit=20'
```

`200 OK`：

```json
{
  "items": [
    {
      "id": "44444444-4444-4444-8444-444444444444",
      "model_id": "11111111-1111-4111-8111-111111111111",
      "benchmark_id": "22222222-2222-4222-8222-222222222222",
      "status": "completed",
      "protocol_version": "llmbenchlab-protocol-v1",
      "model_parameters_snapshot": {
        "generation": {"temperature": 0.0, "top_p": 1.0, "max_tokens": 256, "seed": 42},
        "model": {"id": "11111111-1111-4111-8111-111111111111", "name": "Offline Mock", "remote_model_name": null, "adapter_type": "mock", "base_url": null, "credential_source": "none", "api_key_env": null, "input_price_per_million": "0", "output_price_per_million": "0", "currency_assumption": "USD", "default_parameters": {}},
        "benchmark": {"id": "22222222-2222-4222-8222-222222222222", "slug": "demo-general", "name": "Demo General / 通用演示集", "version": "1.0.0", "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe", "question_count": 15, "is_demo": true, "schema_version": "llmbenchlab-dataset-v1", "source": "Original bilingual demo authored for LLMBenchLab", "license": "MIT", "dimension": "general", "language": "zh-en"},
        "evaluator": {"name": "builtin-objective", "version": "1.0", "mapping": {"exact_match": "exact_match_v1", "multiple_choice": "multiple_choice_v1", "numeric": "numeric_v1"}},
        "execution": {"concurrency": 1, "timeouts_seconds": {"connect": 5.0, "read": 60.0, "write": 30.0, "pool": 5.0}, "retry_policy": {"name": "bounded_exponential_backoff", "max_retries": 2, "max_attempts": 3, "backoff_base_seconds": 0.25, "backoff_cap_seconds": 2.0, "retryable_status_codes": [408, 429, 500, 502, 503, 504]}, "task_delivery": "at_least_once", "task_max_attempts": 3, "restart_recovery": "database_lease_resume_missing_responses"},
        "governance": {"policy_id": "77777777-7777-4777-8777-777777777777", "policy_version": 1, "policy_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "global_concurrency_limit": null, "provider_concurrency_limit": null, "model_concurrency_limit": null, "run_concurrency_limit": null, "global_requests_per_minute": null, "provider_requests_per_minute": null, "model_requests_per_minute": null, "run_requests_per_minute": null, "global_tokens_per_minute": null, "provider_tokens_per_minute": null, "model_tokens_per_minute": null, "run_tokens_per_minute": null, "global_lifetime_request_budget": null, "global_lifetime_token_budget": null, "global_lifetime_cost_budget_usd": null, "run_lifetime_request_budget": null, "run_lifetime_token_budget": null, "run_lifetime_cost_budget_usd": null, "backlog_limit": 1000, "question_quantum": 25, "provider_scope_key": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "local_admission_only": true, "run_overrides": {"input_token_reservation": null, "lifetime_request_budget": null, "lifetime_token_budget": null, "lifetime_cost_budget_usd": null}}
      },
      "benchmark_hash_snapshot": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
      "prompt_template_snapshot": {"system": "你正在运行一个离线演示评测。Follow the requested answer format and return only the short final answer.", "user": "{prompt}\n{choices}"},
      "code_commit_sha": null,
      "total_questions": 15,
      "completed_questions": 15,
      "correct_questions": 15,
      "error_questions": 0,
      "score": 100.0,
      "completion_rate": 100.0,
      "answered_accuracy": 100.0,
      "average_latency_ms": 1.0,
      "input_tokens": 120,
      "output_tokens": 30,
      "estimated_cost": 0.0,
      "cancellation_requested": false,
      "attempt_count": 1,
      "max_attempts": 3,
      "failed_attempt_count": 0,
      "dispatch_count": 1,
      "last_scheduled_at": "2026-08-24T08:03:01Z",
      "governance_policy_id": "77777777-7777-4777-8777-777777777777",
      "governance_status": "managed",
      "governance_reason": null,
      "governance_not_before": null,
      "input_token_reservation": null,
      "lifetime_request_budget": null,
      "lifetime_token_budget": null,
      "lifetime_cost_budget_usd": null,
      "lease_owner": null,
      "lease_token": 1,
      "lease_expires_at": null,
      "heartbeat_at": null,
      "next_attempt_at": null,
      "last_enqueued_at": "2026-08-24T08:03:00Z",
      "last_error": null,
      "dead_lettered_at": null,
      "started_at": "2026-08-24T08:03:01Z",
      "finished_at": "2026-08-24T08:03:02Z",
      "created_at": "2026-08-24T08:03:00Z",
      "error_message": null
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 20
}
```

无效 `run_status`、分页值返回 `422`。

### 6.3 `POST /runs`

创建顺序固定为：先用与 endpoint/credential 更新相同的方言锁读取并校验 Model，再在 global governance scope 锁内原子检查 managed backlog、冻结 active policy 与 Run override、写入 `pending` Run 及完整快照；提交后 best-effort 发布 Redis Streams 通知，然后返回 Run。PostgreSQL 使用 Model row `FOR UPDATE`；SQLite 在首次读取前使用 `BEGIN IMMEDIATE` 串行化写事务。API 不解密 stored credential、不加载 Adapter、不执行题目；独立 Worker 从 Redis 通知或数据库 reconciliation 获取工作。

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "model_id":"11111111-1111-4111-8111-111111111111",
    "benchmark_id":"22222222-2222-4222-8222-222222222222",
    "temperature":0,
    "top_p":1,
    "max_tokens":64,
    "seed":42,
    "concurrency":1,
    "input_token_reservation":4096,
    "lifetime_request_budget":100,
    "lifetime_token_budget":100000,
    "lifetime_cost_budget_usd":"5.00000000",
    "read_timeout_seconds":60
  }'
```

`202 Accepted` 返回 `RunRead`，通常 `status="pending"`。客户端应轮询 `GET /runs/{id}`，不能把 `202` 当作评测已完成。数据库 commit 失败时绝不发布通知，并返回通用脱敏 `500`。数据库 commit 成功但 Redis XADD 不可用时，不回滚或复制 Run；API 仍返回 `202`，Run 可能显示 `last_error="queue_notification_unavailable"`、`last_enqueued_at=null`，Worker 可仅靠数据库对账恢复。

通知使用 at-least-once 语义。消息重复、ACK 结果不确定或 Redis 数据丢失都不改变数据库事实；终态或已被有效 lease 执行的重复消息是 no-op。

错误：

```json
{
  "detail": {"code": "model_disabled", "message": "Model is disabled"}
}
```

- `404 model_not_found`：模型不存在。
- `404 benchmark_not_found`：Benchmark 不存在。
- `409 model_disabled`：模型已禁用。
- `429 run_backlog_full`：global scope 锁内观测到 managed `pending/running` 数已达到 active policy 的 `backlog_limit`；不会留下半写入 Run。
- `500 governance_integrity_error`：冻结 policy 或治理事实不一致，admission fail closed，并尽力追加固定、无秘密的完整性事件。
- `422`：生成参数、治理 override 或读取超时越界、ID 为空、布尔/浮点冒充治理整数，或出现额外字段。

Run 一旦提交，不会因随后 Redis 故障、并发/RPM/TPM 饱和而删除。瞬时治理压力让它回到 `pending/delayed` 并设置 `governance_not_before`；确定性 lifetime/pricing/上界失败会聚合已有 Response 后进入 `failed/exhausted`，不会伪造一条题级 0 分 Response。hard Token/TPM/费用启用时，`input_token_reservation` 和有限 `max_tokens` 是必须的显式上界；UTF-8 长度估计不能替代它，费用边界还要求 Model 快照存在 input/output USD 价格。Provider actual usage 始终保存；只有超过显式 input/output 预留或由完整显式上界和价格派生的 reserved cost 才会产生 `*_overdrawn`。Run Detail 对该类原因显示“实际用量曾被判定超过预留”；这一中性历史措辞也适用于升级前已经终止的 Run，不再把原因等同于 conservative settlement。

### 6.4 `GET /runs/{run_id}`

```bash
curl -sS http://127.0.0.1:8000/api/v1/runs/44444444-4444-4444-8444-444444444444
```

`200 OK` 返回 `RunRead`。运行期间持久化 Run 汇总字段可能尚未追上逐题证据；动态成绩、完成率、错误数、延迟与 usage/cost 已知覆盖应读取 6.7 节的 progress index。Run 的精确 `input_tokens`、`output_tokens` 与 `estimated_cost` 仍遵守 all-or-nothing nullable 语义，不会由 progress 已知小计回填。不存在返回：

```json
{
  "detail": {"code": "run_not_found", "message": "Evaluation run was not found"}
}
```

状态码为 `404 Not Found`。

### 6.5 `POST /runs/{run_id}/cancel`

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/runs/44444444-4444-4444-8444-444444444444/cancel
```

`200 OK` 返回最新 `RunRead`。行为如下：

- `pending`：立即转为 `cancelled`。
- `running`：原子写入 `cancellation_requested=true`；当前有效 Worker 在心跳/题目边界聚合已有证据并转为 `cancelled`。若 Worker 已死，数据库 reconciliation 在租约过期后收敛取消，不再领取执行。
- 已为 `completed`、`failed` 或 `cancelled`：幂等返回原记录。

不存在返回 `404 run_not_found`。取消不是硬中断，响应返回时不保证 Run 已进入终态；已发出且无法撤销的 Provider 请求可能继续至返回或超时，但失效 token 不能再写入 Response/费用/进度。

### 6.6 `GET /runs/{run_id}/responses`

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/runs/44444444-4444-4444-8444-444444444444/responses?offset=0&limit=100'
```

`200 OK`：

```json
{
  "items": [
    {
      "id": "55555555-5555-4555-8555-555555555555",
      "run_id": "44444444-4444-4444-8444-444444444444",
      "question_id": "33333333-3333-4333-8333-333333333333",
      "raw_response": "B",
      "parsed_answer": "B",
      "reference_answer_snapshot": "B",
      "score": 1.0,
      "evaluator_name": "multiple_choice_v1",
      "latency_ms": 1.0,
      "input_tokens": 8,
      "output_tokens": 2,
      "estimated_cost": 0.0,
      "provider_request_id": "provider-request-123",
      "returned_model": "provider/model-v1",
      "system_fingerprint": "fp_123",
      "finish_reason": "stop",
      "http_attempt_count": 1,
      "error_type": null,
      "error_message": null,
      "created_at": "2026-08-24T08:03:01Z",
      "question_external_id": "demo-choice-001",
      "question_type": "multiple_choice",
      "prompt": "Which option is the English translation of “月亮”? Reply with one option letter.",
      "choices": {"A": "river", "B": "moon", "C": "window", "D": "bread"}
    }
  ],
  "total": 15,
  "offset": 0,
  "limit": 100,
  "known_input_tokens": 120,
  "known_output_tokens": 30,
  "input_token_reported_responses": 15,
  "output_token_reported_responses": 15
}
```

四个 usage 汇总字段都针对该 Run 的**全部** Response，不受当前 `offset`/`limit` 影响：

- `known_input_tokens` / `known_output_tokens` 分别汇总对应列中所有非 `null` 值；没有已知值时返回 `0`。
- `input_token_reported_responses` / `output_token_reported_responses` 分别统计对应 Token 字段非 `null` 的 Response 数；合法的 `0` Token 仍计为已上报。两项计数彼此独立，相等不代表必然来自同一批 Response。
- 这些字段是可审计的已知小计与覆盖证据，不是精确 Provider 账单。`RunRead.input_tokens/output_tokens` 继续遵守 protocol-v1 的 all-or-nothing 语义：任一逐题 usage 缺失时，精确 Run Token 保持 `null`，不会由部分小计回填。

请求失败、空回答或解析失败的记录仍会出现，`score=0`，并填写 `error_type` 与
`error_message`；上游 usage 缺失时 Token 和费用为 `null`。非法 SSE UTF-8/JSON/字段映射为 `invalid_provider_stream`，200 SSE 内的上游 error 映射为 `provider_stream_error`，HTTP 干净结束却缺少所选协议的终止证据（Chat `[DONE]`、Responses `response.completed`、Messages `message_stop`）映射为 `incomplete_provider_stream`；已收到的部分 content 不会作为成功答案持久化。除既有 retryable HTTP/transport 分类外，Responses 的 rate-limit/server typed error，以及 Messages 的 `rate_limit_error`、`api_error`、`overloaded_error`、`timeout_error` 会按 Run 快照有限重试；Messages 快照的 HTTP retryable status 另含 `529`。未知流内错误 fail closed，每次重试独立进入 attempt ledger，因此仍可能重复上游计算或计费。若 Provider 返回 `finish_reason="length"`，空输出以及未能解析出有效最终答案的非空输出都会归类为 `output_truncated`，而不是泛化成 `empty_response` 或 `parse_error`；非空输出仍保存在 `raw_response`。成功内容若精确反射当前 Key，会在写入 `raw_response` 前替换为 `[REDACTED]`。

逐题 transport 证据只保存并返回固定字段：Provider request ID、返回模型名、system fingerprint、finish reason 与实际 HTTP attempt 数；不会保存或返回任意 raw usage 对象。四个字符串必须是短、无空白的安全 opaque token，任何超长、控制字符、凭据形态或脱敏占位值都会 fail closed 为 `null`。这些值用于关联和诊断，不改变评分，也不把本地 Response 幂等扩展为 Provider exactly-once。可信本地报告的 `responses.jsonl` 导出同一组固定字段，并再次执行报告级 secret scrub 与安全字符边界。

Web Run Detail 固定以 `limit=100` 请求一页逐题证据，并用 `offset` 提供上一页/下一页导航；它不会把大型正式 Benchmark 截止在前 100 条。当前页仍分别显示未得分与执行异常。全 Run 动态指标与热力图改用下一节的 progress index/block，不从当前证据页推断。精确 Run Token 与 Response 汇总来自同一可核对快照时显示精确值；否则显示“已知小计”、输入/输出各自覆盖率和“完整总量未知”。Run 不存在返回 `404 run_not_found`；分页非法返回 `422`。

### 6.7 `GET /runs/{run_id}/progress` 与 `/progress/blocks/{block_index}`

这两个只读接口为大型 Run Detail 提供轻量热力图事实，不使用通用 offset pagination，也没有 cursor。block 大小固定为 `512`，`block_index` 从 `0` 开始，absolute position 范围由 `block_index * block_size` 派生。

索引请求：

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/runs/44444444-4444-4444-8444-444444444444/progress'
```

`200 OK`：

```json
{
  "block_size": 512,
  "total_questions": 5,
  "completed_questions": 3,
  "correct_questions": 1,
  "error_questions": 1,
  "score": 20.0,
  "completion_rate": 40.0,
  "answered_accuracy": 50.0,
  "average_latency_ms": 200.0,
  "known_input_tokens": 30,
  "known_output_tokens": 15,
  "input_token_reported_responses": 2,
  "output_token_reported_responses": 2,
  "known_estimated_cost": 0.001,
  "estimated_cost_reported_responses": 1,
  "blocks": [
    {"block_index": 0, "response_count": 3}
  ]
}
```

示例为字段结构演示；实际 `blocks` 必须覆盖该 Run 的全部计划 block，并按 `block_index` 升序返回，空 block 也保留 `response_count=0`。`completed_questions`、正确/异常数、三项成绩、平均延迟、known usage/cost 及其 reported coverage 与这些 block counts 从同一数据库读取快照派生，运行中每次请求都可变化。指标公式与 `llmbenchlab-protocol-v1` 相同；前端不得从尚未完全同步的 cells 子集另算主指标。

读取一个 block：

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/runs/44444444-4444-4444-8444-444444444444/progress/blocks/0'
```

`200 OK`：

```json
{
  "block_index": 0,
  "items": [
    {
      "position": 0,
      "outcome": "wrong",
      "score": 0.0,
      "latency_ms": 200.0,
      "input_tokens": 20,
      "output_tokens": 10,
      "estimated_cost": null,
      "error_type": null
    },
    {
      "position": 2,
      "outcome": "passed",
      "score": 1.0,
      "latency_ms": 100.0,
      "input_tokens": 10,
      "output_tokens": 5,
      "estimated_cost": 0.001,
      "error_type": null
    },
    {
      "position": 3,
      "outcome": "error",
      "score": 0.0,
      "latency_ms": 300.0,
      "input_tokens": null,
      "output_tokens": null,
      "estimated_cost": null,
      "error_type": "provider_error"
    }
  ]
}
```

`items` 只含该 block 已持久化的 Response，并按 absolute `position` 升序；没有返回的计划 position 隐式为 `not_run`。`outcome` 的互斥判定优先级固定为：`error_type != null` 时 `error`；否则 `score == 1` 时 `passed`；其余为 `wrong`。这保证执行异常不会同时被展示为普通答错。

cell 是严格白名单，只能包含 `position`、`outcome`、`score`、`latency_ms`、`input_tokens`、`output_tokens`、`estimated_cost`、`error_type`。接口不返回 Question/Response ID、external ID、prompt、choices、raw/parsed/reference answer、error message、Provider request/model/fingerprint/finish reason、任意 raw usage 或其他 Provider metadata。两个响应都带 `Cache-Control: no-store`。

客户端每秒读取小型 index，只拉取 `response_count` 非零且尚未同步、或 count 相对本地发生变化的 block。全部目标 block hydrate 完成前应显示“同步中”，不能把尚未加载的格子伪装成 `not_run`。Response 是 append-only 唯一事实；若新提交发生在 index 与 block 请求之间，block 可能已比旧 index 更新，客户端保留较新 items 并由下一次 index 收敛。Run 进入终态不代表最后一个 block 请求已经返回，客户端应在目标 counts 追齐后再停止 progress 轮询。

Run 不存在返回 `404 run_not_found`。负 `block_index` 由路径参数校验返回 `422`；`block_index >= block_count` 返回 typed `422 progress_block_out_of_range`。若已持久化 Response 的 Question/position 不属于 Run 冻结计划，index 整次 fail closed 为 `500 run_progress_integrity_error`，不返回部分指标或格子。索引响应不重复 Run `status`，客户端继续从 `GET /runs/{run_id}` 读取状态。

`known_input_tokens`、`known_output_tokens` 与 `known_estimated_cost` 始终只是非 `null` 证据的小计，三个 reported-response 计数分别给出覆盖范围；合法的零值仍算已上报。它们不改变 Run 精确字段：任一必要 usage 或价格缺失时，`RunRead.input_tokens/output_tokens/estimated_cost` 仍可为 `null`，不得把 known subtotal 冒充完整账单。

### 6.8 `GET /runs/{run_id}/audit`

返回该 Run 已保留的类型化审计事件。排序固定为 `(occurred_at, id)` 升序；`offset`/`limit` 使用通用 `0..` / `1..100` 分页规则。事件表由执行状态转换事务追加，分页读取不会修改事件、Run 或治理账本。

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/runs/44444444-4444-4444-8444-444444444444/audit?offset=0&limit=100'
```

```json
{
  "items": [
    {
      "id": "66666666-6666-4666-8666-666666666666",
      "event_type": "run_claimed",
      "payload": {"dispatch_count": 1},
      "retention_class": "operational",
      "occurred_at": "2026-08-27T08:00:00Z",
      "expires_at": "2026-11-25T08:00:00Z",
      "correlation_id": "44444444-4444-4444-8444-444444444444",
      "run_id": "44444444-4444-4444-8444-444444444444",
      "model_id": "11111111-1111-4111-8111-111111111111",
      "question_id": null,
      "worker_id": "worker-1",
      "reservation_id": null,
      "attempt": 1,
      "provider_attempt": null,
      "lease_token": 1,
      "duration_ms": null
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 100
}
```

`payload` 由 `event_type` 的固定字段白名单约束，只能包含短枚举、数值、Hash 或 UTC 时间；Prompt、raw response、URL、异常文本、凭据、密文、nonce 与 raw Provider usage 没有可表示字段。接口也不暴露内部幂等 `event_key` 或 `payload_hash`。普通操作事件至少保留 90 天，credential/security 事件至少 365 天；只有显式归档/维护流程才会清理，因此 `total` 是查询时仍在数据库中的保留事件数。这里的 append-only 是应用行为约束，不是 WORM、密码学完整性证明或对数据库管理员的防篡改承诺。读取时会重新校验 event contract、payload hash、identity、数值边界与保留期；损坏行只返回稳定 `500 audit_event_integrity_error`，不会反射损坏 payload。Run 不存在返回 `404 run_not_found`。

## 7. Leaderboard 与 Metrics

### 7.1 `GET /leaderboard`

只包含 `completed` Run，并默认限定当前 `llmbenchlab-protocol-v1`。筛选参数：

| 参数 | 默认值 | 可选值/说明 |
| --- | --- | --- |
| `model_id` | 无 | 精确 ID |
| `benchmark_id` | 无 | 精确 ID；Web 排名视图必须提供，以形成单一 version/hash 分区 |
| `protocol_version` | 当前 v1 | 精确协议字符串 |
| `order` | `score_desc` | `score_desc`、`score_asc`、`latency_asc`、`newest` |
| `offset`, `limit` | `0`, `20` | 通用分页 |

```bash
curl -sS 'http://127.0.0.1:8000/api/v1/leaderboard?benchmark_id=22222222-2222-4222-8222-222222222222&order=score_desc'
```

`200 OK`：

```json
{
  "items": [
    {
      "run_id": "44444444-4444-4444-8444-444444444444",
      "model_id": "11111111-1111-4111-8111-111111111111",
      "model_name": "Offline Mock",
      "benchmark_id": "22222222-2222-4222-8222-222222222222",
      "benchmark_slug": "demo-general",
      "benchmark_name": "Demo General / 通用演示集",
      "benchmark_version": "1.0.0",
      "benchmark_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
      "is_demo": true,
      "protocol_version": "llmbenchlab-protocol-v1",
      "score": 100.0,
      "answered_accuracy": 100.0,
      "completion_rate": 100.0,
      "average_latency_ms": 1.0,
      "input_tokens": 120,
      "output_tokens": 30,
      "estimated_cost": 0.0,
      "started_at": "2026-08-24T08:03:01Z",
      "finished_at": "2026-08-24T08:03:02Z"
    }
  ],
  "total": 1,
  "offset": 0,
  "limit": 20
}
```

`order` 或分页非法返回 `422`。API 默认按协议隔离；未提供 `benchmark_id` 时返回的是跨 Benchmark 分区的结果集合，而不是可解释为统一名次的榜单。排名 UI 或其他调用方必须提供具体 Benchmark ID，并核对 `benchmark_version + benchmark_hash`；Demo 成绩不得解释为正式能力。

### 7.2 `GET /metrics/summary`

```bash
curl -sS http://127.0.0.1:8000/api/v1/metrics/summary
```

`200 OK`：

```json
{
  "model_count": 1,
  "benchmark_count": 1,
  "run_count": 1,
  "completed_run_count": 1,
  "failed_run_count": 0,
  "average_score": 100.0,
  "average_latency_ms": 1.0,
  "total_input_tokens": 120,
  "total_output_tokens": 30,
  "total_estimated_cost": 0.0,
  "recent_runs": [
    {
      "run_id": "44444444-4444-4444-8444-444444444444",
      "model_id": "11111111-1111-4111-8111-111111111111",
      "model_name": "Offline Mock",
      "benchmark_id": "22222222-2222-4222-8222-222222222222",
      "benchmark_slug": "demo-general",
      "benchmark_name": "Demo General / 通用演示集",
      "benchmark_version": "1.0.0",
      "benchmark_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe",
      "is_demo": true,
      "protocol_version": "llmbenchlab-protocol-v1",
      "score": 100.0,
      "answered_accuracy": 100.0,
      "completion_rate": 100.0,
      "average_latency_ms": 1.0,
      "input_tokens": 120,
      "output_tokens": 30,
      "estimated_cost": 0.0,
      "started_at": "2026-08-24T08:03:01Z",
      "finished_at": "2026-08-24T08:03:02Z"
    }
  ]
}
```

没有当前协议的已完成 Run 时，平均分和平均延迟为 `null`，Token/费用总计为 `0`，`recent_runs=[]`。`run_count` 统计全部 Run；成功/失败计数、平均值、Token、费用和最近记录只聚合当前 `llmbenchlab-protocol-v1`，避免跨协议混合。若任一已完成 Run 的某类 Token 或成本为未知，对应总计返回 `null`，不得把部分和伪装为完整总量。跨 Benchmark 的 Dashboard 平均只用于概览，不能作为直接可比结论。

可信本地 CLI 的报告导出不是 REST 端点。报告中的唯一主指标从不可变计划题数和实际导出的 Responses 重新派生，保证 `summary.json`、`groups.csv` 与 `responses.jsonl` 同口径；`summary.metrics_provenance` 标明数据库 Run 汇总字段是否一致并列出漂移字段名。`responses.jsonl` 可包含上述五个固定 transport 证据字段，但不包含 raw usage 或治理 scope material。新 Run 会在快照中固化初次模型发现/canary 的脱敏证据，但 `resume` 期间重新执行的 canary 当前不会追加为独立审计事件。

## 8. CORS 与客户端轮询

- CORS 只允许配置中的显式前端 Origin，拒绝通配符；默认开发 Origin 为
  `http://localhost:5173` 和 `http://127.0.0.1:5173`。
- 允许的方法为 `GET`、`POST`、`PUT`、`PATCH`、`DELETE`、`OPTIONS`，允许的请求头为
  `Accept`、`Content-Type`，并向浏览器暴露响应 `X-Request-ID`；不启用跨域凭据。客户端不应发送 `X-Request-ID`：API 忽略该输入并总是自行生成新 UUID。
- Run 创建后建议每 0.5–2 秒轮询一次，见到终态即停止；MVP 没有 WebSocket。
- API 重启不拥有也不改写 Run；Worker 重启或异常退出后，未完成 Run 在租约过期后被接管，已持久 Response 保留且不重复写入。客户端应继续轮询同一 Run ID，并可使用 `attempt_count`、`last_error`、`dead_lettered_at` 和终态 `error_message` 展示恢复轨迹。
