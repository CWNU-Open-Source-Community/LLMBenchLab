# LLMBenchLab REST API

本文档描述当前 MVP 的实际 HTTP 接口。API 前缀为 `/api/v1`，本地默认地址为
`http://127.0.0.1:8000/api/v1`。交互式 OpenAPI 文档位于 `/docs`，ReDoc 位于
`/redoc`，原始规范位于 `/openapi.json`。

Phase 2 可靠执行基础保持了 `/api/v1` 和 `llmbenchlab-protocol-v1` 评分含义，但将任务执行从 API 进程移到了数据库租约驱动的独立 Worker。Phase 2 仍为 `in_progress`；这些接口不代表已具备公网、HA、完整限流/预算/背压、历史可观测或审计能力。

> MVP 没有身份认证或权限控制，只适合受信任的本机环境。不要把服务直接暴露到公网。

## 1. 通用约定

- 除 Benchmark ZIP 上传外，请求与响应均使用 `application/json`。
- 所有持久化时间以 UTC 产生；示例使用 ISO 8601 的 `Z` 形式。客户端显示时应明确时区。
- 资源 ID 是 36 字符 UUID 字符串。下面 UUID、模型名和地址为示例值；`demo-general` 的内容与 Dataset Hash 使用仓库内置数据的真实 canonical 值。
- Model 写接口接受 Web 使用的 write-only `api_key`，也保留 `api_key_env` 环境变量兼容模式；Model 读响应只公开 `credential_source`、`has_api_key` 和兼容模式的变量名称，绝不返回 Key、密文、nonce、加密 key id 或部署 keyring 内容。
- `score`、`completion_rate` 和 `answered_accuracy` 的单位均为百分比 `0..100`；逐题
  `score` 为 `0..1`。
- Token usage 或费用无法从上游取得时为 `null`，不能解释为零。
- 服务为每个请求始终生成全新的 server-side UUID，并在所有响应（包括通用 500）的 `X-Request-ID` header 中回传。客户端传入的同名 header 会被忽略，且 CORS 不允许它作为请求 header；浏览器仍可读取响应中暴露的 `X-Request-ID`。它用于诊断关联，不是请求幂等键。当前仍没有速率限制。

### 1.1 分页

以下列表接口采用 offset pagination：

- `GET /models`
- `GET /benchmarks`
- `GET /benchmarks/{benchmark_id}/questions`
- `GET /runs`
- `GET /runs/{run_id}/responses`
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
| GET | `/info` | 200 | 服务、协议和能力信息 |
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
  "total_attempts": 3,
  "timestamp": "2026-08-25T06:00:00Z"
}
```

- `pending`：所有 pending Run；`due_pending` 仅包含 `next_attempt_at` 为空或已到期的部分。
- `running` 与 `expired_running`：当前 running 以及按数据库时间已过租约的子集。
- `active_cancellation_requests`：尚在 pending/running 且已请求取消的 Run。
- `retry_scheduled`、`dead_lettered`：已安排后续 attempt 和已进入权威 dead-letter 终态的 Run。
- `runs_with_queue_notification_error`：`last_error=queue_notification_unavailable` 的当前 Run 数。
- `total_attempts`：当前 Run 表中 `attempt_count` 的总和。

这些都是查询时点的 DB-derived gauges，不是完整事件 counters、历史延迟、审计记录或监控面板；它们绝不能覆盖数据库任务状态。

### 3.5 `GET /info`

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
    "providers": ["mock", "openai_compatible"],
    "question_types": ["exact_match", "multiple_choice", "numeric"],
    "runner": "independent_database_lease_worker"
  }
}
```

## 4. Model

### 4.1 Model Schema

创建字段：

| 字段 | 类型/默认值 | 规则 |
| --- | --- | --- |
| `name` | string，必填 | 去首尾空白后 `1..160`，全库唯一 |
| `provider_type` | `mock` 或 `openai_compatible`，必填 | 首期封闭集合 |
| `base_url` | string/null | 绝对 URL；远端只允许 HTTPS，明文 HTTP 仅允许 loopback；禁止 URL 内嵌账号密码、query 与 fragment |
| `remote_model_name` | string/null | 最长 256 |
| `api_key` | string/null | **仅写入**；8–8192 bytes、无首尾空白、只含可见 ASCII；OpenAPI 标记 `writeOnly`，所有响应均省略 |
| `api_key_env` | string/null | 兼容 CLI/旧客户端的环境变量名，不是密钥值；不能与 `api_key` 同时提供 |
| `enabled` | boolean，默认 `true` | 禁用模型不能创建 Run |
| `input_price_per_million` | number/null，默认 `null` | 有限非负数；Mock 未填时规范化为明确的 `0` |
| `output_price_per_million` | number/null，默认 `null` | 有限非负数；Mock 未填时规范化为明确的 `0` |
| `default_parameters` | object，默认 `{}` | 只允许 `temperature`、`top_p`、`max_tokens`、`seed`，并使用与 Run 相同的类型/范围约束 |

