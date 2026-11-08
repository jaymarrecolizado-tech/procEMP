"""Step 7: as-of retrieval + citation verification.

Every retrieved passage is filtered by the CORPUS SNAPSHOT PINNED to the
transaction, the legal regime, and the rule's effective dates as of the
transaction's posting date. "Latest" is never the default — a 2023 decision
must be reproducible against the rules in force in 2023.

Pure-stdlib BM25 + exact section-number boost. Dense retrieval is an optional
pluggable backend (no embedding model is bundled): StubEmbedding (hashing,
wiring tests) or TfidfEmbedding (deterministic TF-IDF prototype, intermediate
step; swap for bge-m3/multilingual-e5 served locally in production). A citation
is only `verified=true` when the chunk exists in the pinned snapshot, is
verified, is
not superseded, is in force on the posting date, and its passage hash is intact.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from core import (
    CoreError, append_event, canonical, get_tx, now, sha256_text, uid,
)

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "is", "are",
    "shall", "be", "by", "with", "as", "that", "this", "it", "on", "any",
    "from", "which", "sec", "section", "no", "irr", "ra", "or", "if",
}


def tokenize(text):
    tokens = re.findall(r"[a-z0-9]+(?:\.[0-9]+)*", str(text).lower())
    return [t for t in tokens if len(t) > 1 and t not in STOPWORDS]


def _section_numbers(text):
    """Capture both dotted sections (59.1, 50.3.1) and integer ones (Sec. 115)."""
    text = str(text)
    found = set(re.findall(r"\b\d+(?:\.\d+)+\b", text))
    for match in re.findall(r"\bsec\.?\s*(\d+(?:\.\d+)*)\b", text, flags=re.I):
        found.add(match)
    return found


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class EmbeddingBackend:
    def embed(self, text):
        raise NotImplementedError


class StubEmbedding(EmbeddingBackend):
    """Deterministic hashing vector for WIRING TESTS ONLY — not semantic.
    Swap for bge-m3 / multilingual-e5 served locally in production."""
    def __init__(self, dim=128):
        self.dim = dim

    def embed(self, text):
        # Stable across processes. Python's built-in hash() is randomized per
        # interpreter, which would make dense scores non-reproducible at audit.
        vec = [0.0] * self.dim
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vec[int.from_bytes(digest[:8], "big") % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class TfidfEmbedding(EmbeddingBackend):
    """Deterministic TF-IDF prototype — an intermediate step, NOT a neural
    embedding. Better than the hashing stub for lexical variants (idf-weighted),
    but still lexical/statistics only; swap for bge-m3/multilingual-e5 served
    locally in production. Requires ``fit()`` on the corpus texts before use;
    without fit, idf defaults to 1.0 (pure TF hashing). Deterministic: no
    randomized hashing; vectors are L2-normalized for cosine comparison."""

    def __init__(self, dim=128):
        self.dim = dim
        self.idf = {}
        self.fitted = False

    def fit(self, texts):
        texts = texts or []
        counts = {}
        n = len(texts)
        for text in texts:
            for token in set(tokenize(text)):
                counts[token] = counts.get(token, 0) + 1
        self.idf = {
            token: (math.log((n + 1) / (df + 1))) + 1.0
            for token, df in counts.items()
        }
        self.fitted = n > 0
        return self

    def embed(self, text):
        vec = [0.0] * self.dim
        weight_by_idx = {}
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:8], "big") % self.dim
            weight_by_idx[idx] = weight_by_idx.get(idx, 0.0) + self.idf.get(token, 1.0)
        for idx, weight in weight_by_idx.items():
            vec[idx] = weight
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _bm25(query_tokens, doc_token_lists, k1=1.5, b=0.75):
    n = len(doc_token_lists)
    if n == 0:
        return []
    lengths = [len(d) for d in doc_token_lists]
    avgdl = sum(lengths) / n or 1.0
    df = Counter()
    for d in doc_token_lists:
        for term in set(d):
            df[term] += 1
    scores = []
    for d in doc_token_lists:
        tf = Counter(d)
        score = 0.0
        for term in set(query_tokens):
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf[term] * (k1 + 1)) / (
                tf[term] + k1 * (1 - b + b * len(d) / avgdl))
        scores.append(score)
    return scores


class AsOfRAG:
    def __init__(self, conn, embedding_backend=None):
        self.conn = conn
        self.embedding = embedding_backend

    # ------------------------------------------------------------ candidates
    def _candidates(self, tx_id):
        tx = get_tx(self.conn, tx_id)
        as_of = tx["posting_date"]
        rows = self.conn.execute(
            "SELECT * FROM corpus_chunk WHERE corpus_snapshot_id=? AND regime=? "
            "AND verified=1 AND effective_from<=? "
            "AND (effective_to IS NULL OR ? < effective_to)",
            (tx["corpus_snapshot_id"], tx["regime"], as_of, as_of),
        ).fetchall()
        superseded = {r["supersedes"] for r in rows if r["supersedes"]}
        return [r for r in rows if r["chunk_id"] not in superseded]

    # ------------------------------------------------------------ retrieval
    def retrieve(self, query, tx_id, top_k=5):
        rows = self._candidates(tx_id)
        if not rows:
            return []
        q_tokens = tokenize(query)
        texts = [r["text"] for r in rows]
        doc_tokens = [tokenize(t) for t in texts]
        lexical = _bm25(q_tokens, doc_tokens)
        sections = _section_numbers(query)
        dense = None
        if self.embedding:
            q_vec = self.embedding.embed(query)
            dense = [_cosine(q_vec, self.embedding.embed(t)) for t in texts]
        max_lex = max(lexical) or 1.0
        scored = []
        for i, r in enumerate(rows):
            score = 0.7 * (lexical[i] / max_lex)
            if dense is not None:
                score += 0.3 * dense[i]
            if r["section"] in sections:
                score += 10.0
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [{
            "chunk_id": r["chunk_id"], "section": r["section"], "law": r["law"],
            "issuance_no": r["issuance_no"], "text": r["text"],
            "passage_hash": r["passage_hash"],
            "corpus_snapshot_id": r["corpus_snapshot_id"],
            "effective_from": r["effective_from"], "effective_to": r["effective_to"],
            "source_url": r["source_url"], "score": round(score, 4),
        } for score, r in scored[:top_k]]

    # ------------------------------------------------------------ citations
    def resolve_citation(self, section_ref, tx_id):
        """Return a verifiable citation dict, or verified=False -> checker abstains."""
        rows = self._candidates(tx_id)
        numbers = _section_numbers(section_ref)
        chosen = None
        reason = None
        if numbers:
            # An explicit section reference must resolve to THAT section.
            # Never satisfy it with a fuzzy match to a different rule.
            for r in rows:
                if r["section"] in numbers:
                    chosen = r
                    break
            if chosen is None:
                reason = ("cited section is not present in the pinned corpus snapshot; "
                          "fuzzy fallback is refused for an explicit section citation")
        else:
            hits = self.retrieve(section_ref, tx_id, top_k=1)
            content_words = [t for t in tokenize(section_ref) if t not in STOPWORDS]
            if hits and any(w in tokenize(hits[0]["text"]) for w in content_words):
                chosen = self.conn.execute(
                    "SELECT * FROM corpus_chunk WHERE chunk_id=?", (hits[0]["chunk_id"],)
                ).fetchone()
            if chosen is None:
                reason = "no verified in-force passage supports this assertion"
        if chosen is None:
            append_event(self.conn, tx_id, "CITATION_MISMATCH", "rag", "RAG", {
                "section_ref": section_ref, "reason": reason,
            })
            return {"section": section_ref, "verified": False, "reason": reason}
        return {
            "section": section_ref, "chunk_id": chosen["chunk_id"],
            "matched_section": chosen["section"], "law": chosen["law"],
            "issuance_no": chosen["issuance_no"],
            "passage_hash": chosen["passage_hash"],
            "corpus_snapshot_id": chosen["corpus_snapshot_id"],
            "source_url": chosen["source_url"],
            "effective_from": chosen["effective_from"],
            "verified": True,
        }

    def verify(self, citation, tx_id):
        """Re-check a citation later (audit / COA verification path)."""
        tx = get_tx(self.conn, tx_id)
        chunk_id = citation.get("chunk_id")
        if not chunk_id:
            return False, "citation has no chunk_id"
        row = self.conn.execute(
            "SELECT * FROM corpus_chunk WHERE chunk_id=?", (chunk_id,)
        ).fetchone()
        if not row:
            return False, "chunk no longer exists"
        if row["corpus_snapshot_id"] != tx["corpus_snapshot_id"]:
            return False, "chunk is not in the transaction's pinned corpus snapshot"
        if row["regime"] != tx["regime"]:
            return False, "chunk regime does not match the transaction"
        if not row["verified"]:
            return False, "chunk is not verified"
        if row["effective_from"] > tx["posting_date"]:
            return False, "rule was not yet in force on the posting date"
        if row["effective_to"] and tx["posting_date"] >= row["effective_to"]:
            return False, "rule had expired on the posting date"
        if row["passage_hash"] != sha256_text(row["text"]):
            return False, "passage text was altered after indexing"
        if citation.get("passage_hash") and citation["passage_hash"] != row["passage_hash"]:
            return False, "citation passage_hash does not match the corpus"
        siblings = self.conn.execute(
            "SELECT 1 FROM corpus_chunk WHERE supersedes=? AND verified=1 LIMIT 1",
            (chunk_id,),
        ).fetchone()
        if siblings:
            return False, "chunk has been superseded"
        return True, "verified"

    # ------------------------------------------------------------ corpus admin
    def build_snapshot(self, snapshot_id, version, regime, chunks, approver):
        """Ingest a new GPPB issuance. Requires a named human approver."""
        if not approver:
            raise CoreError("a named human approver is required for every corpus snapshot")
        self.conn.execute(
            "INSERT OR REPLACE INTO corpus_snapshot "
            "(id, version, regime, created_at, checksum, approved_by, is_active) "
            "VALUES (?,?,?,?,?,?,0)",
            (snapshot_id, version, regime, now(), "PENDING", approver),
        )
        for c in chunks:
            self.conn.execute(
                "INSERT OR REPLACE INTO corpus_chunk "
                "(chunk_id, corpus_snapshot_id, law, issuance_no, section, regime, "
                " effective_from, effective_to, supersedes, source_url, text, "
                " passage_hash, verified) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["chunk_id"], snapshot_id, c["law"], c["issuance_no"], c["section"],
                 regime, c["effective_from"], c.get("effective_to"), c.get("supersedes"),
                 c["source_url"], c["text"], sha256_text(c["text"]),
                 int(c.get("verified", False))),
            )
        checksum = sha256_text(canonical([c["chunk_id"] for c in chunks]))
        self.conn.execute("UPDATE corpus_snapshot SET checksum=? WHERE id=?",
                          (checksum, snapshot_id))
        return append_event(self.conn, "SYSTEM", "CORPUS_SNAPSHOT_BUILT", approver,
                            "LEGAL_OFFICER", {"snapshot_id": snapshot_id,
                                              "version": version, "regime": regime,
                                              "chunk_count": len(chunks)})

    def activate_snapshot(self, snapshot_id, approver):
        """Human gate. Existing transactions keep their pinned snapshot."""
        if not approver:
            raise CoreError("a named human approver is required to activate a corpus")
        row = self.conn.execute("SELECT * FROM corpus_snapshot WHERE id=?",
                                (snapshot_id,)).fetchone()
        if not row:
            raise CoreError(f"unknown corpus snapshot {snapshot_id}")
        self.conn.execute("UPDATE corpus_snapshot SET is_active=0 WHERE regime=?",
                          (row["regime"],))
        self.conn.execute("UPDATE corpus_snapshot SET is_active=1, approved_by=? WHERE id=?",
                          (approver, snapshot_id))
        return append_event(self.conn, "SYSTEM", "CORPUS_SNAPSHOT_ACTIVATED", approver,
                            "LEGAL_OFFICER", {"snapshot_id": snapshot_id,
                                              "version": row["version"],
                                              "regime": row["regime"],
                                              "note": "existing transactions remain pinned"})


def verify_corpus_integrity(conn):
    """Recompute every passage hash — usable by COA without trusting the app."""
    problems = []
    for row in conn.execute("SELECT chunk_id, text, passage_hash FROM corpus_chunk"):
        if sha256_text(row["text"]) != row["passage_hash"]:
            problems.append(row["chunk_id"])
    return {"ok": not problems, "altered_chunks": problems}
