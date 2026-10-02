"""
SOLARGRID AI - RAG engine (engineering-evidence retrieval layer).

Ported from the recruiter ``rag.py`` (PDF -> word-window chunks -> MiniLM
embeddings -> cosine similarity -> confidence threshold), re-shaped to the
SOLARGRID contract:

  kept     PDF ingestion (pypdf), 200-word / 40-word-overlap chunking,
           ``all-MiniLM-L6-v2`` + cosine similarity
  added    markdown ingestion (the current Tool 16 corpus is *.md),
           page numbers + section labels + chunk ids on every chunk,
           lazy loading (no work at import time), corpus-change detection,
           WEAK vs NONE evidence quality, visible ingestion gaps
  removed  the LLM answer step (``rag_query`` / ``call_llm``): RAG returns
           evidence, it does not compose answers or conclusions
           the chart helpers and their hard-coded fallback scores (85.0 / 82.0):
           an unknown score must stay unknown, never become a made-up number

What this module is NOT
-----------------------
* Not a source of live operational truth: it never touches the database and
  never reports current MW / SOC / reserve / limits of the running system.
* Not a calculator and not an interpreter: no feasibility logic, no "safe".

Tool 16 integration (not applied here; Tool 16 keeps persistence + envelope)
----------------------------------------------------------------------------
    from RAG.rag_engine import get_engine
    outcome = get_engine().retrieve(query, top_k=top_k, topic=topic)
    for h in outcome.chunks:
        d = h.to_evidence_dict()  # document_id, source, source_type,
                                  # document_metadata, chunk_id, text,
                                  # relevance_score, score_type
    quality = outcome.evidence_quality   # STRONG | WEAK | NONE

Failure semantics (all distinguishable):
  corpus missing/empty/unreadable ...... CorpusUnavailableError
  embedding backend not installed ...... EmbeddingBackendUnavailableError
  retrieved, but below the threshold ... chunks=[], evidence_quality="WEAK"
  nothing to retrieve (empty query) .... chunks=[], evidence_quality="NONE"
  a document that could not be read .... listed in ``skipped_documents``
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from agents.prompts import RAG_SYSTEM_PROMPT, RAG_STRUCT_RULES

try:  # optional accelerator; a pure-python path is used when absent
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR_CANDIDATES = ("documents", "docs")   # first one that exists under RAG/
DEFAULT_CORPUS_DIR = ROOT / "RAG" / CORPUS_DIR_CANDIDATES[0]


def default_corpus_dir(root: Path | str = ROOT) -> Path:
    """RAG/documents (Tool 16's current path) if present, else RAG/docs."""
    base = Path(root) / "RAG"
    for name in CORPUS_DIR_CANDIDATES:
        if (base / name).is_dir():
            return base / name
    return base / CORPUS_DIR_CANDIDATES[0]

# Engineering-corpus retrieval threshold.  The original recruiter/CV RAG used
# 0.35, which systematically discarded relevant SolarGrid generator/BESS
# evidence.  0.18 is calibrated against the bundled engineering corpus; real
# deployments should re-calibrate it against their approved document set.
MAX_WORDS = 200
OVERLAP = 40
TOP_K = 3
CONFIDENCE_THRESHOLD = 0.18
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
TOPIC_BOOST = 0.05  # ranking hint only; never lifts a chunk over the threshold
SCORE_TYPE = "cosine_similarity"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the "
    "this to was were will with shall must".split()
)


class RAGEngineError(Exception):
    """Base class for RAG engine failures."""


class CorpusUnavailableError(RAGEngineError):
    """The document corpus is missing, unreadable or empty.

    Distinct from "no relevant evidence": callers must not treat this as an
    empty (and therefore harmless) result.
    """


class EmbeddingBackendUnavailableError(RAGEngineError):
    """The embedding model/library could not be loaded."""


# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------

