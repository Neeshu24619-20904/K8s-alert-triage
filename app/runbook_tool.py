from pathlib import Path

RUNBOOKS_DIR = Path(__file__).resolve().parent.parent / "runbooks"


def search_runbooks(query: str) -> list[dict]:
    """Keyword-scored search over local Markdown runbooks."""
    if not RUNBOOKS_DIR.exists():
        return []

    terms = {w.lower() for w in query.replace("-", " ").split() if len(w) > 2}
    matches: list[dict] = []

    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        lower = content.lower()
        score = sum(1 for t in terms if t in lower)
        if score > 0:
            matches.append(
                {
                    "file": path.name,
                    "score": score,
                    "excerpt": content[:1500],
                }
            )

    matches.sort(key=lambda item: item["score"], reverse=True)
    return matches[:3]