"""Advisory embedding-backend comparison on PUBLIC legal text (prototype).

Compares the deterministic TF-IDF baseline against a locally served neural
backend (LM Studio / llama.cpp / vLLM, OpenAI-compatible ``/v1/embeddings`` on
loopback) for phrased and Taglish retrieval queries against the exact statute
sections in ``seed_corpus.json``. This is a decision-support aid only:

- The seed corpus contains public legal passages; nothing confidential is sent.
- nomic-embed-text is English-leaning. A production embedding decision
  (bge-m3 / multilingual-e5 served locally in a controlled environment) needs
  the approved benchmark set and human verification — see
  ``procurement-review/05-data-governance-training.md``.
- Citation verification never depends on this comparison.

Usage:
  python embedding_compare.py                       # default LM Studio URL/model
  python embedding_compare.py --url http://127.0.0.1:1234/v1/embeddings
  python embedding_compare.py --json
"""

from __future__ import annotations

import argparse
import json
import os

from rag import HTTPEmbedding, TfidfEmbedding, tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_CORPUS_PATH = os.path.join(HERE, "seed_corpus.json")

DEFAULT_URL = "http://127.0.0.1:1234/v1/embeddings"
DEFAULT_MODEL = "text-embedding-nomic-embed-text-v1.5"

# Illustrative phrased / Taglish variants of the seed passages. Not a benchmark,
# not a legal position — retrieval sanity checks only.
QUERIES = [
    {"query": "checklist of required documents in the invitation to bid", "section": "59.1"},
    {"query": "confidentiality of submitted bids", "section": "58.5"},
    {"query": "agrading ng mga bid at financial envelopes bago ang second opening (MEARB)", "section": "58.2"},
    {"query": "small value procurement threshold amount two million pesos", "section": "34.1"},
    {"query": "tatlong milyon o mas mababa ang posting requirement sa SVP", "section": "34.3"},
    {"query": "three-year transitory period RA 9184", "section": "113.1"},
    {"query": "repeal of RA 9184 and Commonwealth Act 138", "section": "115"},
    {"query": "three bidders invited at least one quotation sufficient", "section": "34.3"},
    {"query": "public opening of bids", "section": "58.1"},
    {"query": "posting of availability of funds", "section": "50.3.1"},
    {"query": "small value procurement posting days three", "section": "34.3"},
    {"query": "hindi dapat isira ng bidding ang lihim ng mga bid bago ang opening", "section": "58.5"},
    {"query": "Standard Forms under GPPB Resolution 03-2025", "section": "definition"},
    {"query": "Recommended Forms under GPPB Resolution 03-2025", "section": "definition"},
]


def load_chunks(path=SEED_CORPUS_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return [c for c in data.get("chunks", []) if c.get("verified")]


def rank_by_cosine(texts, matrix):
    return sorted(range(len(texts)), key=lambda i: matrix[i], reverse=True)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def evaluate(backend, texts, chunks, queries):
    rows = []
    for item in queries:
        qvec = backend.embed(item["query"])
        dvec = backend.embed_batch(texts) if hasattr(backend, "embed_batch") else [backend.embed(t) for t in texts]
        scores = [_cosine(qvec, v) for v in dvec]
        order = rank_by_cosine(texts, scores)
        rank = None
        for pos, idx in enumerate(order, start=1):
            if chunks[idx].get("section") == item["section"]:
                rank = pos
                break
        rows.append({"query": item["query"], "expected_section": item["section"],
                     "rank": rank})
    n = len(rows)
    found1 = sum(1 for r in rows if r["rank"] == 1)
    found3 = sum(1 for r in rows if r["rank"] is not None and r["rank"] <= 3)
    mrr = sum(1.0 / r["rank"] for r in rows if r["rank"]) / n if n else 0.0
    return {"rows": rows, "queries": n, "precision_at_1": found1 / n,
            "recall_at_3": found3 / n, "mrr": round(mrr, 4)}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Advisory comparison of TF-IDF vs a loopback neural embedding backend"
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    chunks = load_chunks()
    texts = [c["text"] for c in chunks]

    tfidf = TfidfEmbedding().fit(texts)
    neural = HTTPEmbedding(args.url, args.model, timeout=args.timeout)

    baseline = evaluate(tfidf, texts, chunks, QUERIES)
    candidate = evaluate(neural, texts, chunks, QUERIES)

    result = {
        "advisory_only": True,
        "not_a_benchmark": True,
        "purpose": "retrieval sanity comparison on public statute passages only",
        "chunks_evaluated": len(chunks),
        "tfidf": baseline,
        "neural": candidate,
        "model": args.model,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Seed passages (public text): {len(chunks)}")
        print(f"Queries: {baseline['queries']}")
        print(f"TF-IDF            P@1={baseline['precision_at_1']:.2f} R@3={baseline['recall_at_3']:.2f} MRR={baseline['mrr']:.4f}")
        print(f"Neural (LM Studio) P@1={candidate['precision_at_1']:.2f} R@3={candidate['recall_at_3']:.2f} MRR={candidate['mrr']:.4f}")
        for name, table in (("TF-IDF", baseline), ("Neural", candidate)):
            print(f"\n{name} ranks:")
            for row in table["rows"]:
                print(f"  rank={row['rank'] or '-':>2}  expected={row['expected_section']:10}  {row['query'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())