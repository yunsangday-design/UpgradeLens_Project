# Shared corpus fixtures

Offline documentation snapshots used by the shared-corpus retrieval evaluation
(`tests/fixtures/retrieval_shared/`).

Each package directory is a self-contained corpus entry:

```
<package>/
  manifest.yaml          # DocSourceSpec entries (package, version window, trust)
  sources/*.md           # the snapshots the manifest points at
```

**None of these packages has a Skill Pack.** That is the point: since S6 a
dependency joins the shared corpus by adding data here, and retrieval works off
the package name, the upgrade window and the symbols found in the scanned code.
If any of these fixtures ever required a Skill to be retrievable, the
decoupling has regressed.

The snapshots are *condensed* versions of the upstream migration notes — real
API changes, trimmed to the sections the evaluation cases assert on, so the
fixtures stay reviewable. They are evidence fixtures, not a mirror of upstream
documentation; `url` records where the content came from.