def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> List[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def chunk_words(pages: Sequence[Optional[str]], max_words: int = MAX_WORDS,
                overlap: int = OVERLAP) -> List[Tuple[str, int, int]]:
    """Word-window chunking (from rag.py) that also tracks page numbers.

    Returns ``(text, page_start, page_end)`` with 1-based pages. Fixes over
    rag.py: ``None`` page text is tolerated, it terminates when the last word
    is reached (no trailing all-overlap chunk), and ``overlap >= max_words``
    is rejected instead of looping forever.
    """
    if max_words <= 0 or overlap < 0 or overlap >= max_words:
        raise ValueError("Require max_words > 0 and 0 <= overlap < max_words.")
    words: List[str] = []
    page_of: List[int] = []
    for page_no, page_text in enumerate(pages, start=1):
        for word in (page_text or "").split():
            words.append(word)
            page_of.append(page_no)
    chunks: List[Tuple[str, int, int]] = []
    start, step = 0, max_words - overlap
    while start < len(words):
        end = min(start + max_words, len(words))
        chunks.append((" ".join(words[start:end]), page_of[start], page_of[end - 1]))
        if end >= len(words):
            break
        start += step
    return chunks


def _pack_paragraphs(body: str, max_chars: int) -> List[str]:
    pieces: List[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        while len(para) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(para[:max_chars])
            para = para[max_chars:].lstrip()
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            pieces.append(current)
            current = para
    if current:
        pieces.append(current)
    return pieces


def split_markdown(text: str, max_chars: int = 1200) -> List[Tuple[Optional[str], str]]:
    """Split markdown into ``(section_path, chunk_text)`` pairs along headings."""
    blocks: List[Tuple[Optional[str], str]] = []
    stack: List[Tuple[int, str]] = []
    buf: List[str] = []
    path: Optional[str] = None
    in_fence = False

    def flush() -> None:
        content = [ln for ln in buf if not _HEADING_RE.match(ln)]
        if not "\n".join(content).strip():
            return
        for piece in _pack_paragraphs("\n".join(buf), max_chars):
            blocks.append((path, piece))

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            flush()
            buf = [line]
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = " > ".join(t for _, t in stack)
        else:
            buf.append(line)
    flush()
    return blocks


def source_type_for(path: Path) -> str:
    """Same labelling Tool 16 uses today (DEMO in filename => synthetic)."""
    name = path.name.lower()
    return "synthetic" if ("demo" in name or "synthetic" in name) else "real_public"


def extract_pdf_pages(path: Path) -> List[str]:
    """Default PDF reader (pypdf), one string per page. ``None`` -> ''."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RAGEngineError("pypdf is required to read PDF documents.") from exc
    return [(page.extract_text() or "") for page in PdfReader(str(path)).pages]


# ---------------------------------------------------------------------------
# Embedders
# ---------------------------------------------------------------------------

class Embedder(Protocol):
    name: str

    def encode(self, texts: Sequence[str]) -> Any:
        """Return one vector per text (list of lists or a 2-D array)."""


class SentenceTransformerEmbedder:
    """Lazy wrapper around ``SentenceTransformer`` (model loads on first use)."""

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        self.name = model_name
        self._model: Any = None

    def encode(self, texts: Sequence[str]) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                allow_download = str(os.getenv("SOLARGRID_RAG_ALLOW_MODEL_DOWNLOAD", "false")).strip().lower() in {"1", "true", "yes", "on"}
                if allow_download:
                    self._model = SentenceTransformer(self.name)
                else:
                    # Local-first loading prevents the first Execute click from
                    # blocking on a Hugging Face download/network timeout. If
                    # MiniLM is not already cached, get_engine() immediately
                    # falls back to the deterministic HashingEmbedder.
                    self._model = SentenceTransformer(self.name, local_files_only=True)
            except Exception as exc:  # ImportError, download/offline failures...
                raise EmbeddingBackendUnavailableError(
                    f"Cannot load embedding model {self.name!r}: {exc}") from exc
        return self._model.encode(list(texts))


class HashingEmbedder:
    """Dependency-free, deterministic bag-of-words embedder.

    For offline use and tests only. Its cosine scores are NOT comparable with
    MiniLM scores, so the 0.35 threshold does not transfer to it.
    """

    def __init__(self, dim: int = 2048):
        self.name = f"hashing-bow-{dim}"
        self.dim = dim

    def encode(self, texts: Sequence[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in tokenize(text):
                h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:8], "big")
                vec[h % self.dim] += 1.0
            out.append(vec)
        return out


_default_embedder: Optional[SentenceTransformerEmbedder] = None


def default_embedder() -> SentenceTransformerEmbedder:
    """Process-wide embedder so the model is loaded at most once."""
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = SentenceTransformerEmbedder()
    return _default_embedder


# ---------------------------------------------------------------------------
# Vector math (numpy when available, pure python otherwise)
# ---------------------------------------------------------------------------

def _normalise_rows(vectors: Any, use_numpy: bool) -> Any:
    if use_numpy:
        arr = np.asarray(vectors, dtype=float)
        if arr.ndim != 2:
            raise RAGEngineError("Embedder must return a 2-D array of vectors.")
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms
    rows = []
    for v in vectors:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        rows.append([x / n for x in v])
    return rows


def _cosine_scores(query_vec: Any, matrix: Any, use_numpy: bool) -> List[float]:
    q = _normalise_rows([query_vec], use_numpy)
    if use_numpy:
        return [float(s) for s in (matrix @ q[0])]
    return [sum(a * b for a, b in zip(row, q[0])) for row in matrix]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineeringChunk:
    chunk_id: str
    document_id: str
    source: str
    source_type: str
    section: Optional[str]      # heading path (markdown) or page label (PDF)
    text: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: EngineeringChunk
    score: float                # raw cosine similarity

    def to_evidence_dict(self) -> Dict[str, Any]:
        """Shape compatible with the items Tool 16 already returns."""
        c = self.chunk
        return {
            "document_id": c.document_id,
            "source": c.source,
            "source_type": c.source_type,
            "document_metadata": dict(c.metadata),
            "chunk_id": c.chunk_id,
            "text": c.text,
            "relevance_score": round(self.score, 4),
            "score_type": SCORE_TYPE,
        }


@dataclass
class RetrievalOutcome:
    chunks: List[RetrievedChunk]
    evidence_quality: str                 # STRONG | WEAK | NONE
    best_score: Optional[float]           # None when nothing was scored
    min_score: float
    embedder: str
    skipped_documents: List[Dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class RAGEngine:
    """Embedding retrieval over engineering evidence using the canonical RAG contract."""

    # Canonical prompt/rules are imported from agents.prompts; they are exposed
    # as immutable contract metadata so downstream RAG consumers cannot drift
    # to a duplicate prompt source. The engine itself remains retrieval-only.
    system_prompt = RAG_SYSTEM_PROMPT
    structured_rules = RAG_STRUCT_RULES

    def __init__(self, corpus_dir: Optional[Path | str] = None, *,
                 embedder: Optional[Embedder] = None,
                 pdf_extractor: Optional[Callable[[Path], Sequence[Optional[str]]]] = None,
                 max_words: int = MAX_WORDS, overlap: int = OVERLAP,
                 max_chunk_chars: int = 1200, min_score: float = CONFIDENCE_THRESHOLD,
                 root: Optional[Path | str] = None, use_numpy: bool = True):
        if max_words <= 0 or overlap < 0 or overlap >= max_words:
            raise ValueError("Require max_words > 0 and 0 <= overlap < max_words.")
        self.root = Path(root) if root is not None else ROOT
        self.corpus_dir = Path(corpus_dir) if corpus_dir is not None else default_corpus_dir(self.root)
        self.embedder = embedder if embedder is not None else default_embedder()
        self._pdf_extractor = pdf_extractor or extract_pdf_pages
        self.max_words, self.overlap = max_words, overlap
        self.max_chunk_chars = max_chunk_chars
        self.min_score = min_score
        self._use_numpy = bool(use_numpy and np is not None)
        self._chunks: List[EngineeringChunk] = []
        self._topic_tokens: List[frozenset] = []
        self._matrix: Any = None
        self.skipped_documents: List[Dict[str, str]] = []
        self._loaded = False

    # -- indexing ----------------------------------------------------------

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def _make_chunk(self, path: Path, idx: int, section: Optional[str], text: str,
                    extra: Optional[Dict[str, Any]] = None) -> EngineeringChunk:
        rel, stype = self._rel(path), source_type_for(path)
        meta = {"path": rel, "source_type": stype, "section": section, "chunk_index": idx}
        meta.update(extra or {})
        return EngineeringChunk(f"{path.stem}#{idx}", path.stem, rel, stype, section, text, meta)

    def _chunks_for_markdown(self, path: Path) -> List[EngineeringChunk]:
        text = path.read_text(encoding="utf-8")
        return [self._make_chunk(path, i, section, body)
                for i, (section, body) in enumerate(split_markdown(text, self.max_chunk_chars))]

    def _chunks_for_pdf(self, path: Path) -> List[EngineeringChunk]:
        pages = list(self._pdf_extractor(path))
        out = []
        for i, (body, p0, p1) in enumerate(chunk_words(pages, self.max_words, self.overlap)):
            label = f"p. {p0}" if p0 == p1 else f"pp. {p0}-{p1}"
            out.append(self._make_chunk(path, i, label, body,
                                        {"page_start": p0, "page_end": p1}))
        return out

    def load(self) -> "RAGEngine":
        """(Re)build the index. Raises CorpusUnavailableError if unusable."""
        if not self.corpus_dir.is_dir():
            raise CorpusUnavailableError(f"Corpus directory not found: {self.corpus_dir}")
        paths = sorted(p for p in self.corpus_dir.rglob("*")
                       if p.suffix.lower() in (".md", ".pdf") and p.is_file())
        chunks: List[EngineeringChunk] = []
        skipped: List[Dict[str, str]] = []
        for path in paths:
            try:
                doc_chunks = (self._chunks_for_pdf(path) if path.suffix.lower() == ".pdf"
                              else self._chunks_for_markdown(path))
            except RAGEngineError:
                raise  # e.g. pypdf missing: the corpus cannot be read at all
            except Exception as exc:  # unreadable / corrupt document
                skipped.append({"source": self._rel(path), "reason": f"EXTRACTION_FAILED: {exc}"})
                continue
            if not doc_chunks:
                skipped.append({"source": self._rel(path), "reason": "NO_EXTRACTABLE_TEXT"})
                continue
            chunks.extend(doc_chunks)
        if not chunks:
            detail = f" ({len(skipped)} document(s) skipped)" if skipped else ""
            raise CorpusUnavailableError(f"No usable engineering documents in {self.corpus_dir}{detail}")

        texts = [f"{c.section}\n{c.text}" if c.metadata.get("page_start") is None and c.section
                 else c.text for c in chunks]
        vectors = self.embedder.encode(texts)
        if len(vectors) != len(chunks):
            raise RAGEngineError("Embedder returned a different number of vectors than chunks.")

        self._matrix = _normalise_rows(vectors, self._use_numpy)
        self._chunks = chunks
        self._topic_tokens = [frozenset(tokenize(f"{c.section or ''} {c.text}")) for c in chunks]
        self.skipped_documents = skipped
        self._loaded = True
        return self

    @property
    def chunk_count(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._chunks)

    # -- retrieval ---------------------------------------------------------

    def retrieve(self, query: str, top_k: int = TOP_K, topic: Optional[str] = None,
                 min_score: Optional[float] = None) -> RetrievalOutcome:
        """Rank chunks by cosine similarity and report evidence quality.

        The confidence threshold applies to the raw cosine score. ``topic`` is
        a ranking hint (small boost for chunks containing every topic term);
        it can reorder results but never rescues a below-threshold chunk.
        """
        if not self._loaded:
            self.load()
        threshold = self.min_score if min_score is None else min_score
        base = dict(min_score=threshold, embedder=self.embedder.name,
                    skipped_documents=list(self.skipped_documents))
        if not (query or "").strip() or top_k <= 0:
            return RetrievalOutcome([], "NONE", None, **base)

        q_vec = self.embedder.encode([query])[0]
        scores = _cosine_scores(q_vec, self._matrix, self._use_numpy)
        topic_terms = frozenset(tokenize(topic)) if topic else frozenset()

        ranked = []
        for i, score in enumerate(scores):
            boost = TOPIC_BOOST if topic_terms and topic_terms <= self._topic_tokens[i] else 0.0
            ranked.append((score + boost, score, i))
        ranked.sort(key=lambda r: (-r[0], r[2]))

        best = max(scores)
        kept = [RetrievedChunk(self._chunks[i], raw)
                for _, raw, i in ranked if raw >= threshold][:top_k]
        return RetrievalOutcome(kept, "STRONG" if kept else "WEAK", best, **base)

    def search(self, query: str, top_k: int = TOP_K, topic: Optional[str] = None,
               min_score: Optional[float] = None) -> List[RetrievedChunk]:
        """Chunks that pass the confidence threshold (may be empty)."""
        return self.retrieve(query, top_k, topic, min_score).chunks


# ---------------------------------------------------------------------------
# Cached access (avoids re-embedding the corpus on every call)
# ---------------------------------------------------------------------------

def corpus_fingerprint(corpus_dir: Path | str) -> Tuple:
    """(path, size, mtime_ns) for every document; changes when the corpus does."""
    d = Path(corpus_dir)
    if not d.is_dir():
        return ()
    return tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                 for p in sorted(d.rglob("*"))
                 if p.is_file() and p.suffix.lower() in (".md", ".pdf"))


_ENGINES: Dict[Tuple[str, int, Optional[float]], Tuple[Tuple, RAGEngine]] = {}


def get_engine(corpus_dir: Optional[Path | str] = None,
               embedder: Optional[Embedder] = None,
               min_score: Optional[float] = None) -> RAGEngine:
    """Return a loaded engine, rebuilding only if the corpus changed.

    The production preference remains MiniLM.  If that backend cannot be
    loaded (for example on an offline machine without a cached model), fall
    back to the deterministic hashing embedder so the bundled engineering
    corpus remains queryable.  The fallback is retrieval-only and is surfaced
    through ``RetrievalOutcome.embedder``; it never fabricates engineering
    values or bypasses the Safety Agent.
    """
    directory = Path(corpus_dir) if corpus_dir is not None else default_corpus_dir()
    requested_embedder = embedder
    key = (str(directory), id(requested_embedder), min_score)
    fp = corpus_fingerprint(directory)
    cached = _ENGINES.get(key)
    if cached is not None and cached[0] == fp and fp:
        return cached[1]
    kwargs = {} if min_score is None else {"min_score": min_score}
    try:
        engine = RAGEngine(directory, embedder=requested_embedder, **kwargs).load()
    except EmbeddingBackendUnavailableError:
        if requested_embedder is not None:
            raise
        # Offline/demo-safe fallback over the same traceable corpus.  Keep the
        # calibrated engineering threshold unless the caller supplied one.
        engine = RAGEngine(directory, embedder=HashingEmbedder(), **kwargs).load()
    _ENGINES[key] = (fp, engine)
    return engine


def retrieve_chunks(query: str, top_k: int = TOP_K, topic: Optional[str] = None,
                    corpus_dir: Optional[Path | str] = None,
                    embedder: Optional[Embedder] = None,
                    min_score: Optional[float] = None) -> List[Dict[str, Any]]:
    """Convenience wrapper returning Tool-16-shaped evidence dicts."""
    engine = get_engine(corpus_dir, embedder)
    return [h.to_evidence_dict()
            for h in engine.search(query, top_k=top_k, topic=topic, min_score=min_score)]
