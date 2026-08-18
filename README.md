# UpgradeLens

A general-purpose, **evidence-driven software-engineering agent**. UpgradeLens
turns a plain-language request into a structured task and runs one of **five
capabilities** through a single controlled execution layer — dependency
upgrade, PR review, issue repair, breaking-change analysis and security review
— producing verified findings with evidence citations, action proposals and a
unified verification gate.

> **Status:** five capabilities live behind one brain (`EngineeringAgent`,
> v0.3.0); S0–S9 complete; Supervisor+Handoff multi-agent orchestration;
> unified Workbench, GitHub PR bot and MCP server all verified. 737+ tests,
> ruff/mypy clean. The model gateway defaults to `fake` (offline,
> deterministic); switch to `live`/`replay` for real or recorded LLM runs.
> The recommended entry point for new code is
> [`EngineeringAgent`](#python-api).

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

# One goal in, one unified result out — any of the five capabilities
uv run upgradelens run "review this PR and run a security scan" \
  --repo tests/fixtures/eval/capabilities/repo --dry-run
uv run upgradelens run "fix bug: login button fails"

# Per-capability gold-set evaluation (no-hallucination gate)
uv run upgradelens eval-capability

# Architecture comparison (offline)
uv run upgradelens eval-compare
uv run upgradelens eval-ablate

# Quality gates
uv run pytest
uv run ruff check .
uv run mypy src
```

## Python API

`EngineeringAgent` is the unified entry point for all five capabilities —
CLI, MCP, the Workbench and the Supervisor all drive the same kernel:

```python
from upgradelens import EngineeringAgent

agent = EngineeringAgent(mode="fake")

# One sentence routes to any capability (or fans out to several)
result = agent.run("review the security of https://github.com/o/r")
print(result.capabilities, result.verification_passed)
for finding in result.findings:
    print(f"  [{finding.severity.value}] {finding.summary}")

# Multi-capability decomposition without executing anything
plan = agent.run("review this PR and run a security scan", repo="./repo", dry_run=True)
print(plan.capabilities)  # ['security_review', 'pr_review']
```

For dependency-upgrade-only callers, `DependencyUpgradeAgent` remains the
focused front door:

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
| `run` | **Unified entry (A4):** one natural-language goal → routed to any of the five capabilities → unified `EngineeringResult`. |
| `agent` | Natural-language entry for dependency upgrade: route → plan → agent loop → run artifacts. |
| `assess` | Run the full upgrade pipeline: scan → code evidence → RAG → model → verify → patch draft. |
| `capability-list` | List the five unified capabilities and their allowed tools. |
| `capability-run` | Run one capability end-to-end by explicit `--kind`. |
| `eval-capability` | **A5 gold-set gate:** per-capability scoreboard (pass rate / verification / no-hallucination) with `--fail-under` CI threshold. |
| `comment-pr` | Run `assess` and post the report as a GitHub PR comment. |
| `eval` / `eval-compare` / `eval-ablate` / `eval-replay` | Offline evaluation suites (S8 baseline / architecture comparison / ablation / recorded replay). |
| `scan-dependency` | Parse manifests and resolve the declared version. |
| `ingest-docs` / `retrieve-docs` | Ingest and query the SQLite doc store (FTS5 + sqlite-vec). |
| `mcp` | Start the MCP server (stdio transport). |

Also: `upgradelens-pr-bot --repo owner/repo --pr N [--mode live]` reviews a
real GitHub PR (pr_review + security_review) and posts the report as a
comment.

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

## Verifier as a first-class citizen

Every conclusion UpgradeLens produces is **verified before it is believed**.
The verifier is not a post-hoc filter — it is a structural part of the result
contract shared by all five capabilities:

- A finding promoted to `verified` **must cite at least one evidence id**;
  the pydantic model itself rejects the construction otherwise
  (`core/finding.py`).
- Findings without evidence are **degraded** (surfaced, never silently
  dropped); findings citing non-existent evidence are **quarantined**.
- Every capability declares its own verifiers and runs them through the same
  gate (`CapabilityRunResult.verification`), so "verified" means the same
  thing for an upgrade risk, a security finding and a PR review comment.
- Only verified findings may drive automatic remediation proposals; degraded
  ones always require a human.

This aligns with the 2026 shift from "LLM as generator" to "LLM as
verifier": the harness — not the model weights — decides what is trustworthy.

## The Harness: five engineering guardrails

Agent reliability is decided more by the harness than by model weights.
UpgradeLens ships five guardrails, all on by default:

| Guardrail | What it does | Where |
|---|---|---|
| **Budget** | hard cap on total tokens per run; model calls are rejected beyond it | `ModelConfig.max_total_tokens` |
| **Coverage** | per-capability evidence coverage; insufficient coverage triggers deterministic supplementary retrieval before any conclusion | `agent/coverage.py`, per-capability `CoveragePolicy` |
| **Fake / replay determinism** | every path runs offline in `fake`; live runs can be recorded and replayed byte-for-byte | `llm/gateway.py` |
| **SSRF guard** | allow-listed hosts only; GitHub URLs validated (scheme / host / slug / internal-address) before any fetch | `tools/fetcher.py`, `agent/router.py` |
| **Sandbox, zero source mutation** | the analysed repository is never modified; patches are drafts verified in a sandbox; every write action defaults to `requires_approval=True` | `plan/executor.py`, `core/action.py` |

Token values never enter traces or artifacts; the GitHub token travels in
headers only.

## Capabilities as Skills / Subagents

UpgradeLens maps to the Claude Code mental model: a **Skill** runs in the
main context (a single-capability request drives one deterministic state
machine), a **Subagent** runs in its own isolated context with a fixed role
(a multi-capability request fans out via Supervisor + Handoff, then
aggregates through one verification gate).

| Capability | Inputs | Allowed tools | Verifier gate | Output findings |
|---|---|---|---|---|
| `dependency_upgrade` | repo, dependency, target/source version | clone_repo, scan_dependency, scan_code, retrieve_for_package, supplement_retrieval | `verify_report` | dependency risks (verified / degraded / quarantined) |
| `pr_review` | repo, unified diff | load_change_set, build_repository_context, analyze_change_impact, retrieve_code_context, retrieve_docs, recommend_tests, verify_findings | `pr_review_verifier` | logic / compatibility / test_gap / impact / documentation |
| `issue_repair` | repo, issue text | load_issue, reproduce_issue, locate_root_cause, generate_patch, run_tests, verify_fix | issue verifier | root-cause findings + patch & test proposals |
| `security_review` | repo, unified diff, dependency | load_change_set, build_repository_context, semgrep_scan, dependency_cve_check, verify_findings | `security_review_verifier` | security:secret / injection / dependency |
| `breaking_change` | repo, unified diff, from/to version | load_change_set, detect_breaking_changes, extract_public_symbols, classify_api_change, compare_versions, verify_report | breaking-change verifier | deletion / rename / signature / type / behaviour changes |

Adding a capability means registering one `TaskKind` plus a capability entry
— the router, dispatcher, supervisor, CLI `run`, MCP `run_task` and the
Workbench pick it up without any changes of their own.

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

Exposes 15 tools: the upgrade toolkit (`scan_dependency`, `scan_code`,
`assess`, `ingest_docs`, `retrieve_docs`, `fetch_docs`, `list_skills`,
`resolve_skill`, `list_capabilities`, `resolve_capability`, `run_eval`) plus
the unified five-capability surface — `list_unified_capabilities`,
`run_capability` (explicit kind), `run_supervisor` (natural-language
Supervisor orchestration) and `run_task` (the `EngineeringAgent` entry, the
same object the CLI `run` command prints).

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
