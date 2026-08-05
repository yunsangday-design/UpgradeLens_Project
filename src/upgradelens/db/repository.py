"""Repository helpers for persisting stage 2 code evidence (stage 4 tables)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from upgradelens.db import models
from upgradelens.domain.code_evidence import CodeEvidenceReport


def persist_code_report(session: Session, report: CodeEvidenceReport) -> int:
    """Persist every usage of ``report`` into the ``code_evidence`` table."""
    count = 0
    for usage in report.usages:
        row = models.CodeEvidenceRow(
            dependency=report.dependency_name,
            path=usage.path,
            start_line=usage.start_line,
            end_line=usage.end_line,
            column=usage.column,
            kind=usage.kind.value,
            symbol=usage.symbol,
            snippet=usage.snippet,
            content_hash=usage.content_hash,
            is_test_code=usage.is_test_code,
            bound_as=usage.bound_as or "",
            confidence=usage.confidence,
        )
        session.add(row)
        count += 1
    session.commit()
    return count


def code_usage_count(session: Session, dependency: str | None = None) -> int:
    """Count stored code usages, optionally filtered by ``dependency``."""
    stmt = select(func.count(models.CodeEvidenceRow.id))
    if dependency is not None:
        stmt = stmt.where(models.CodeEvidenceRow.dependency == dependency)
    return int(session.execute(stmt).scalar_one())
