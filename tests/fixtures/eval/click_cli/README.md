# Click CLI Demo Fixture

Tests online fallback (S16 two-stage: PyPI → web search) for **click** —
a package NOT in the local RAG corpus.

## What this tests

- `click` 7.x → 8.x upgrade triggers RAG miss (`NO_PACKAGE`)
- Stage 1: PyPI JSON API discovers docs URL
- Stage 2 (if Stage 1 insufficient): DuckDuckGo web search for migration guide

## Repo structure

- `main.py` — uses Click 7.x APIs: `@click.command()`, `@click.option()`,
  `@click.group()`, `@click.argument()`, `click.Path()`
- `pyproject.toml` — pins `click>=7.0,<8.0`

## Expected breaking changes in click 8.x

- `prompt=` parameter removed from `@click.option()` (use `confirmation_prompt`)
- Some internal API renames
