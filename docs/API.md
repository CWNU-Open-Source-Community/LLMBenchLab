# LLMBenchLab REST API

本文档描述当前 MVP 的实际 HTTP 接口。API 前缀为 `/api/v1`，本地默认地址为
`http://127.0.0.1:8000/api/v1`。交互式 OpenAPI 文档位于 `/docs`，ReDoc 位于
`/redoc`，原始规范位于 `/openapi.json`。

> MVP 没有身份认证或权限控制，只适合受信任的本机环境。不要把服务直接暴露到公网。

## 1. 通用约定

- 除 Benchmark ZIP 上传外，请求与响应均使用 `application/json`。
- 所有持久化时间以 UTC 产生；示例使用 ISO 8601 的 `Z` 形式。客户端显示时应明确时区。
- 资源 ID 是 36 字符 UUID 字符串。下面 UUID、模型名和地址为示例值；`demo-general` 的内容与 Dataset Hash 使用仓库内置数据的真实 canonical 值。
- Model 只保存并返回 `api_key_env`（环境变量名称），不会返回该变量对应的值。
- `score`、`completion_rate` 和 `answered_accuracy` 的单位均为百分比 `0..100`；逐题
  `score` 为 `0..1`。
- Token usage 或费用无法从上游取得时为 `null`，不能解释为零。
- 当前没有自定义请求 ID、幂等键或速率限制。

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

应用主动从 422 响应中省略 Pydantic 的 `input` 与 `ctx`，避免反射可能敏感的原始输入。调用方仍不得把真实密钥作为未知字段发送；正确做法始终是只发送环境变量名称。

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

未捕获的服务端异常可能返回 `500`。调用方不应依赖英文 `message` 做分支，应优先使用稳定的业务 `code` 与 HTTP 状态码。

## 2. 接口总览

| 方法 | 路径 | 成功状态 | 说明 |
| --- | --- | ---: | --- |
| GET | `/health` | 200 | 本地 API/数据库健康检查 |
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
| POST | `/runs` | 202 | 创建并在进程内后台启动 Run |
| GET | `/runs/{run_id}` | 200 | 轮询 Run 状态与汇总 |
| POST | `/runs/{run_id}/cancel` | 200 | 请求协作式取消 |
| GET | `/runs/{run_id}/responses` | 200 | 分页读取逐题证据 |
| GET | `/leaderboard` | 200 | 已完成 Run 的严格总分榜 |
| GET | `/metrics/summary` | 200 | Dashboard 汇总 |

## 3. 系统接口

### 3.1 `GET /health`

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

### 3.2 `GET /info`

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
    "runner": "in_process_mvp"
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
| `base_url` | string/null | 绝对 HTTP(S) URL；禁止 URL 内嵌账号密码、query 与 fragment |
| `remote_model_name` | string/null | 最长 256 |
| `api_key_env` | string/null | 环境变量名格式，不是密钥值 |
| `enabled` | boolean，默认 `true` | 禁用模型不能创建 Run |
| `input_price_per_million` | number/null，默认 `null` | 有限非负数；Mock 未填时规范化为明确的 `0` |
| `output_price_per_million` | number/null，默认 `null` | 有限非负数；Mock 未填时规范化为明确的 `0` |
| `default_parameters` | object，默认 `{}` | 只允许 `temperature`、`top_p`、`max_tokens`、`seed`，并使用与 Run 相同的类型/范围约束 |

`openai_compatible` 必须同时提供 `base_url`、`remote_model_name` 和 `api_key_env`；`mock` 的这三个远端连接字段必须为空。API 只验证 URL 语法，**不会阻止内网或云元数据地址**，详见 [SECURITY.md](SECURITY.md)。

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

注册 OpenAI-compatible 配置。这里的 `LOCAL_COMPAT_API_KEY` 只是环境变量名称：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/models \
  -H 'Content-Type: application/json' \
  -d '{
    "name":"Local Compatible",
    "provider_type":"openai_compatible",
    "base_url":"https://llm-gateway.invalid/v1",
    "remote_model_name":"example-chat-model",
    "api_key_env":"LOCAL_COMPAT_API_KEY",
    "enabled":true,
    "input_price_per_million":null,
    "output_price_per_million":null,
    "default_parameters":{}
  }'
