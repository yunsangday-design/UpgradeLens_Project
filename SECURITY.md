# Security

## Scope boundaries

UpgradeLens is a **read-only pre-audit** tool. During stage 0-1 it:

- reads the target repository's manifest files (`requirements.txt`, `pyproject.toml`);
- does **not** install target dependencies;
- does **not** execute target code, tests, or build steps;
- does **not** modify the target repository;
- does **not** make network calls.

## Secrets

- API keys and other secrets are read only from environment variables or a local `.env` file.
- `.env` is git-ignored and must never be committed.
- `Settings.require_secret()` raises a clear error when a required secret is missing and never prints the secret value.
- Logs and traces must not include secret values. Use `pydantic.SecretStr` and redact before logging.

## Logging and trace redaction

- Structured logs may carry only explicit `data_*` fields.
- Never pass secrets, full environment dumps, or absolute machine paths into logs.

## Future stages

Network access (GitHub/PyPI/official docs) and Human-in-the-loop approvals are introduced in later stages behind controlled Tools with auditing. They are out of scope for stage 0-1.
