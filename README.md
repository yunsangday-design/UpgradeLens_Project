# UpgradeLens

Evidence-driven dependency upgrade pre-audit agent. UpgradeLens analyses a
repository for the impact of upgrading a Python dependency: it scans the
codebase for usages of the dependency's API, retrieves relevant documentation,
and produces a **verified** impact report with specific breaking-change risks,
evidence citations, and migration recommendations.

> **Status:** stages S0–S8 complete (518 tests, ruff/mypy clean). The model
> gateway defaults to `fake` (offline, deterministic); switch to `live`/
> `replay` for real or recorded LLM runs. The recommended entry point for new
> code is [`DependencyUpgradeAgent`](#python-api).

## Quick start

```zsh
# Install
uv sync --all-groups --locked

# Offline demo (no API key, no network)
uv run python demo/run_offline.py

# Run an assessment
uv run upgradelens assess \
  --repo tests/fixtures/eval/alias_import/repo \
  --dependency pydantic --target-version 2.0 \
  --mode fake

# Run the agent (plan-driven, with tool trace)
uv run upgradelens agent "upgrade pydantic to 2.0" \
  --repo tests/fixtures/eval/alias_import/repo \
  --dependency pydantic --target-version 2.0 \
  --mode fake

# Architecture comparison (offline)
uv run upgradelens eval-compare
uv run upgradelens eval-ablate

# Quality gates
uv run pytest
uv run ruff check .
uv run mypy src
```

## Python API

The `DependencyUpgradeAgent` class is the single entry point for programmatic
use — CLI, MCP and demo all drive the same kernel:

```python
from upgradelens import DependencyUpgradeAgent

agent = DependencyUpgradeAgent(mode="fake")
result = agent.run("upgrade pydantic to 2.0", repo="./repo", dependency="pydantic")

if result.verified:
    print(result.verified.conclusion)  # impacted / no_impact / evidence_insufficient
    for risk in result.verified.verified_risks:
        print(f"  [{risk.severity.value}] {risk.title}")

# Run the deterministic pipeline directly (baseline)
outcome = agent.run_pipeline(repo="./repo", dependency="pydantic", target_version="2.0")
```

**Modes:**

| Mode | Network | API Key | Use case |
|---|---|---|---|
| `fake` | no | no | CI, demos, unit tests (deterministic) |
| `replay` | no | no | replay recorded live responses offline |
| `live` | yes | yes | real LLM assessment |

## CLI

| Command | What it does |
|---|---|
| `scan-dependency` | Parse manifests and resolve the declared version. |
| `assess` | Run the full pipeline: scan → code evidence → RAG → model → verify → patch draft. |
| `agent` | Natural-language entry: route → plan → agent loop → run artifacts. |
| `comment-pr` | Run `assess` and post the report as a GitHub PR comment. |
| `eval` | Offline hybrid evaluation (baseline suite). |
| `eval-compare` | S8 architecture comparison: direct LLM vs pipeline vs agent. |
| `eval-ablate` | S8 ablation: isolate verifier / supplement / agent value. |
| `eval-replay` | S8 comparison against recorded live model responses. |
| `ingest-docs` | Ingest documentation into the SQLite evidence store. |
| `retrieve-docs` | Query the doc store (FTS5 + sqlite-vec). |
| `mcp` | Start the MCP server (stdio transport). |

### Source version inference

`assess`, `comment-pr`, and `agent` run the same dependency scan **first** and
infer the *from-version* from the manifest:

- **declared** — an exact pin (`pydantic==1.10.13`) is used verbatim;
- **inferred** — a range (`pydantic>=1.10`) is reported as a range;
- **conflict** — conflicting declarations are flagged;
- **unknown** — when nothing is declared, the assessment degrades honestly.

Pass `--source-version` to override (treated as `user`-provided).

### Run artifacts

`upgradelens agent` writes a self-contained directory under `runs/<run_id>/`:

```
runs/<run_id>/
  intent.json    # routed intent (repo/dependency/version)
  plan.json      # execution plan (steps + statuses)
  trace.jsonl    # one JSON object per tool call
  report.json    # verified assessment (machine-readable)
  report.md      # verified assessment (human-readable)
  RUN.md         # run summary
```

## Architecture

```
                    ┌──────────────────────────────────────────┐
                    │          DependencyUpgradeAgent           │
                    │         (.run(goal) / .run_pipeline())    │
                    └──────────────────┬───────────────────────┘
                                       │
           ┌───────────┬───────────────┼───────────────┬──────────┐
           ▼           ▼               ▼               ▼          ▼
        CLI agent    CLI assess     MCP assess     Streamlit    demo script
                    / comment-pr                   demo         run_offline.py
                                       │
                    ┌──────────────────┴───────────────────────┐
                    │              Agent Loop                   │
                    │  route → plan → collect → verify →       │
                    │  supplement → re-verify → report          │
                    └──────────────────┬───────────────────────┘
                                       │
                    ┌──────────────────┴───────────────────────┐
                    │           Pipeline (baseline)            │
                    │  scan_dependency → scan_code →            │
                    │  retrieve_for_package → analyse → verify  │
                    └──────────────────────────────────────────┘
```

### One pipeline, several front doors

`assess`, `comment-pr`, the MCP `assess` tool, the Streamlit demo and the agent
all share the same evidence-collection and verification core. The agent loop
(`run_agent`) wraps the pipeline with plan-driven collection, coverage
assessment (S4), supplementary retrieval, and verifier feedback (S5). When the
agent cannot produce a report it falls back to the deterministic pipeline.

### Evidence contract

Every verified risk must cite real evidence:

- **code evidence** — AST-scanned usage of the dependency's API (symbol, path, line);
- **doc evidence** — ingested documentation chunks (retrieved via FTS5 + sqlite-vec).

Risks with no evidence are **degraded**, not dropped silently. Risks citing
non-existent evidence are **quarantined** by the verifier. This is the core
no-hallucination guarantee, enforced by `tests/unit/test_s8_ci_gate.py`.

### Evaluation (S8)

The offline comparison harness runs three architectures over 18 cases covering
pydantic, sqlalchemy and fastapi:

| System | Retrieval | Verification | Agent loop | Supplement |
|---|---|---|---|---|
| `direct_llm` | no | no | no | no |
| `fixed_pipeline` | yes | yes | no | no |
| `agent` | yes | yes | yes | yes |
| `agent_no_supplement` | yes | yes | yes | no (ablation) |

Key metric: **verifier detection rate** — the verifier catches 100% of
fabricated claims in `fixed_pipeline` and `agent`, while `direct_llm` trusts
them (0%). See `uv run upgradelens eval-compare` for the full table.

## Streamlit demo

```zsh
uv run --extra demo streamlit run demo/app.py
```

Tabs: Overview, Agent Plan (step statuses + cost), Risk Details, Code Evidence,
Report Markdown, Patch Draft. Default is `fake` mode (offline). Check "Agent
模式" to see the plan-driven loop with tool trace and token cost.

## MCP server

```zsh
uv run upgradelens-mcp
```

Exposes 11 tools: `scan_dependency`, `scan_code`, `assess`, `ingest_docs`,
`retrieve_docs`, `fetch_docs`, `list_skills`, `resolve_skill`,
`list_capabilities`, `resolve_capability`, `run_eval`.

## Project layout

```text
src/upgradelens/
  __init__.py            # public API: DependencyUpgradeAgent, AgentResult
  cli.py                 # all CLI commands
  config.py              # pydantic-settings
  agent/
    api.py               # DependencyUpgradeAgent (unified entry point)
    router.py            # NL intent routing
    planner.py           # plan construction
    loop.py              # plan-driven agent loop (S3-S5)
    plan.py              # AgentPlan / AgentPlanStep models
    run_store.py         # run artifact writer
    coverage.py          # evidence coverage assessment (S4)
  pipeline.py            # the deterministic assessment sequence
  domain/                # domain models (dependency, code, doc, skill)
  analyzers/             # manifest parsers, AST code scan
  skills/                # Skill Pack loader + built-in registry
  docs/                  # doc cleaning, chunking, ingest, FTS5 + sqlite-vec
  db/                    # SQLite evidence store
  llm/                   # fake/replay/live gateway, prompts, query rewrite
  verify/                # verifier, risk rules, remediation
  patch/                 # patch draft generator
  plan/                  # UpgradePlan export + executor (S7)
  report/                # Markdown / JSON rendering
  eval/                  # offline evaluation harness, S8 comparison/ablation
  tools/                 # ToolRegistry, fetcher, GitHub client, trace
  mcp/                   # MCP server
demo/
  app.py                 # Streamlit UI
  pipeline.py            # headless assess + agent demo functions
  run_offline.py         # CLI demo script (offline, no deps)
tests/
  unit/                  # 518 tests
  fixtures/eval/         # 18 eval cases (pydantic/sqlalchemy/fastapi)
```

## Limitations

- Only `pyproject.toml` and `requirements.txt` manifests are parsed;
- Only Python is analysed (no multi-language AST);
- Only `pydantic` and `sqlalchemy` have built-in Skill Packs; other dependencies
  use the generic skill (code-evidence only, risks degrade without docs);
- The target repository is never imported, installed or executed;
- `live` mode requires an OpenAI-compatible API (tested with Alibaba Cloud
  qwen-plus/qwen-max/qwen-flash);
- No automatic code application — patch drafts and UpgradePlans are
  human-reviewed before execution.
