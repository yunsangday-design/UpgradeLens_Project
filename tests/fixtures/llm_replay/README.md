# LLM replay fixtures

This directory holds **recorded model responses** so the assess pipeline can be
replayed fully offline (deterministic, no API key, no cost). It is consumed by
the gateway's `replay` mode (CLI `--mode replay` and the Streamlit demo's
`replay` mode).

## Record real responses (needs a key)

Run the live pipeline once with a real OpenAI-compatible endpoint and let it
write each node's response to a sub-directory:

```bash
export UPGRADELENS_MODEL_API_KEY=sk-...        # or pass --api-key
upgradelens assess \
  --repo tests/fixtures/eval/validator_direct_hit/repo \
  --dependency pydantic --target-version 2.0.0 \
  --mode live --model qwen-flash \
  --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --record-replay tests/fixtures/llm_replay/pydantic_validator
```

This produces, per node:

- `planner.json`
- `extractor__<pattern_id>.json` (one per plan item)
- `impact_analyzer.json`

Each file is `{"output": <model response>}`, exactly what `replay` mode reads.

## Replay offline

```bash
upgradelens assess --repo tests/fixtures/eval/validator_direct_hit/repo \
  --dependency pydantic --target-version 2.0.0 \
  --mode replay --replay-dir tests/fixtures/llm_replay/pydantic_validator
```

Or in the Streamlit demo: set `模型模式 = replay` and paste the directory above.

> The recording mechanism is covered by `tests/unit/test_replay.py` (a
> key-less fake run is recorded, then replayed, and must reproduce the same
> report). No real key is required for that test.
