# Benchmark 数据集格式

LLMBenchLab 的 Benchmark 是一个不可变、可版本化的目录。MVP 只读取两个固定文件，不下载 manifest 指向的资源，也不执行其中的代码：

```text
my-benchmark/
├── manifest.json
└── questions.jsonl
```

两个文件必须是 UTF-8（允许 UTF-8 BOM 但导入时移除），JSON 字符串必须是有效 Unicode，换行可为 LF 或 CRLF。`questions.jsonl` 每个非空行必须恰好包含一个完整 JSON object；MVP 不允许注释、尾随逗号、跨行 object 或空行。

当前格式版本为：

```text
llmbenchlab-dataset-v1
```

## manifest.json 完整 Schema

以下是 JSON Schema Draft 2020-12。源文件不允许额外字段；Importer 生成的数据库 ID、Hash、导入时间和绝对路径不属于 manifest。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://llmbenchlab.local/schemas/manifest-v1.json",
  "title": "LLMBenchLab Benchmark Manifest v1",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version",
    "id",
    "name",
    "version",
    "description",
    "dimension",
    "language",
    "license",
    "source",
    "evaluator",
    "prompt_template",
    "question_count"
  ],
  "properties": {
    "schema_version": {
      "const": "llmbenchlab-dataset-v1"
    },
    "id": {
      "type": "string",
      "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
      "minLength": 1,
      "maxLength": 80
    },
    "name": {
      "type": "string",
      "minLength": 1,
      "maxLength": 160
    },
    "version": {
      "type": "string",
      "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(?:-[0-9A-Za-z.-]+)?$",
      "maxLength": 64
    },
    "description": {
      "type": "string",
      "minLength": 1,
      "maxLength": 4000
    },
    "dimension": {
      "type": "string",
      "pattern": "^[a-z][a-z0-9_-]*$",
      "minLength": 1,
      "maxLength": 64
    },
    "language": {
      "type": "string",
      "pattern": "^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
      "maxLength": 35
    },
    "license": {
      "type": "string",
      "minLength": 1,
      "maxLength": 128
    },
    "source": {
      "type": "string",
      "minLength": 1,
      "maxLength": 2048
    },
    "evaluator": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "version", "mapping"],
      "properties": {
        "name": {
          "const": "builtin-objective"
        },
        "version": {
          "const": "1.0"
        },
        "mapping": {
          "type": "object",
          "additionalProperties": false,
          "required": ["exact_match", "multiple_choice", "numeric"],
          "properties": {
            "exact_match": { "const": "exact_match_v1" },
            "multiple_choice": { "const": "multiple_choice_v1" },
            "numeric": { "const": "numeric_v1" }
          }
        }
      }
    },
    "prompt_template": {
      "type": "object",
      "additionalProperties": false,
      "required": ["system", "user"],
      "properties": {
        "system": {
          "type": "string",
          "maxLength": 4000
        },
        "user": {
          "type": "string",
          "minLength": 1,
          "maxLength": 12000
        }
      }
    },
    "question_count": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10000
    }
  }
}
```

补充语义约束：

- `id` 是跨发布版本稳定的 Benchmark 标识；目录名建议与它相同。
- `version` 使用 SemVer 形式。它表示数据内容版本，不是 Schema 版本。
- `dimension` 是机器可读能力维度，如 `general`、`math`、`reasoning`。
- `language` 使用 BCP 47 风格标签，如 `zh-CN`、`en`；混合语言使用项目约定的 `mul`。
- `prompt_template.user` 必须且只能把 `{prompt}`、可选的 `{choices}` 作为受支持占位符。未知占位符、未闭合花括号和模板表达式均拒绝。
- `source` 仅是来源说明或 HTTPS URL；Importer 不访问该地址，也不接受它作为本地路径。
- v1 的 evaluator 名称、版本和映射是封闭集合。新增或改变解析语义需升级数据 Schema 或评测协议。

## questions.jsonl 完整 Schema

下面的 union Schema 覆盖 v1 的三种题型。每一行独立按该 Schema 校验。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://llmbenchlab.local/schemas/question-v1.json",
  "title": "LLMBenchLab Question v1",
  "oneOf": [
    { "$ref": "#/$defs/exact_match" },
    { "$ref": "#/$defs/multiple_choice" },
    { "$ref": "#/$defs/numeric" }
  ],
  "$defs": {
    "metadata": {
      "type": "object",
      "default": {},
      "additionalProperties": true
    },
    "exact_match_config": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "case_sensitive": { "type": "boolean", "default": false },
        "normalize_whitespace": { "type": "boolean", "default": true }
      }
    },
    "numeric_answer": {
      "oneOf": [
        { "type": "number" },
        {
          "type": "string",
          "pattern": "^[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?$",
          "maxLength": 128
        }
      ]
    },
    "exact_match": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "type", "prompt", "answer", "metadata"],
      "properties": {
        "id": {
          "type": "string",
          "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
          "maxLength": 128
        },
        "type": { "const": "exact_match" },
        "prompt": { "type": "string", "minLength": 1, "maxLength": 20000 },
        "answer": { "type": "string", "maxLength": 4000 },
        "evaluator_config": { "$ref": "#/$defs/exact_match_config" },
        "metadata": { "$ref": "#/$defs/metadata" }
      }
    },
    "multiple_choice": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "type", "prompt", "choices", "answer", "metadata"],
      "properties": {
        "id": {
          "type": "string",
          "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
          "maxLength": 128
        },
        "type": { "const": "multiple_choice" },
        "prompt": { "type": "string", "minLength": 1, "maxLength": 20000 },
        "choices": {
          "type": "object",
          "minProperties": 2,
          "maxProperties": 26,
          "propertyNames": { "pattern": "^[A-Z]$" },
          "additionalProperties": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000
          }
        },
        "answer": { "type": "string", "pattern": "^[A-Z]$" },
        "metadata": { "$ref": "#/$defs/metadata" }
      }
    },
    "numeric": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "type", "prompt", "answer", "evaluator_config", "metadata"],
      "properties": {
        "id": {
          "type": "string",
          "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
          "maxLength": 128
        },
        "type": { "const": "numeric" },
        "prompt": { "type": "string", "minLength": 1, "maxLength": 20000 },
        "answer": { "$ref": "#/$defs/numeric_answer" },
        "evaluator_config": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "absolute_tolerance": {
              "type": "number",
              "minimum": 0,
              "default": 0
            },
            "relative_tolerance": {
              "type": "number",
              "minimum": 0,
              "default": 0
            }
          }
        },
        "metadata": { "$ref": "#/$defs/metadata" }
      }
    }
  }
}
```

