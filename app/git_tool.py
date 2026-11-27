import subprocess
from pathlib import Path


def get_recent_git_diff(repo_path: str = ".", commits: int = 2, max_chars: int = 2000) -> dict:
    """Read-only recent git log and diff summary for commit/config correlation."""
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return {"repo": str(path), "available": False, "note": "Not a git repository"}

    try:
        log_proc = subprocess.run(
            ["git", "-C", str(path), "log", f"-n{max(1, min(commits, 5))}", "--stat"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        diff_proc = subprocess.run(
            ["git", "-C", str(path), "diff", f"HEAD~{max(1, min(commits, 3))}..HEAD", "--", "*.yaml", "*.yml", "*.tf", "*.env", "*.py"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return {
            "repo": str(path),
            "available": True,
            "recent_commits": (log_proc.stdout or "")[:1200],
            "config_and_code_diff": (diff_proc.stdout or "<no diff in recent commits>")[:max_chars],
        }
    except Exception as exc:
        return {"repo": str(path), "available": False, "error": str(exc)}