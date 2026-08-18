---
skill_id: safe-dependency-migration
name: Safe Dependency Migration
applies_to:
  - dependency_upgrade
language: en
version: "1.0.0"
description: >
  A capability-agnostic method for ANY dependency upgrade. It deliberately holds
  no version-specific facts -- those live in the shared RAG corpus -- and instead
  prescribes a safe, evidence-driven migration workflow plus where the mechanical
  rewrites come from (a TransformationPack).
when_to_use:
  - Migrating a dependency from one version range to another, of any ecosystem.
steps:
  - Identify the exact from/to version range and the public API surface in use.
  - Retrieve the trusted migration docs for this dependency from the RAG corpus.
  - Enumerate the breaking changes that intersect the used API surface.
  - Run the applicable TransformationPack rewrites only on verified risk sites.
  - Verify each rewrite with the project's test/static checks before claiming done.
constraints:
  - Do NOT hard-code version facts, removed APIs, or package names in this skill.
  - Mechanical rewrites come only from the TransformationPack, never invented here.
  - A rewrite is proposed only where evidence shows the old API is actually used.
  - Every breaking-change finding must cite the doc source and the usage evidence.
completion_criteria:
  - The from/to range and used API surface are stated with evidence.
  - Breaking changes are scoped to actually-used APIs, not the whole changelog.
  - Rewrites are evidence-gated and pass verification before being called "done".
evidence_policy:
  required_for_breaking_change: true
  facts_belong_to_corpus: true
---

# Safe Dependency Migration

This skill is deliberately *not* a knowledge pack for one library. It is the
method that works for any upgrade, separating **how to migrate** (here) from
**what changed** (RAG corpus) and **how to rewrite mechanically** (TransformationPack).

## The method

1. **Bound the move.** State `from` and `to` version ranges and the API surface
   the code actually touches. Guessing the surface wastes effort and invents risk.
2. **Fetch facts from the corpus.** The shared RAG corpus holds the trusted
   migration docs. Retrieve, don't memorise. This is why this skill carries no
   version-specific knowledge.
3. **Intersect with usage.** A breaking change matters only if the code uses the
   affected API. Cross the changelog with the code symbol scan.
4. **Rewrite mechanically, verify.** Apply only the TransformationPack rules that
   match a verified usage site. Hand-written rewrites are proposals, not facts.
5. **Verify before done.** The verifier gates "migrated". See
   `verification-before-completion`.

## What this skill is NOT

- Not a pydantic/sqlalchemy/anything-specific cheat sheet.
- Not a place to paste removed function names or version numbers.
- Not a patch generator; the TransformationPack owns mechanical rewrites.