`openai_compatible` 必须同时提供 `base_url`、`remote_model_name`，并在 `api_key` 与 `api_key_env` 中恰好选择一个；`mock` 的四个远端连接/凭据字段必须为空。Model Schema、Provider preflight 和 Adapter 都拒绝远端明文 HTTP，只有 `localhost` 或字面量 loopback IP 可使用 HTTP；HTTPS 私网、云元数据、DNS rebinding 和其他出站目标仍没有 allowlist，详见 [SECURITY.md](SECURITY.md)。

读响应额外包含两个派生字段：`credential_source` 为 `none | environment | stored`；`has_api_key` 只表示该 Model 当前拥有应用加密保存的 Web Key。环境变量模式即使 Worker 环境中已有值也仍返回 `has_api_key=false`。`stored` 模式在独立 `model_credentials` 行中以 `model_id` 为主键保存 AES-GCM envelope，Model/Run/Response Schema 均不映射其内部列。

本 API 的 `GET /models` 是 LLMBenchLab 本地模型注册表，不会代替操作者访问 Provider。可信本地 `llmbenchlab-evaluate` 才会调用上游 `/models` 与付费 canary：发现到的任一模型 ID 若包含当前 Key，预检立即失败；canary 成功体若明确返回不同于请求目标的模型名，也会失败。模型发现与正式 Chat 请求声明 `Accept-Encoding: identity` 并拒绝其他响应编码；发现体上限为 2 MiB，Chat 成功体上限为 4 MiB、错误体上限为 64 KiB。成功内容、raw usage 的对象键/所有 JSON 标量、token/status 数值、request ID、返回模型名、system fingerprint 与 finish reason 中出现的当前 Key 会在进入持久化边界前按精确值替换为 `[REDACTED]`。

Phase 1 的 Model 默认参数只覆盖上述四个实际由 Adapter 转发的生成字段。创建 Run 时，显式请求值优先；某字段未出现在请求 JSON 中时才使用 Model 默认值，否则使用协议默认值。Run 的 `generation` 快照保存最终有效值。

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

筛选参数：`provider_type=mock|openai_compatible`、`enabled=true|false`，并支持通用分页。

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

注册供 Web 使用的 OpenAI-compatible 配置。推荐在 Models 页面粘贴 Key；`api_key` 只出现在这次写请求中，成功响应不会返回它，数据库也不会保存其明文。下面只是请求结构，不是可直接填入真实 Key 的 shell 命令：

