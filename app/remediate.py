from datetime import datetime, timezone
from uuid import uuid4
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from app.config import settings

PENDING_ACTIONS: dict[str, dict] = {}
AUDIT_LOG: list[dict] = []


def queue_remediation(namespace: str, deployment: str, action_type: str, reason: str) -> dict:
    action_id = str(uuid4())[:8]
    record = {
        "action_id": action_id,
        "status": "pending_approval",
        "namespace": namespace,
        "deployment": deployment,
        "action_type": action_type,
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    PENDING_ACTIONS[action_id] = record
    return record


def execute_approved_action(action_id: str, approved: bool) -> dict:
    if action_id not in PENDING_ACTIONS:
        return {"error": f"Unknown action_id: {action_id}"}

    record = PENDING_ACTIONS.pop(action_id)
    now = datetime.now(timezone.utc).isoformat()

    if not approved:
        record["status"] = "rejected"
        record["decided_at"] = now
        AUDIT_LOG.append(record)
        return record

    if settings.dry_run_remediation:
        record["status"] = "executed_dry_run"
        record["detail"] = (
            f"[DRY_RUN] Would patch {record['namespace']}/{record['deployment']} "
            f"restartedAt={now}"
        )
        record["decided_at"] = now
        AUDIT_LOG.append(record)
        return record

    try:
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
        record["status"] = "executed"
        record["detail"] = f"Triggered rollout restart on {record['namespace']}/{record['deployment']}"
    except ApiException as exc:
        record["status"] = "failed"
        record["detail"] = f"Kubernetes API error {exc.status}: {exc.reason}"

    record["decided_at"] = now
    AUDIT_LOG.append(record)
    return record