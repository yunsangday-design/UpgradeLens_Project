# Fixture: `pydantic_serialization`

## Input

A repository that has **both** supported manifests, each declaring the target
dependency as a range rather than an exact pin:

`pyproject.toml`

```toml
[project]
dependencies = [
  "pydantic>=1.10,<2",
]
```

`requirements.txt`

```text
pydantic>=1.10,<2
orjson==3.9.5
```

## Expected

- `status = ambiguous`, `current_version = null`.
- `current_specifier = <2,>=1.10` — the canonical `SpecifierSet` string, which
  `packaging` sorts deterministically regardless of how it was written.
- `transition = unknown`, `cross_major = null`.
- Two declarations, `pyproject.toml` first and `requirements.txt` second,
  following the fixed manifest discovery order (never the OS directory order).
- Two warnings: `duplicate_declaration` (with the location of the second
  occurrence) and `ambiguous_specifier` (a whole-result conclusion, so it
  carries no location).
- No errors: a range is a legitimate declaration, not a parse failure.

## Why

This is the most important negative case in stage 1. `pydantic>=1.10,<2` states
what is *allowed*, not what is *installed*. Reporting `1.10` here would produce
a confident, wrong upgrade diff. The fixture therefore forces the analyzer to
downgrade its own certainty to `ambiguous` and to refuse to emit a transition or
a `cross_major` verdict. It simultaneously locks the deterministic ordering of
declarations across two manifests.
