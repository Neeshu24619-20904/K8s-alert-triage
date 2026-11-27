from datetime import datetime, timezone
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from app.config import settings
from app.db import append_audit_event, get_action, update_action_decision


def execute_approved_action(action_id: str, approved: bool) -> dict:
    record = get_action(action_id)
    if not record:
        return {"error": f"Unknown action_id: {action_id}"}

    if record["status"] != "pending_approval":
        return {"error": f"Action {action_id} is already finalized with status '{record['status']}'"}

    now = datetime.now(timezone.utc).isoformat()

    if not approved:
        updated = update_action_decision(action_id, "rejected", "Rejected by operator", now)
        append_audit_event({"event": "action_rejected", "action": updated})
        return updated or {}

    if settings.dry_run_remediation:
        detail = f"[DRY_RUN] Would patch {record['namespace']}/{record['deployment']} restartedAt={now}"
        updated = update_action_decision(action_id, "executed_dry_run", detail, now)
        append_audit_event({"event": "action_executed_dry_run", "action": updated})
        return updated or {}

    try:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config(context=settings.kube_context)
        apps_v1 = client.AppsV1Api()
        patch_body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now
                        }
                    }
                }
            }
        }
        apps_v1.patch_namespaced_deployment(
            name=record["deployment"],
            namespace=record["namespace"],
            body=patch_body,
        )
        status = "executed"
        detail = f"Triggered rollout restart on {record['namespace']}/{record['deployment']}"
    except ApiException as exc:
        status = "failed"
        detail = f"Kubernetes API error {exc.status}: {exc.reason}"

    updated = update_action_decision(action_id, status, detail, now)
    append_audit_event({"event": "action_decided", "action": updated})
    return updated or {}