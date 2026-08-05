# Fixture: pydantic_usage

A small but deliberately varied Python repository used to validate the stage 2
AST code-evidence scanner (plan section 1652). It exercises every usage kind
the scanner must recognise and the two "uncertain" cases (dynamic import and
syntax error) plus same-name shadowing and test-code marking.

## Files

- `src/models.py` — `from pydantic import BaseModel, validator, root_validator`
  plus a `User(BaseModel)` with a nested `class Config:`, a `@validator` and a
  `@root_validator` decorator.
- `src/advanced.py` — `import pydantic` and `import pydantic as pyd`, attribute
  access (`pyd.BaseModel`, `pyd.Field`, `pydantic.VERSION`) and
  `class Settings(pydantic.BaseSettings)`.
- `src/with_alias.py` — `from pydantic import BaseModel as BM` and
  `class Product(BM)`.
- `src/shadowed.py` — `import pydantic` immediately re-bound by
  `pydantic = load_config()`; the later `pydantic.BaseModel` usage must be
  flagged `confidence=low`.
- `tests/test_models.py` — a `from pydantic import BaseModel` usage inside a
  test directory (must be marked `is_test_code`).
- `broken_syntax.py` — invalid Python; must surface as a `ParseError`, not abort
  the scan.
- `dynamic_loader.py` — `importlib.import_module("pydantic")`; must surface as a
  `DynamicImport`, never as a normal `CALL` usage.

## Expected usages (Recall target >= 90%)

The contract test (`tests/unit/test_fixture_code_evidence.py`) enumerates 19
intended (path, kind, symbol) fingerprints and asserts the scanner finds all of
them. Any fingerprint the scanner misses would lower Recall below 100%; the
>= 90% threshold leaves room for benign extras without masking real misses.

## Contract

No machine-absolute path, no LLM call, no execution of the fixture code. The
scanner only parses these files.