JSON 标准本身不允许 NaN 或 Infinity；Importer 还必须显式禁止宽松解析器接受这些非标准值。数字字符串用于保留高精度十进制表示，不允许表达式、单位或千位分隔符。

跨字段语义约束：

- 同一 Benchmark 内所有 `id` 必须唯一。
- multiple choice 的 `answer` 必须恰好是 `choices` 中存在的键。
- choices 键必须稳定且明确；v1 只允许单个大写 `A` 到 `Z`，渲染时按键字典序排列。
- numeric tolerance 必须是有限非负值；缺失项按 0 处理。`answer` 解析后也必须有限。
- `metadata` 用于标签、难度、主题等非执行信息。它不得改变评分；任何影响评分的值必须放在 `evaluator_config`。
- 问题正文、选项、答案和 metadata 都视为不可信数据，前端必须按文本转义展示。

## 合法示例

### manifest.json

```json
{
  "schema_version": "llmbenchlab-dataset-v1",
  "id": "format-example",
  "name": "Dataset Format Example",
  "version": "1.0.0",
  "description": "仅用于说明数据格式的三题独立示例，不是内置 demo-general。",
  "dimension": "general",
  "language": "zh-CN",
  "license": "MIT",
  "source": "Original examples authored for the LLMBenchLab format guide",
  "evaluator": {
    "name": "builtin-objective",
    "version": "1.0",
    "mapping": {
      "exact_match": "exact_match_v1",
      "multiple_choice": "multiple_choice_v1",
      "numeric": "numeric_v1"
    }
  },
  "prompt_template": {
    "system": "你正在参加客观题评测。请遵循题目要求给出简短的最终答案。",
    "user": "{prompt}\n{choices}"
  },
  "question_count": 3
}
```

### questions.jsonl

下面三行分别是 exact match、multiple choice 和 numeric；实际文件中每个 object 占一行。

