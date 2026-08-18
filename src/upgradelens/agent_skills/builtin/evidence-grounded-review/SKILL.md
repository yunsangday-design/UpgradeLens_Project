---
skill_id: evidence-grounded-review
name: Evidence-Grounded Review
applies_to:
  - pr_review
  - issue_repair
  - security_review
  - breaking_change
  - dependency_upgrade
language: en
version: "1.0.0"
description: >
  Every conclusion an agent emits must be traceable to a concrete piece of
  evidence. This skill removes unverified assertions and forces a verifiable
  evidence chain for each finding.
when_to_use:
  - Producing findings, risk ratings, or "this will break" claims.
  - Summarising a code change, a dependency diff, or a security report.
steps:
  - Collect the raw signals (diff hunks, retrieved docs, tool output).
  - For each candidate finding, record the exact evidence id(s) it rests on.
  - Drop any finding with no evidence id, or downgrade it to "suspected".
  - Tag each finding with a confidence derived from evidence strength, never from intuition.
  - In the summary, cite evidence ids; never state a fact the trace cannot reach.
constraints:
  - A finding with status "verified" MUST carry at least one evidence_id.
  - Do not invent file paths, line numbers, or API names that are not in the evidence.
  - Separate "verified", "suspected", and "informational" tiers explicitly.
completion_criteria:
  - Every emitted finding has an evidence_id or is explicitly "suspected"/"informational".
  - The summary references only findings backed by the trace.
evidence_policy:
  required_for_verified: true
  tiers: [verified, suspected, informational]
---

# Evidence-Grounded Review

Upgrades, reviews and repairs fail when an agent asserts something it cannot
prove. This skill makes the evidence chain the unit of trust.

## Principles

1. **No evidence, no claim.** If you cannot point at the diff line, the doc
   snippet, or the tool output, the statement is not a finding — it is a
   hypothesis, and must be labelled as such.
2. **Three tiers, not one.** `verified` (evidence present and strong),
   `suspected` (plausible, weak/incomplete evidence), `informational` (context,
   no action required). The verifier rejects `verified` findings without an
   `evidence_id`.
3. **Confidence from evidence, not mood.** Confidence is a function of how
   direct and authoritative the evidence is, not how sure the agent feels.

## How to apply

- While gathering signals, attach an id to each: `diff:src/x.py:42`,
  `doc:pydantic-migration:validator`, `tool:grep:Config`.
- When writing a finding, set `evidence_ids: [...]` and pick the tier.
- In the final summary, cite the ids. A reader should be able to reconstruct
  the reasoning from the trace alone.
