import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from app.config import settings
from app.db import (
    append_audit_event,
    get_recent_audit,
    init_db,
    list_pending_actions,
    upsert_incident_and_action,
)
from app.llm_agent import run_triage_agent
from app.remediate import execute_approved_action

app = FastAPI(title="K8s Alert Triage Agent", version="0.2.0")
init_db()


class AlertWebhookPayload(BaseModel):
    alert_name: str = "KubePodCrashLooping"
    namespace: str = "default"
    pod: str
    deployment: str | None = None
    summary: str = ""


class AlertmanagerAlert(BaseModel):
    status: str = "firing"
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}


class AlertmanagerPayload(BaseModel):
    alerts: list[AlertmanagerAlert] = []


class ApprovalDecision(BaseModel):
    approved: bool = True


async def _notify_chat(report_dict: dict, pending: dict | None) -> None:
    if not settings.webhook_notify_url:
        return
    msg = (
        f"*Triage Alert:* `{report_dict['alert_name']}` on `{report_dict['namespace']}/{report_dict['pod']}`\n"
        f"*Root Cause:* {report_dict['root_cause']}\n"
        f"*Action:* {report_dict['recommended_action']}"
    )
    if pending:
        msg += f"\n*Pending Approval ID:* `{pending['action_id']}` (`POST /actions/{pending['action_id']}/decide`)"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(settings.webhook_notify_url, json={"text": msg, "content": msg})
    except Exception:
        pass


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/alert")
async def receive_alert(payload: AlertWebhookPayload) -> dict:
    report = await run_triage_agent(
        alert_name=payload.alert_name,
        namespace=payload.namespace,
        pod=payload.pod,
        summary=payload.summary,
    )
    report_dict = report.model_dump()
    target_deploy = payload.deployment or report.target_deployment
    pending = upsert_incident_and_action(
        alert_name=payload.alert_name,
        namespace=payload.namespace,
        pod=payload.pod,
        deployment=payload.deployment,
        summary=payload.summary,
        triage_dict=report_dict,
        action_type=report.action_type,
        target_deployment=target_deploy,
    )
    append_audit_event({"event": "triage_completed", "report": report_dict, "pending": pending})
    await _notify_chat(report_dict, pending)
    return {
        "triage_report": report_dict,
        "pending_remediation": pending,
    }


@app.post("/webhook/alertmanager")
async def receive_alertmanager(payload: AlertmanagerPayload) -> dict:
    results = []
    for alert in payload.alerts:
        if alert.status != "firing":
            continue
        labels = alert.labels
        annotations = alert.annotations
        pod = labels.get("pod", "")
        if not pod:
            continue
        single = AlertWebhookPayload(
            alert_name=labels.get("alertname", "KubePodCrashLooping"),
            namespace=labels.get("namespace", "default"),
            pod=pod,
            deployment=labels.get("deployment"),
            summary=annotations.get("summary") or annotations.get("description") or "",
        )
        results.append(await receive_alert(single))
    return {"processed": len(results), "results": results}


@app.get("/actions/pending")
def get_pending() -> dict:
    return {"pending": list_pending_actions()}


@app.post("/actions/{action_id}/decide")
def decide_action(action_id: str, decision: ApprovalDecision) -> dict:
    return execute_approved_action(action_id=action_id, approved=decision.approved)


@app.get("/audit")
def get_audit_log() -> dict:
    return {"audit": get_recent_audit()}