```jsonl
{"id":"sample-exact-001","type":"exact_match","prompt":"法国的首都是哪座城市？只写城市名。","answer":"巴黎","evaluator_config":{"case_sensitive":false,"normalize_whitespace":true},"metadata":{"topic":"geography"}}
{"id":"sample-choice-001","type":"multiple_choice","prompt":"2 + 3 等于多少？只回答选项字母。","choices":{"A":"4","B":"5","C":"6","D":"7"},"answer":"B","metadata":{"topic":"arithmetic"}}
{"id":"sample-numeric-001","type":"numeric","prompt":"标准重力加速度约为多少 m/s²？答案保留一位小数。","answer":9.8,"evaluator_config":{"absolute_tolerance":0.05,"relative_tolerance":0},"metadata":{"topic":"science"}}
```

## 非法示例

### JSON 语法错误

```jsonl
{"id":"q-1","type":"exact_match","prompt":"示例","answer":"x","metadata":{},}
```

尾随逗号不是合法 JSON。错误应定位为 `questions.jsonl:1`，并包含解析器列号。

### 题型缺少必要字段

```jsonl
{"id":"q-2","type":"multiple_choice","prompt":"选择正确项","answer":"A","metadata":{}}
```

`multiple_choice` 缺少 `/choices`，Schema 校验失败。

### 标准答案不在 choices 中

```jsonl
{"id":"q-3","type":"multiple_choice","prompt":"选择正确项","choices":{"A":"一","B":"二"},"answer":"C","metadata":{}}
```

该行可通过局部类型校验，但跨字段校验必须以 `answer_not_in_choices` 拒绝。

### 非法数值与 tolerance

```jsonl
{"id":"q-4","type":"numeric","prompt":"给出数值","answer":"NaN","evaluator_config":{"absolute_tolerance":-0.1},"metadata":{}}
```

`answer` 不是受支持的十进制格式，且 tolerance 为负；Importer 应同时返回可定位的问题，而不是执行或强制转换。

### manifest 计数漂移

```json
{
  "schema_version": "llmbenchlab-dataset-v1",
  "id": "short-demo",
  "name": "Short Demo",
  "version": "1.0.0",
  "description": "错误示例",
  "dimension": "general",
  "language": "zh-CN",
  "license": "MIT",
  "source": "local example",
  "evaluator": {
    "name": "builtin-objective",
    "version": "1.0",
    "mapping": {
      "exact_match": "exact_match_v1",
      "multiple_choice": "multiple_choice_v1",
      "numeric": "numeric_v1"
    }
  },
  "prompt_template": { "system": "", "user": "{prompt}" },
  "question_count": 2
}
```

若 questions.jsonl 实际只有 1 行，导入必须以 `question_count_mismatch` 整体失败。

以下情况同样非法：重复问题 ID、未知题型、未知 Evaluator、额外顶层字段、`../questions.jsonl` 路径、符号链接逃逸、远程文件引用、未闭合模板占位符、重复 JSON object 键、超限文件或行。

## 导入校验规则

Importer 必须按下列顺序执行，任何错误都不能产生部分 Benchmark：

1. **边界与路径**：只接受允许导入根目录内的普通文件；解析真实路径后再次检查边界，拒绝路径穿越、符号链接和设备文件。固定读取 `manifest.json` 与 `questions.jsonl`，manifest 无权指定其他路径。
2. **资源限制**：MVP 默认 manifest 不超过 1 MiB、JSONL 不超过 16 MiB、单行不超过 256 KiB、问题不超过 10,000。配置可收紧，放宽时需评估内存和拒绝服务风险。
3. **编码与 JSON**：严格 UTF-8；拒绝空行、注释、尾逗号、跨行 JSON、重复对象键、NaN 和 Infinity。JSONL 错误必须带 1-based 行号与列号。
4. **Schema**：先验证 manifest，再逐行验证 Question union；报告 JSON Pointer 和稳定错误码。
5. **跨字段**：验证问题 ID 唯一、`question_count` 与有效行数相等、multiple choice 答案存在、数值有限、Prompt 占位符受支持。
6. **Evaluator 兼容**：每道题的 `type` 必须存在于 manifest mapping，映射必须是 v1 支持的对应 Evaluator；配置字段必须被该 Evaluator 接受。
7. **稳定 Hash**：按照下一节的唯一算法计算 Dataset SHA-256。
8. **冲突检查**：相同 `id + version + hash` 可幂等返回现有数据；相同 `id + version` 但 Hash 不同返回 409 冲突。
9. **原子写入**：Benchmark 和全部 Questions 在一个事务中写入；失败全部回滚。

