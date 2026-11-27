from pathlib import Path
from fastapi.testclient import TestClient
import pytest

import app.db as db_mod
import app.git_tool as git_mod
import app.llm_agent as agent_mod
import app.main as main_mod
import app.rag_store as rag_mod
import app.remediate as rem_mod


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    runbooks_dir = tmp_path / "runbooks"
    runbooks_dir.mkdir(parents=True, exist_ok=True)

    (runbooks_dir / "crashloopbackoff.md").write_text(
        "# Runbook: CrashLoopBackOff\n\n## Safe Remediation\nCheck logs and roll back or restart deployment.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(db_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(db_mod, "DB_PATH", data_dir / "triage.db")
    monkeypatch.setattr(db_mod, "AUDIT_JSONL_PATH", data_dir / "audit.jsonl")
    monkeypatch.setattr(rag_mod, "RUNBOOKS_DIR", runbooks_dir)

    # Avoid real network calls to Ollama embeddings or chat webhooks during unit tests
    monkeypatch.setattr(rag_mod, "_embed_texts", lambda texts, model="nomic-embed-text": None)

    async def fake_notify(report_dict: dict, pending: dict | None) -> None:
        return None

    monkeypatch.setattr(main_mod, "_notify_chat", fake_notify)

    # Ensure dry-run mode during tests so no real K8s API call is needed
    monkeypatch.setattr(rem_mod.settings, "dry_run_remediation", True)

    db_mod.init_db()
    return TestClient(main_mod.app)


def test_health_and_initial_state(isolated_env: TestClient):
    client = isolated_env
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

    assert client.get("/actions/pending").status_code == 200
    assert client.get("/actions/pending").json() == {"pending": []}
    assert client.get("/audit").status_code == 200
    assert client.get("/audit").json() == {"audit": []}


def test_webhook_alert_deduplication_and_approval(isolated_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    client = isolated_env
    call_count = {"n": 0}

    async def fake_chat_completion(messages: list[dict]) -> dict:
        call_count["n"] += 1
        # First LLM turn requests a tool call; second turn returns final TriageReport JSON
        if call_count["n"] == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_runbooks",
                            "arguments": '{"query": "CrashLoopBackOff"}',
                        },
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": (
                '{"alert_name": "KubePodCrashLooping", "namespace": "default", '
                '"pod": "demo-crasher-abc", "root_cause": "Missing DB_URL config", '
                '"evidence": ["exit code 1", "missing DB_URL config"], '
                '"runbook_used": "crashloopbackoff.md", '
                '"recommended_action": "Restart deployment after setting DB_URL", '
                '"target_deployment": "demo-crasher", "action_type": "restart_deployment"}'
            ),
        }

    monkeypatch.setattr(agent_mod, "_chat_completion", fake_chat_completion)

    payload = {
        "alert_name": "KubePodCrashLooping",
        "namespace": "default",
        "pod": "demo-crasher-abc",
        "deployment": "demo-crasher",
        "summary": "Pod crashing repeatedly",
    }

    r1 = client.post("/webhook/alert", json=payload)
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["triage_report"]["root_cause"] == "Missing DB_URL config"
    assert body1["pending_remediation"] is not None
    action_id = body1["pending_remediation"]["action_id"]
    assert body1["pending_remediation"]["status"] == "pending_approval"

    # Second alert for same deployment/alert should reuse the existing pending action_id
    call_count["n"] = 1
    r2 = client.post("/webhook/alert", json=payload)
    assert r2.status_code == 200
    assert r2.json()["pending_remediation"]["action_id"] == action_id

    # Approve action
    dec = client.post(f"/actions/{action_id}/decide", json={"approved": True})
    assert dec.status_code == 200
    assert dec.json()["status"] == "executed_dry_run"

    # Approving again should return an error rather than re-executing
    dec_again = client.post(f"/actions/{action_id}/decide", json={"approved": True})
    assert dec_again.status_code == 200
    assert "error" in dec_again.json()

    # Audit log should have triage + approval events
    audit = client.get("/audit").json()["audit"]
    assert len(audit) >= 3


def test_alertmanager_webhook_skips_resolved(isolated_env: TestClient, monkeypatch: pytest.MonkeyPatch):
    client = isolated_env

    async def fake_chat_completion(messages: list[dict]) -> dict:
        return {
            "role": "assistant",
            "content": (
                '{"alert_name": "KubePodCrashLooping", "namespace": "default", '
                '"pod": "api-pod-1", "root_cause": "Startup error", '
                '"evidence": ["Back-off restarting failed container"], '
                '"runbook_used": "crashloopbackoff.md", '
                '"recommended_action": "Inspect logs", '
                '"target_deployment": "api-deploy", "action_type": "none"}'
            ),
        }

    monkeypatch.setattr(agent_mod, "_chat_completion", fake_chat_completion)

    am_payload = {
        "alerts": [
            {
                "status": "resolved",
                "labels": {"alertname": "KubePodCrashLooping", "namespace": "default", "pod": "old-pod"},
                "annotations": {"summary": "Resolved alert"},
            },
            {
                "status": "firing",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "namespace": "default",
                    "pod": "api-pod-1",
                    "deployment": "api-deploy",
                },
                "annotations": {"summary": "Container restarting"},
            },
        ]
    }

    res = client.post("/webhook/alertmanager", json=am_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["processed"] == 1
    assert data["results"][0]["triage_report"]["pod"] == "api-pod-1"


def test_runbook_search_and_git_diff_tools(isolated_env: TestClient, tmp_path: Path):
    matches = rag_mod.search_runbooks("CrashLoopBackOff rollback")
    assert len(matches) >= 1
    assert matches[0]["file"] == "crashloopbackoff.md"

    non_repo = git_mod.get_recent_git_diff(str(tmp_path))
    assert non_repo["available"] is False