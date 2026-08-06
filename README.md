# UpgradeLens

Evidence-driven dependency upgrade pre-audit agent. This repository implements
the full **stage 1-8** pipeline (declaration scan → AST code evidence → Skill
Packs → RAG document retrieval → model-backed assessment → verification & eval
→ live doc fetch → patch draft), an **MCP server**, a **Streamlit demo**, and a
**PR-comment** command that posts the assessment back to GitHub.

> Status: stages 1-8, MCP server, Streamlit demo, and `comment-pr` are
> implemented. The model gateway defaults to `fake` (offline, deterministic);
> switch to `live`/`replay` for real or recorded LLM runs.

## Requirements

- `uv` (https://docs.astral.sh/uv/)
- Python 3.12 (managed by `uv`; the system Python is not used)

## macOS / zsh

```zsh
# install uv (official installer)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# run
uv sync --all-groups --locked
uv run pytest
uv run pytest --cov=upgradelens --cov-report=term-missing
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

## Windows / PowerShell

```powershell
# install uv (official installer)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# run
uv sync --all-groups --locked
uv run pytest
uv run pytest --cov=upgradelens --cov-report=term-missing
uv run ruff format --check .
uv run ruff check .
uv run mypy src
```

> **Compatibility note:** the `windows-latest` GitHub Actions runner runs the
> same `uv sync --all-groups --locked` and the same quality commands as macOS.
> Passing Windows CI means the build is continuously checked on Windows. It is
> **not** the same as the native Windows manual Gate performed after stage 4.

## CLI (stage 1)

```zsh
uv run upgradelens scan-dependency \
  --repo tests/fixtures/pydantic_validator/repo \
  --dependency pydantic \
  --target-version 2.0.0
```

### Posting the assessment to a GitHub PR

`comment-pr` runs the same assessment as `assess` and posts the rendered report
as a comment on a pull request or issue. It reuses the project's SSRF-guarded,
traced HTTP path, so posting stays inside the same security model (no ad-hoc
HTTP, token never logged).

```zsh
# Offline-safe preview (renders and prints, does not post):
uv run upgradelens comment-pr \
  --repo . \
  --dependency pydantic --target-version 2.0 \
  --slug owner/repo --pr 123 \
  --mode fake --dry-run

# Real post (token from --token or the GITHUB_TOKEN env var):
uv run upgradelens comment-pr \
  --repo . \
  --dependency pydantic --target-version 2.0 \
  --slug owner/repo --pr 123 \
  --mode live --token "$GITHUB_TOKEN"
```

Options:

| Option | Required | Meaning |
|---|---|---|
| `--repo` | yes | Repository root to scan. |
| `--dependency` | yes | Dependency name, any casing (`PyDantic` works). |
| `--target-version` | yes | Target PEP 440 version. |
| `--manifest` | no | Scan a single manifest instead of auto-discovery. |

Exit codes: `0` scan completed (any status, including `not_found`), `1` the
request was rejected, `2` argparse usage error.

### Output contract

```json
{
  "schema_version": "1.0",
  "requested_name": "pydantic",
  "dependency_name": "pydantic",
  "status": "resolved",
  "current_version": "1.10.13",
  "current_specifier": "==1.10.13",
  "target_version": "2.0.0",
  "transition": "upgrade",
  "cross_major": true,
  "declarations": [
    {
      "manifest_type": "requirements_txt",
      "path": "requirements.txt",
      "location": "line:2",
      "raw": "pydantic==1.10.13",
      "raw_name": "pydantic",
      "specifier": "==1.10.13",
      "extras": [],
      "marker": null
    }
  ],
  "warnings": [],
  "errors": []
}
```

`status` is the confidence of the answer:

| Status | Meaning |
|---|---|
| `resolved` | A single unambiguous `==` pin; `current_version` is trustworthy. |
| `ambiguous` | Declared as a range or with conflicting pins. `current_version`, `transition` and `cross_major` are deliberately left empty — a manifest states which versions are *allowed*, not which one is *installed*. |
| `not_found` | No supported manifest, or the dependency is not declared in one. |
| `invalid` | The request was rejected, or every manifest was unreadable. |
| `unsupported` | An explicitly requested manifest is not a supported format. |

`path` is always a POSIX path relative to `--repo`, so results are identical on
macOS and Windows. Machine-absolute paths never appear in the output, including
in error messages.

`location` uses two formats on purpose: real 1-based line numbers
(`line:2`) for `requirements.txt`, and array indices
(`[project].dependencies[1]`) for `pyproject.toml`, because `tomllib` does not
expose the source line of an array element. Fabricating a line number there
would be undetectable downstream.

### Stage 1 scope

Only `pyproject.toml` (`[project].dependencies`, PEP 621) and `requirements.txt`
in the repository root are parsed. Poetry, Pipfile, `setup.py` and dynamic
dependencies are reported as structured issues rather than silently skipped.
The target repository is never imported, installed or executed.

## Project layout

```text
src/upgradelens/
  cli.py               # scan-dependency entry point
  config.py            # minimal pydantic-settings
  platform.py          # cross-platform path/text helpers
  observability/       # structured logging
  domain/              # stage 1 domain models
  analyzers/           # manifest parsers + version comparison
tests/
  unit/                # domain, parsers, comparison, fixture contracts
  cli/                 # CLI behaviour and exit codes
  fixtures/            # offline pydantic fixtures (request + expected JSON)
```

See `升级透镜第一步实施计划_环境与依赖解析.md` for the full plan and acceptance gates.
