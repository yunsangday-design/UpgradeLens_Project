---
skill_id: systematic-issue-diagnosis
name: Systematic Issue Diagnosis
applies_to:
  - issue_repair
  - security_review
  - breaking_change
language: en
version: "1.0.0"
description: >
  Diagnose the root cause before proposing any change. A structured search for
  the mechanism behind a symptom prevents whack-a-mole fixes and false patches.
when_to_use:
  - An issue, failing test, or vulnerability report must be explained, not just patched.
steps:
  - Reproduce or localise the symptom precisely (file, function, trigger).
  - Enumerate candidate mechanisms; do not stop at the first plausible one.
  - For each candidate, gather evidence that confirms or excludes it.
  - Pick the mechanism with the strongest evidence; state why alternatives were ruled out.
  - Define the minimal fix scope that addresses the root cause, not just the symptom.
constraints:
  - Do not edit code until the root-cause mechanism is stated and evidenced.
  - Do not propose a patch that only silences the symptom (e.g. blanket try/except).
  - If evidence is insufficient, report "needs_human" rather than guessing.
completion_criteria:
  - The chosen mechanism is stated with supporting evidence ids.
  - Alternative mechanisms are explicitly ruled in or out.
  - The proposed change targets the root cause and its blast radius is stated.
evidence_policy:
  required_for_root_cause: true
---

# Systematic Issue Diagnosis

The most expensive repair is the wrong one. This skill forces the agent to
understand the failure before touching the code.

## Workflow

1. **Localise.** Pin the symptom to a concrete location and trigger. Vague
   "it crashes" is not a diagnosis.
2. **Hypothesise broadly.** List several mechanisms. The first idea is often
   shallow.
3. **Test hypotheses with evidence.** Read the code path, run a targeted check,
   or retrieve the relevant docs. Record what confirms or excludes each.
4. **Decide on evidence.** Choose the best-supported mechanism; say why the
   others lost.
5. **Scope the fix.** Fix the cause, name the blast radius, and avoid changes
   that merely hide the symptom.

## Red flags

- A patch appears before the mechanism is written down.
- A catch-all `except:` is offered as the fix.
- "Probably" without a cited evidence id.
