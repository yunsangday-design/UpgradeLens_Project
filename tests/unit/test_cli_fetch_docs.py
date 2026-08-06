"""End-to-end test for `upgradelens fetch-docs` (stage 7) with the network mocked."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from sqlalchemy import select

from upgradelens.cli import main
from upgradelens.db import models
from upgradelens.db.database import engine_for, init_db, session_for
from upgradelens.tools.fetcher import RestrictedFetcher


class FakeResponse:
    def __init__(
        self,
        code: int,
        body: bytes,
        headers: dict[str, str] | None = None,
        url: str = "http://example.com/x",
    ) -> None:
        self._code = code
        self._body = body
        self._pos = 0
        self.headers = headers or {}
        self._url = url

    def getcode(self) -> int:
        return self._code

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out = self._body[self._pos :]
            self._pos = len(self._body)
        else:
            out = self._body[self._pos : self._pos + n]
            self._pos += len(out)
        return out

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        pass

    def items(self) -> list[tuple[str, str]]:
        return list(self.headers.items())


def _fake_opener(req: urllib.request.Request, timeout: float) -> FakeResponse:
    url = req.full_url
    if "pypi.org/pypi" in url:
        payload = {
            "info": {"version": "2.7.0"},
            "releases": {
                "2.7.0": [{"upload_time_iso_8601": "2024-01-01T00:00:00Z", "yanked": False}]
            },
        }
        return FakeResponse(
            200,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"},
            url=url,
        )
    return FakeResponse(
        200,
        b"# Migration Guide\n\n`Field` validator changes in v2.",
        {"Content-Type": "text/markdown"},
        url=url,
    )


def test_fetch_docs_ingests_and_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Patch the network surface, not the method, so the real Tool Trace records.
    monkeypatch.setattr("upgradelens.tools.fetcher._default_open", _fake_opener)
    monkeypatch.setattr(RestrictedFetcher, "_resolve_ips", lambda self, host: ["93.184.216.34"])
    db = tmp_path / "docs.db"
    cache_dir = tmp_path / "cache"

    rc = main(
        [
            "fetch-docs",
            "--db",
            str(db),
            "--dependency",
            "pydantic",
            "--cache-dir",
            str(cache_dir),
            "--format",
            "json",
        ]
    )
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["dependency"] == "pydantic"
    assert out["skill_id"] == "pydantic_v1_to_v2"
    # The PyPI changelog is always ingested; skill-declared URL sources too.
    assert out["ingested"] >= 1
    assert out["network_calls"] >= 1
    assert len(out["tool_trace"]) >= 1
    assert any(e["tool"] == "fetcher" for e in out["tool_trace"])

    # The data really landed in the SQLite store.
    engine = engine_for(db)
    init_db(engine)
    session = session_for(engine)()
    try:
        sources = session.execute(select(models.DocSourceRow)).scalars().all()
        assert len(sources) >= 1
        changelog = next((s for s in sources if s.id.endswith(":pypi-changelog")), None)
        assert changelog is not None
        assert changelog.trust_level == "official"
    finally:
        session.close()
