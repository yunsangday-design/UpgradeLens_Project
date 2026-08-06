# Core evaluation cases

Each sub-directory is one offline evaluation case for `upgradelens eval`.
Unlike the stage 1/2 fixtures, these are not scanner golden files — they
describe a scenario and the behaviour a correct system must show.

## Layout

```
<case_id>/
  case.yaml           # metadata + expectations (required)
  repo/               # the repository under analysis (required)
  model_report.json   # optional synthetic model output
```

## `case.yaml`

| Key | Meaning |
| --- | --- |
| `dependency`, `source_version`, `target_version` | what upgrade is being assessed |
| `skill_id` | which Skill Pack to load |
| `with_docs` | whether a documentation index is available |
| `model_report` | optional file with a synthetic model report |
| `expect` | the assertions, see below |

### `expect`

| Key | Meaning |
| --- | --- |
| `conclusion` | `impacted` / `no_impact` / `evidence_insufficient` |
| `min_verified_risks`, `max_verified_risks` | bounds on the verified section |
| `must_cite_paths` | files the system must locate (across *all* risks) |
| `must_flag_symbols` | symbols the system must surface (across *all* risks) |
| `must_quarantine_risk_ids` | risk ids that must **not** reach the verified section |
| `max_severity` | severity ceiling for verified risks |
| `partial` | whether the report must be marked incomplete |

## `model_report.json`

Evidence ids are content hashes, so they cannot be written by hand. Use
placeholders, which are resolved against the real bundle at run time:

- `{{code:<path>:<symbol>}}` — the code evidence for that symbol in that file
- `{{doc:<n>}}` — the *n*-th documentation chunk

Any id that is **not** a placeholder is passed through verbatim. That is how
`hallucinated_citation` injects a fabricated reference: the verifier must catch
it, and the `llm_only` baseline must be shown failing to.

## Adding a case

Drop in a new directory — `load_cases` discovers it automatically and
`test_eval_harness.py` will start enforcing it. The `hybrid` baseline is
required to pass every case.
