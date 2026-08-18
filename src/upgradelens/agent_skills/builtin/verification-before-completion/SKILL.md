---
skill_id: verification-before-completion
name: Verification Before Completion
applies_to:
  - pr_review
  - issue_repair
  - security_review
  - breaking_change
  - dependency_upgrade
language: en
version: "1.0.0"
description: >
  An agent may only claim success after an independent verifier has run. This
  skill bans "fixed"/"passed"/"safe" language unless the verification result
  backs it.
when_to_use:
  - Before marking a task done, a finding resolved, or a patch ready.
steps:
  - Enumerate the checks that must pass for this task (tests, static, schema).
  - Run the verifier; capture the structured VerificationResult.
  - Map each claimed fix to the check(s) that confirm it.
  - If any required check fails, keep status "failed"/"needs_human"; do not relabel.
constraints:
  - Never use "fixed", "passed", "resolved", "safe" without a passing verification.
  - Do not self-attest success; the verifier is the authority.
  - A failed verification must downgrade the claim, not be hand-waved away.
completion_criteria:
  - Each positive claim has a corresponding passing check in the VerificationResult.
  - The summary states the verification status verbatim from the result.
evidence_policy:
  verification_required: true
---

# Verification Before Completion

Confidence is not a result. This skill makes the verifier the gatekeeper of
"done".

## Rules

1. **No verdict without a run.** Produce or reference a
   `VerificationResult`. Claims without it are unsanctioned.
2. **Map claims to checks.** Every "this is fixed" must point at the specific
   check that proves it. Orphan claims are rejected.
3. **Fail loudly.** A red check stays red. Do not rewrite the summary to say
   "resolved" while the verifier says otherwise.

## Language guard

- Allowed: "verifier reports 12/12 checks passed", "patch addresses finding F3
  (confirmed by test T2)".
- Forbidden: "I fixed it", "should be safe now", "probably works".
