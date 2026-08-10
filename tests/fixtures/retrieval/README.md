# Retrieval evaluation cases (ROADMAP Step 4, B0)

This directory is a **container** of retrieval evaluation cases for the
`upgradelens eval retrieval-baseline` command. It is *not* a stage-1 scan
fixture (no `request.json` / `expected_dependency_scan.json`).

Each `*.yaml` file is one labelled case:

```yaml
case_id: pydantic_validator
package: pydantic
source_version: "1.x"
target_version: "2.x"
pattern_id: pydantic_validator      # a UsagePattern id inside the skill
code_symbols: [validator, "@validator", field_validator]
expected_chunks:                    # leaf chunk titles (heading_path[-1]) that must be recalled
  - "@validator → @field_validator"
```

The baseline runner ingests the built-in skills' offline fixtures, then for
each case runs the curated FTS5 path (`skill.sources` × `pattern.retrieval_queries`
with a `pattern.match` boost) and records recall@k / MRR / top-k hit rate over
the ranked evidence. These numbers are the reproducible pre-sqlite-vec baseline
that any later retrieval change must preserve or beat.

Run it:

```bash
upgradelens eval retrieval-baseline --format md
```
