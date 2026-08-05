# Fixture: `pydantic_root_validator`

## Input

A repository whose only manifest is `pyproject.toml`, declaring the target
dependency as an exact pin inside `[project].dependencies`:

```toml
[project]
dependencies = [
  "fastapi==0.95.2",
  "pydantic==1.10.13",
  "uvicorn>=0.20",
]
```

Request: analyse `pydantic` against target version `2.0.0`.

## Expected

- `status = resolved`, `current_version = 1.10.13`.
- `transition = upgrade`, `cross_major = true`.
- One declaration with `manifest_type = pyproject_toml` and location
  `[project].dependencies[1]` (0-based array index).
- No warnings, no errors.

## Why

`tomllib` cannot report the source line of an individual array element, so this
fixture locks in the array-index location format instead of a fabricated line
number. Apart from `manifest_type` and `location`, the result must be identical
to `pydantic_validator` — proving both parsers emit the same domain model.
