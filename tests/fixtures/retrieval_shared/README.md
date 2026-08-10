# Shared-corpus retrieval cases (S6)

Labelled cases for `run_shared_corpus_baseline`, which retrieves using only the
package, the upgrade window, the user intent and the symbols found in the
scanned code — **no curated Skill queries**.

Kept separate from `tests/fixtures/retrieval/` on purpose:

| directory | path measured | why it still exists |
| --- | --- | --- |
| `retrieval/` | curated, per-pattern, FTS5-only | frozen B0 regression guard |
| `retrieval_shared/` | shared corpus, no curated queries | what the product actually runs |

Cases therefore leave `pattern_id` empty and cover five dependencies:

- `pydantic`, `sqlalchemy` — still have Skill Packs, ingested through the
  deprecated compatibility path;
- `flask`, `httpx`, `attrs` — corpus-only, ingested from
  `tests/fixtures/corpus/*/manifest.yaml`, no Skill anywhere.

Comparing those two groups is the S6 acceptance question: a dependency without
a Skill must not retrieve measurably worse than one with a Skill.

`expected_chunks` holds leaf chunk titles (`heading_path[-1]`), so they must
match the snapshot headings exactly.
