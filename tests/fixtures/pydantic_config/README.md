# Fixture: `pydantic_config`

## Input

A `requirements.txt` that combines several real-world details at once:

```text
# 依赖清单：配置层使用 pydantic 的 email 校验能力
# 本文件用于验证 UTF-8 注释、行尾注释、extras 与 marker

Pydantic[email]==1.10.13 ; python_version >= "3.8"  # 配置模型使用
python-dotenv==1.0.0
```

The request deliberately spells the dependency as `PyDantic`.

## Expected

- `requested_name = PyDantic` is preserved, `dependency_name = pydantic` is the
  canonical form used for matching.
- The declaration's own spelling `Pydantic` is preserved in `raw_name`.
- `raw` keeps the declaration text with the trailing `# ...` comment stripped.
- `extras = ["email"]`, `marker = python_version >= "3.8"`.
- `status = resolved`, `current_version = 1.10.13`, `transition = upgrade`,
  `cross_major = true`.
- One warning `marker_conditional_declaration` pointing at `line:4`.
- Location is `line:4` — blank lines and comment lines still consume line numbers.

## Why

Three rules are locked in here. First, name matching must go through
`canonicalize_name`, so casing in either the request or the manifest is
irrelevant. Second, UTF-8 comments and pip-style trailing comments must not
break PEP 508 parsing. Third, an environment marker does not make the version
ambiguous, but it is not silently dropped either — it surfaces as a structured
warning, because a marker-guarded pin may not describe every environment.
