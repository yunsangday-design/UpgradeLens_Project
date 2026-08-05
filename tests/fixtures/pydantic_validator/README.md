# Fixture: `pydantic_validator`

## Input

A repository whose only manifest is `requirements.txt`, containing a single
exact pin of the target dependency:

```text
pydantic==1.10.13
```

Request: analyse `pydantic` against target version `2.0.0`.

## Expected

- `status = resolved` — one unambiguous `==` pin, so the current version is known.
- `current_version = 1.10.13`, `current_specifier = ==1.10.13`.
- `transition = upgrade`, `cross_major = true` — 1.x to 2.x crosses a major boundary.
- One declaration located at `line:2` (1-based, comment on line 1).
- No warnings, no errors.

## Why

This is the baseline happy path. It proves that an exact pin in
`requirements.txt` yields a definite current version, that line numbers are
1-based and real (not fabricated), and that a major-version jump is detected.
It also anchors the JSON contract that all other fixtures vary from.