建议的机器可读错误形状：

```json
{
  "error": {
    "code": "dataset_validation_error",
    "message": "Benchmark 数据校验失败",
    "issues": [
      {
        "file": "questions.jsonl",
        "line": 8,
        "column": 19,
        "pointer": "/choices",
        "code": "required_field_missing",
        "message": "multiple_choice 题目必须提供 choices"
      }
    ]
  }
}
```

消息不得回显整份文件、密钥形态 metadata 或服务器绝对路径。为便于一次修复多个问题，可以累计有限数量的 issues；达到上限后明确标注已截断。

## Dataset Hash 规则

格式 v1 使用项目自定义的 **JCS 风格**规范化，不声称完整实现 RFC 8785。算法是协议的一部分：

1. 使用严格 JSON 解析得到 manifest 和按行排序的问题对象。
2. manifest 仅保留 Schema 允许的源字段；`dataset_hash`、数据库 ID、导入时间、绝对路径等派生字段不参与。
3. 每个 JSON 值等价使用 Python `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` 规范化；对象键递归排序，数组顺序保持不变。
4. questions 保持 JSONL 原始题目顺序。题目顺序影响运行顺序，因而有意参与身份。
5. 精确载荷为 manifest 的规范化字符串加 LF，再依次加每题规范化字符串和 LF；最后一题后也必须有 LF。
6. 对载荷 UTF-8 字节计算 SHA-256，使用 64 位小写十六进制输出。

参考伪代码：

```python
canonical = lambda value: json.dumps(
    value,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
)
payload = canonical(source_manifest) + "\n"
payload += "".join(canonical(question) + "\n" for question in questions_in_file_order)
dataset_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

源文件缩进、对象键顺序、CRLF/LF 和 `\uXXXX` 与等价 Unicode 字符的写法不会改变 Hash。数组/题目顺序、字符串内容、答案、metadata、Prompt、Evaluator 配置以及 JSON 解析后仍不同的数值表示会改变 Hash。Hash 只能检测漂移，不能证明作者身份；未来需要供应链真实性时应在 Hash 之外增加签名。

## 版本与不可变规则

- `schema_version` 只在文件结构或解释方式发生破坏性变化时升级，例如 `llmbenchlab-dataset-v2`。
- `version` 属于单个 Benchmark。题目增删、顺序、正文、选项、标准答案、metadata、Prompt 或 Evaluator 配置改变时必须升级。
- 修正文案同样会改变模型输入，因此也要升级 version；不能以“只修 typo”为由原位覆盖。
- 数据库导入后视为不可变。更新方式是导入新 version，而不是修改旧 Questions。
- 旧 Run 永远引用其创建时的 Benchmark version、Hash 和标准答案快照。
- Loader 可以继续支持旧 Schema，但必须用对应旧 Hash/解释规则；禁止用新规则悄悄重算旧数据。

建议 Benchmark 版本遵循 SemVer：内容/评分语义不兼容改变提升 major；新增题目或兼容元数据提升 minor；不改变题目输入和评分的发布修复提升 patch。由于大多数内容修复会影响结果，保守地提升 minor 或 major，并在 changelog 说明。

## License 字段

`license` 描述问题内容和相关数据的使用许可，不是本仓库代码许可证。优先使用 SPDX License Identifier，例如 `MIT`、`CC-BY-4.0`、`Apache-2.0`。组合表达式使用 SPDX 表达式，如 `CC-BY-4.0 AND MIT`。

若数据不能公开分发，使用明确的自定义标识，例如 `LicenseRef-Proprietary` 或 `LicenseRef-Internal-Only`，并在 `source`/项目文档中说明访问条件。`Unknown` 不等于可自由使用；许可证未知的数据不得提交到公开仓库或内置镜像。

Importer 只校验字段存在、长度和已知格式，不替用户做法律判断。数据集贡献者负责确认：

- 有权存储、转换和评测这些题目；
- 是否允许重新分发题目与答案；
- 是否需要署名、NOTICE 或来源链接；
- 向第三方模型 API 发送题目是否符合许可与隐私义务。

内置 `demo-general` 必须包含 12–20 道完全由项目自行编写、无敏感信息的简单题，并显著标注“Demo 数据，不代表正式模型能力”。
