import unicodedata
from typing import Protocol

from rank_bm25 import BM25Okapi

from app.rag.loader import Reglamento, Section


class Retriever(Protocol):
    def search(self, query: str, top_k: int = 3) -> list[Section]: ...


def _tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    return [t for t in "".join(c if c.isalnum() else " " for c in ascii_text).split() if len(t) > 2]


class BM25Retriever:
    """Local lexical retriever over the reglamento sections.

    The document is small, so BM25 is enough locally; a Bedrock Knowledge Base or
    an embeddings index can implement the same `Retriever` protocol in AWS.
    """

    def __init__(self, reglamento: Reglamento):
        self._sections = reglamento.sections
        self._index = BM25Okapi([_tokenize(f"{s.title} {s.text}") for s in self._sections])

    def search(self, query: str, top_k: int = 3) -> list[Section]:
        scores = self._index.get_scores(_tokenize(query))
        ranked = sorted(zip(scores, self._sections), key=lambda pair: pair[0], reverse=True)
        hits = [section for score, section in ranked[:top_k] if score > 0]
        # Never return nothing: the agent must always be able to read the rules.
        return hits or self._sections[:top_k]
