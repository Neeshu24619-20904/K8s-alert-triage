import math
from pathlib import Path
import httpx
from app.config import settings

RUNBOOKS_DIR = Path(__file__).resolve().parent.parent / "runbooks"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _load_chunks() -> list[dict]:
    if not RUNBOOKS_DIR.exists():
        return []
    chunks: list[dict] = []
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        sections = raw.split("\n## ")
        for i, sec in enumerate(sections):
            text = sec if i == 0 else "## " + sec
            text = text.strip()
            if not text:
                continue
            heading = text.splitlines()[0].lstrip("#").strip()
            chunks.append(
                {
                    "file": path.name,
                    "section": heading,
                    "content": text[:1600],
                }
            )
    return chunks


def _embed_texts(texts: list[str], model: str = "nomic-embed-text") -> list[list[float]] | None:
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/embed",
                json={"model": model, "input": texts},
            )
            if resp.status_code != 200:
                return None
            embeddings = resp.json().get("embeddings")
            if isinstance(embeddings, list) and len(embeddings) == len(texts):
                return embeddings
    except Exception:
        return None
    return None


def search_runbooks(query: str, top_k: int = 3) -> list[dict]:
    """Semantic similarity search over runbooks with keyword fallback."""
    chunks = _load_chunks()
    if not chunks:
        return []

    docs = [f"{c['file']} - {c['section']}\n{c['content']}" for c in chunks]
    doc_vecs = _embed_texts(docs)
    query_vecs = _embed_texts([query]) if doc_vecs else None

    scored: list[tuple[float, dict]] = []
    if doc_vecs and query_vecs:
        q_vec = query_vecs[0]
        for chunk, d_vec in zip(chunks, doc_vecs):
            score = _cosine_similarity(q_vec, d_vec)
            scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
    else:
        terms = {w.lower() for w in query.replace("-", " ").replace("_", " ").split() if len(w) > 2}
        for chunk in chunks:
            hay = f"{chunk['file']} {chunk['section']} {chunk['content']}".lower()
            score = float(sum(1 for t in terms if t in hay))
            scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)

    results: list[dict] = []
    for score, chunk in scored[:top_k]:
        results.append(
            {
                "file": chunk["file"],
                "section": chunk["section"],
                "score": round(score, 4),
                "excerpt": chunk["content"],
            }
        )
    return results