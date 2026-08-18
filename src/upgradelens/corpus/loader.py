"""Load the shared RAG corpus source descriptors (LS-1).

The built-in :file:`corpus/builtin/sources.yaml` holds the documentation facts
that used to live on the deprecated dependency-upgrade Skill Packs. A single
multi-document YAML file contains one :class:`DocSourceManifest` block per
package; :func:`load_builtin_corpus` flattens them into the individual
:class:`DocSourceSpec` entries the retriever indexes.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from upgradelens.domain.doc_source_spec import DocSourceManifest, DocSourceSpec

_BUILTIN = Path(__file__).resolve().parent / "builtin" / "sources.yaml"


def load_builtin_corpus() -> list[DocSourceSpec]:
    """Parse every manifest block in the built-in corpus YAML."""
    return list(iter_builtin_sources())


def iter_builtin_sources() -> Iterable[DocSourceSpec]:
    if not _BUILTIN.is_file():
        return
    text = _BUILTIN.read_text(encoding="utf-8")
    for doc in yaml.safe_load_all(text):
        if not doc:
            continue
        manifest = DocSourceManifest.model_validate(doc)
        yield from manifest.sources


def builtin_corpus_path() -> Path:
    return _BUILTIN


__all__ = ["load_builtin_corpus", "iter_builtin_sources", "builtin_corpus_path"]