```

`201 Created` 返回 `ModelRead`。错误：

- `409 model_name_conflict`：名称已存在。
- `422`：字段缺失、额外字段、非法 URL/环境变量名，或 Provider 必需字段不完整。

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

`200 OK` 返回更新后的 `ModelRead`。`404 model_not_found`、`409 model_name_conflict` 和
`422` 的含义与创建接口一致。

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

- `413 archive_too_large`：上传体超过 18 MiB。
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
    "model": {"id": "11111111-1111-4111-8111-111111111111", "name": "Offline Mock", "remote_model_name": null, "adapter_type": "mock", "base_url": null, "api_key_env": null, "input_price_per_million": "0", "output_price_per_million": "0", "currency_assumption": "USD", "default_parameters": {}},
    "benchmark": {"id": "22222222-2222-4222-8222-222222222222", "slug": "demo-general", "name": "Demo General / 通用演示集", "version": "1.0.0", "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe", "question_count": 15, "is_demo": true},
    "evaluator": {"name": "builtin-objective", "version": "1.0", "mapping": {"exact_match": "exact_match_v1", "multiple_choice": "multiple_choice_v1", "numeric": "numeric_v1"}},
    "execution": {"concurrency": 1, "timeouts_seconds": {"connect": 5.0, "read": 60.0, "write": 30.0, "pool": 5.0}, "retry_policy": {"name": "bounded_exponential_backoff", "max_retries": 2, "max_attempts": 3, "backoff_base_seconds": 0.25, "backoff_cap_seconds": 2.0, "retryable_status_codes": [408, 429, 500, 502, 503, 504]}, "restart_recovery": "mark_failed_without_resume"}
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
  "started_at": "2026-08-24T08:03:01Z",
  "finished_at": "2026-08-24T08:03:02Z",
  "created_at": "2026-08-24T08:03:00Z",
  "error_message": null
}
```

状态集合：`pending`、`running`、`completed`、`failed`、`cancelled`。

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
        "model": {"id": "11111111-1111-4111-8111-111111111111", "name": "Offline Mock", "remote_model_name": null, "adapter_type": "mock", "base_url": null, "api_key_env": null, "input_price_per_million": "0", "output_price_per_million": "0", "currency_assumption": "USD", "default_parameters": {}},
        "benchmark": {"id": "22222222-2222-4222-8222-222222222222", "slug": "demo-general", "name": "Demo General / 通用演示集", "version": "1.0.0", "dataset_hash": "5c51bb4fa42fc6aa2e8b0b95bb7e37ef8bdff8b6fa4eecfb66da5d4faf755afe", "question_count": 15, "is_demo": true},
        "evaluator": {"name": "builtin-objective", "version": "1.0", "mapping": {"exact_match": "exact_match_v1", "multiple_choice": "multiple_choice_v1", "numeric": "numeric_v1"}},
        "execution": {"concurrency": 1, "timeouts_seconds": {"connect": 5.0, "read": 60.0, "write": 30.0, "pool": 5.0}, "retry_policy": {"name": "bounded_exponential_backoff", "max_retries": 2, "max_attempts": 3, "backoff_base_seconds": 0.25, "backoff_cap_seconds": 2.0, "retryable_status_codes": [408, 429, 500, 502, 503, 504]}, "restart_recovery": "mark_failed_without_resume"}
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

创建后先持久化，再立即返回 Run；执行由当前 API 进程内的后台任务完成。

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

`202 Accepted` 返回 `RunRead`，通常 `status="pending"`。客户端应轮询 `GET /runs/{id}`，
不能把 `202` 当作评测已完成。调度被拒绝时同一响应中的状态可能已是 `failed`，
`error_message="task_schedule_rejected"`。

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
- `running`：写入 `cancellation_requested=true`；正在执行的单题可能结束，随后转为 `cancelled`。
- 已为 `completed`、`failed` 或 `cancelled`：幂等返回原记录。

不存在返回 `404 run_not_found`。取消不是硬中断，响应返回时不保证 Run 已进入终态。

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
`error_message`；上游 usage 缺失时 Token 和费用为 `null`。Run 不存在返回
`404 run_not_found`；分页非法返回 `422`。

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

## 8. CORS 与客户端轮询

- CORS 只允许配置中的显式前端 Origin，拒绝通配符；默认开发 Origin 为
  `http://localhost:5173` 和 `http://127.0.0.1:5173`。
- 允许的方法为 `GET`、`POST`、`PATCH`、`DELETE`、`OPTIONS`，允许的请求头仅
  `Accept`、`Content-Type`，不启用跨域凭据。
- Run 创建后建议每 0.5–2 秒轮询一次，见到终态即停止；MVP 没有 WebSocket。
- 进程重启后，遗留 `running` Run 会被标为 `failed`，不会自动续跑。客户端应显示
  `error_message`，由用户显式创建新 Run。