```json
{
  "name": "Local Compatible",
  "provider_type": "openai_compatible",
  "base_url": "https://llm-gateway.invalid/v1",
  "remote_model_name": "example-chat-model",
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
| `temperature` | `0.0` | `0..2` |
| `top_p` | `1.0` | `>0` 且 `<=1` |
| `max_tokens` | `256` | `1..32768` |
| `seed` | `42` | 32 位有符号整数或 `null` |
| `system_prompt` | `null` | 最长 4000；提供时覆盖 Benchmark system prompt |
| `concurrency` | `1` | `1..4`；快照值即实际执行并发度 |

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
    "execution": {"concurrency": 1, "timeouts_seconds": {"connect": 5.0, "read": 60.0, "write": 30.0, "pool": 5.0}, "retry_policy": {"name": "bounded_exponential_backoff", "max_retries": 2, "max_attempts": 3, "backoff_base_seconds": 0.25, "backoff_cap_seconds": 2.0, "retryable_status_codes": [408, 429, 500, 502, 503, 504]}, "task_delivery": "at_least_once", "task_max_attempts": 3, "restart_recovery": "database_lease_resume_missing_responses"}
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
| `lease_owner` | 当前 Worker ID；只在 `running` 且租约活动时非空 |
| `lease_token` | 单调递增的 fencing generation；租约释放后保留最后值，防止旧 Worker 写入 |
| `lease_expires_at` / `heartbeat_at` | 由数据库时间裁决的租约截止点和最近心跳；非 running 时为 `null` |
| `next_attempt_at` | 可重试失败后的最早再领取时间；只用于 pending |
| `last_enqueued_at` | 最近一次 Redis 通知成功的数据库时间；为空不代表 Run 不可恢复 |
| `last_error` | 最近一次执行/通知层的稳定脱敏错误码；与终态展示的 `error_message` 不同 |
| `dead_lettered_at` | attempt 耗尽且 Response 集不完整时的权威 dead-letter 时间；只用于 failed |

`model_parameters_snapshot.execution.retry_policy` 是每题 Adapter 的有限重试；`task_delivery`、`task_max_attempts` 和 `restart_recovery` 是 Run/Worker 恢复语义。新 Run 固定为 `at_least_once` 和 `database_lease_resume_missing_responses`，恢复时跳过已有 Response。Runner 在取得租约并启动心跳后，通过工作线程加载/物化数据库快照，避免大型 Benchmark 的同步加载阻塞事件循环而饿死心跳。完成、取消以及 attempt 耗尽进入 dead-letter 前都会从已持久化 Response 聚合 Run 字段。这不保证 Provider 调用或计费 exactly-once；若进程在 Provider 响应后、本地提交前崩溃，可能再次调用 Provider，但数据库仍只保留一条计分/费用 Response 证据。

### 6.2 `GET /runs`

筛选参数：`model_id`、`benchmark_id`、`run_status`、`protocol_version`。注意参数名是
`run_status`，不是 `status`。

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
        "execution": {"concurrency": 1, "timeouts_seconds": {"connect": 5.0, "read": 60.0, "write": 30.0, "pool": 5.0}, "retry_policy": {"name": "bounded_exponential_backoff", "max_retries": 2, "max_attempts": 3, "backoff_base_seconds": 0.25, "backoff_cap_seconds": 2.0, "retryable_status_codes": [408, 429, 500, 502, 503, 504]}, "task_delivery": "at_least_once", "task_max_attempts": 3, "restart_recovery": "database_lease_resume_missing_responses"}
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

创建顺序固定为：先用与 endpoint/credential 更新相同的方言锁读取并校验 Model，再写入 `pending` Run 及完整快照，提交后 best-effort 发布 Redis Streams 通知，然后返回 Run。PostgreSQL 使用 Model row `FOR UPDATE`；SQLite 在首次读取前使用 `BEGIN IMMEDIATE` 串行化写事务。API 不解密 stored credential、不加载 Adapter、不执行题目；独立 Worker 从 Redis 通知或数据库 reconciliation 获取工作。

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
    "concurrency":1
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
- `422`：生成参数越界、ID 为空或出现额外字段。

### 6.4 `GET /runs/{run_id}`

```bash
curl -sS http://127.0.0.1:8000/api/v1/runs/44444444-4444-4444-8444-444444444444
```

`200 OK` 返回 `RunRead`。运行期间汇总字段可能为 `null`，进度由
`completed_questions / total_questions` 表示。不存在返回：

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
  "limit": 100
}
```

请求失败、空回答或解析失败的记录仍会出现，`score=0`，并填写 `error_type` 与
`error_message`；上游 usage 缺失时 Token 和费用为 `null`。成功内容若精确反射当前 Key，会在写入 `raw_response` 前替换为 `[REDACTED]`。当前 EvaluationResponse/API Schema 不保存或返回逐题 Provider request ID、返回模型名、system fingerprint 或 raw usage；这些 transport 证据仍是 P2-06 审计缺口。Run 不存在返回 `404 run_not_found`；分页非法返回 `422`。

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

可信本地 CLI 的报告导出不是 REST 端点。报告中的唯一主指标从不可变计划题数和实际导出的 Responses 重新派生，保证 `summary.json`、`groups.csv` 与 `responses.jsonl` 同口径；`summary.metrics_provenance` 标明数据库 Run 汇总字段是否一致并列出漂移字段名。新 Run 会在快照中固化初次模型发现/canary 的脱敏证据，但 `resume` 期间重新执行的 canary 当前不会追加为独立审计事件。

## 8. CORS 与客户端轮询

- CORS 只允许配置中的显式前端 Origin，拒绝通配符；默认开发 Origin 为
  `http://localhost:5173` 和 `http://127.0.0.1:5173`。
- 允许的方法为 `GET`、`POST`、`PATCH`、`DELETE`、`OPTIONS`，允许的请求头为
  `Accept`、`Content-Type`，并向浏览器暴露响应 `X-Request-ID`；不启用跨域凭据。客户端不应发送 `X-Request-ID`：API 忽略该输入并总是自行生成新 UUID。
- Run 创建后建议每 0.5–2 秒轮询一次，见到终态即停止；MVP 没有 WebSocket。
- API 重启不拥有也不改写 Run；Worker 重启或异常退出后，未完成 Run 在租约过期后被接管，已持久 Response 保留且不重复写入。客户端应继续轮询同一 Run ID，并可使用 `attempt_count`、`last_error`、`dead_lettered_at` 和终态 `error_message` 展示恢复轨迹。
