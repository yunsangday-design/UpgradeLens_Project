#!/usr/bin/env python
"""Offline demo: show the Agent planning, collecting evidence, and verifying.

Runs entirely in FAKE mode — no network, no API key, no database required.
Demonstrates the full agent loop: route → plan → collect → verify → report.

Usage::

    uv run python demo/run_offline.py
    uv run python demo/run_offline.py --repo tests/fixtures/eval/alias_import/repo
    uv run python demo/run_offline.py --out runs/demo

The script prints a human-readable summary and writes run artifacts to
``runs/demo/`` (or the specified ``--out``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure the package is importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from upgradelens import DependencyUpgradeAgent

DEFAULT_REPO = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "eval" / "alias_import" / "repo"
)
DEFAULT_GOAL = "upgrade pydantic in {repo} to 2.0"


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline UpgradeLens Agent demo (FAKE mode).")
    parser.add_argument(
        "--repo",
        type=Path,
        default=DEFAULT_REPO,
        help="Repository to analyse (default: a shipped eval fixture).",
    )
    parser.add_argument(
        "--goal",
        default=None,
        help="Natural-language goal (default: auto-generated from --repo).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("runs/demo"),
        help="Directory for run artifacts (default: runs/demo).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Route and plan only; do not execute the assessment.",
    )
    args = parser.parse_args()

    repo_str = str(args.repo.resolve())
    goal = args.goal or DEFAULT_GOAL.format(repo=repo_str)

    print("=" * 60)
    print("UpgradeLens — Offline Agent Demo (FAKE mode)")
    print("=" * 60)
    print(f"  goal: {goal}")
    print(f"  out:  {args.out}")
    print("  mode: fake (deterministic, no network)")
    print()

    agent = DependencyUpgradeAgent(mode="fake")
    result = agent.run(goal, repo=args.repo, out_dir=args.out, dry_run=args.dry_run)

    # -- Intent -- #
    print("-" * 60)
    print("1. Intent routing")
    print("-" * 60)
    print(f"  kind:       {result.intent.kind}")
    print(f"  repo:       {result.intent.repo}")
    print(f"  dependency: {result.intent.dependency}")
    print(f"  target:     {result.intent.target_version}")
    print(f"  source:     {result.intent.source_version}")
    print()

    if result.plan is not None:
        print("-" * 60)
        print("2. Execution plan")
        print("-" * 60)
        for step in result.plan.steps:
            status_icon = {"succeeded": "[OK]", "failed": "[FAIL]", "skipped": "[SKIP]"}.get(
                step.status, f"[{step.status}]"
            )
            print(f"  {status_icon} #{step.seq} {step.tool}: {step.reason}")
        print()

    if args.dry_run:
        print("Dry run — skipping assessment.")
        if result.run_dir:
            print(f"Artifacts: {result.run_dir}")
        return 0

    if result.error:
        print(f"ERROR: {result.error}")
        return 1

    if result.verified is None:
        print("No assessment produced (intent was not an upgrade task).")
        return 0

    # -- Assessment -- #
    print("-" * 60)
    print("3. Verified assessment")
    print("-" * 60)
    v = result.verified
    print(f"  conclusion:      {v.conclusion.value}")
    print(f"  verified risks:  {len(v.verified_risks)}")
    print(f"  degraded risks:  {len(v.degraded_risks)}")
    print(f"  citation rate:   {v.citation_existence_rate:.0%}")
    print(f"  partial:         {v.partial}")
    print()

    if v.verified_risks:
        print("  Verified risks:")
        for r in v.verified_risks:
            print(f"    [{r.severity.value}] {r.title}")
            if r.code_evidence_ids:
                print(f"      code: {', '.join(r.code_evidence_ids[:3])}")
            if r.doc_evidence_ids:
                print(f"      docs: {', '.join(r.doc_evidence_ids[:3])}")
        print()

    if v.degraded_risks:
        print("  Degraded risks (evidence insufficient):")
        for r in v.degraded_risks:
            print(f"    [{r.severity.value}] {r.title}")
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

    if result.degradations:
        print("-" * 60)
        print("5. Degradations")
        print("-" * 60)
        for d in result.degradations:
            print(f"  - {d}")
        print()

    if result.run_dir:
        print("=" * 60)
        print(f"Artifacts written to: {result.run_dir}")
        print("  - intent.json  (routed intent)")
        print("  - plan.json    (execution plan)")
        print("  - trace.jsonl   (tool call trace)")
        print("  - report.json   (machine-readable report)")
        print("  - report.md     (human-readable report)")
        print("  - RUN.md        (run summary)")
        print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
