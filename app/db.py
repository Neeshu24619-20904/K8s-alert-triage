import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "triage.db"
AUDIT_JSONL_PATH = DATA_DIR / "audit.jsonl"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                fingerprint TEXT PRIMARY KEY,
                alert_name TEXT NOT NULL,
                namespace TEXT NOT NULL,
                pod TEXT NOT NULL,
                deployment TEXT,
                summary TEXT,
                triage_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remediation_actions (
                action_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                namespace TEXT NOT NULL,
                deployment TEXT NOT NULL,
                action_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )


def compute_fingerprint(alert_name: str, namespace: str, pod: str, deployment: str | None) -> str:
    raw = f"{alert_name}|{namespace}|{deployment or pod}".lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def append_audit_event(event: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_JSONL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def get_recent_audit(limit: int = 100) -> list[dict]:
    if not AUDIT_JSONL_PATH.exists():
        return []
    lines = AUDIT_JSONL_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def upsert_incident_and_action(
    alert_name: str,
    namespace: str,
    pod: str,
    deployment: str | None,
    summary: str,
    triage_dict: dict,
    action_type: str,
    target_deployment: str | None,
) -> dict | None:
    fp = compute_fingerprint(alert_name, namespace, pod, deployment or target_deployment)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO incidents (fingerprint, alert_name, namespace, pod, deployment, summary, triage_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                pod=excluded.pod,
                summary=excluded.summary,
                triage_json=excluded.triage_json,
                updated_at=excluded.updated_at
            """,
            (fp, alert_name, namespace, pod, deployment or target_deployment, summary, json.dumps(triage_dict), now),
        )

        if action_type != "restart_deployment" or not target_deployment:
            return None

        existing = conn.execute(
            """
            SELECT * FROM remediation_actions
            WHERE fingerprint = ? AND status = 'pending_approval'
            ORDER BY created_at DESC LIMIT 1
            """,
            (fp,),
        ).fetchone()
        if existing:
            return dict(existing)

        action_id = str(uuid4())[:8]
        record = {
            "action_id": action_id,
            "fingerprint": fp,
            "status": "pending_approval",
            "namespace": namespace,
            "deployment": target_deployment,
            "action_type": action_type,
            "reason": triage_dict.get("root_cause", "Automated triage recommendation"),
            "detail": None,
            "created_at": now,
            "decided_at": None,
        }
        conn.execute(
            """
            INSERT INTO remediation_actions
            (action_id, fingerprint, status, namespace, deployment, action_type, reason, detail, created_at, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["action_id"],
                record["fingerprint"],
                record["status"],
                record["namespace"],
                record["deployment"],
                record["action_type"],
                record["reason"],
                record["detail"],
                record["created_at"],
                record["decided_at"],
            ),
        )
        return record


def list_pending_actions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM remediation_actions WHERE status = 'pending_approval' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_action(action_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM remediation_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return dict(row) if row else None


def update_action_decision(action_id: str, status: str, detail: str, decided_at: str) -> dict | None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE remediation_actions
            SET status = ?, detail = ?, decided_at = ?
            WHERE action_id = ?
            """,
            (status, detail, decided_at, action_id),
        )
        row = conn.execute(
            "SELECT * FROM remediation_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return dict(row) if row else None