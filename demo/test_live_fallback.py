#!/usr/bin/env python
"""Live demo: test two-stage online fallback with click (not in RAG corpus)."""

import json as _json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from upgradelens import DependencyUpgradeAgent

REPO = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "eval" / "celery_upgrade" / "repo"
)
GOAL = "upgrade celery in {repo} from 4.x to 5.x"

print("=" * 60)
print("UpgradeLens — Live Demo (two-stage online fallback)")
print("=" * 60)
print(f"  goal: {GOAL.format(repo=str(REPO))}")
print(f"  repo: {REPO}")
print("  mode: live (real LLM + real network)")
print()

agent = DependencyUpgradeAgent(mode="live")
result = agent.run(
    GOAL.format(repo=str(REPO)),
    repo=str(REPO),
    out_dir=Path("runs/demo-click-live"),
)

# -- Debug: bundle doc evidence_ids -- #
print("-" * 60)
print("0. Bundle doc evidence (debug)")
print("-" * 60)
# Print what doc evidence exists in the run artifacts
_run_dir = result.run_dir
if _run_dir and Path(_run_dir).exists():
    _report_path = Path(_run_dir) / "report.json"
    if _report_path.exists():
        _r = _json.loads(_report_path.read_text())
        _sum = _r.get("evidence_summary", {})
        print(f"  evidence_summary: {_sum}")
print()

# -- Intent -- #
print("-" * 60)
print("1. Intent routing")
print("-" * 60)
if result.intent:
    print(f"  kind:       {result.intent.kind}")
    print(f"  repo:       {result.intent.repo}")
    print(f"  dependency: {result.intent.dependency}")
    print(f"  target:     {result.intent.target_version}")
    print(f"  source:     {result.intent.source_version}")
else:
    print("  No intent produced")
print()

# -- Plan -- #
if result.plan is not None:
    print("-" * 60)
    print("2. Execution plan")
    print("-" * 60)
    for step in result.plan.steps:
        icon = {"succeeded": "[OK]", "failed": "[FAIL]", "skipped": "[SKIP]"}.get(
            step.status, f"[{step.status}]"
        )
        print(f"  {icon} #{step.seq} {step.tool}: {step.reason}")
    print()

# -- Verified report -- #
if result.verified is not None:
    print("-" * 60)
    print("3. Verified assessment")
    print("-" * 60)
    v = result.verified
    print(f"  conclusion:      {v.conclusion.value}")
    print(f"  verified risks:  {len(v.verified_risks)}")
    print(f"  degraded risks:  {len(v.degraded_risks)}")
    print(f"  citation rate:   {v.citation_existence_rate:.0%}")
    if v.verified_risks:
        print("\n  Verified risks:")
        for r in v.verified_risks[:5]:
            sev = getattr(r.severity, "value", r.severity)
            print(f"    [{sev}] {r.title}")
    print()

# -- Cost -- #
if result.gateway is not None:
    ledger = result.gateway.ledger
    total_tokens = sum(r.prompt_tokens + r.completion_tokens for r in ledger)
    print("-" * 60)
    print("4. Cost")
    print("-" * 60)
    print(f"  model calls:  {len(ledger)}")
    print(f"  total tokens: {total_tokens}")
    print()

# -- Error -- #
if result.error:
    print(f"ERROR: {result.error}")

if result.run_dir:
    print("=" * 60)
    print(f"Artifacts written to: {result.run_dir}")
    print("=" * 60)
