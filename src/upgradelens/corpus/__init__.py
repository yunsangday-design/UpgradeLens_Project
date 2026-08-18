"""Shared RAG corpus (LS-1): dependency documentation as findable facts."""

from upgradelens.corpus.loader import (
    builtin_corpus_path,
    iter_builtin_sources,
    load_builtin_corpus,
)

__all__ = ["load_builtin_corpus", "iter_builtin_sources", "builtin_corpus_path"]
