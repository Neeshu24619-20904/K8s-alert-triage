from fastapi import FastAPI
from pydantic import BaseModel
from app.llm_agent import run_triage_agent
from app.remediate import AUDIT_LOG, PENDING_ACTIONS, execute_approved_action, queue_remediation

app = FastAPI(title="K8s Alert Triage Agent", version="0.1.0")


class AlertWebhookPayload(BaseModel):
    alert_name: str = "KubePodCrashLooping"
    namespace: str = "default"
    pod: str
    deployment: str | None = None
    summary: str = ""


class ApprovalDecision(BaseModel):
    approved: bool = True


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

    target_deploy = payload.deployment or report.target_deployment
    pending = None
    if report.action_type == "restart_deployment" and target_deploy:
        pending = queue_remediation(
            namespace=payload.namespace,
            deployment=target_deploy,
            action_type=report.action_type,
            reason=report.root_cause,
        )

    return {
        "triage_report": report.model_dump(),
        "pending_remediation": pending,
    }


@app.get("/actions/pending")
def get_pending_actions() -> dict:
    return {"pending": list(PENDING_ACTIONS.values())}


@app.post("/actions/{action_id}/decide")
def decide_action(action_id: str, decision: ApprovalDecision) -> dict:
    return execute_approved_action(action_id=action_id, approved=decision.approved)


@app.get("/audit")
def get_audit_log() -> dict:
    return {"audit": AUDIT_LOG}