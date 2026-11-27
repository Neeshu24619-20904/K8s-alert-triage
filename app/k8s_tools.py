from kubernetes import client, config
from kubernetes.client.rest import ApiException
from app.config import settings


def _load_core_and_apps() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(context=settings.kube_context)
    return client.CoreV1Api(), client.AppsV1Api()

def get_pod_diagnostics(namespace: str, pod_name: str) -> dict:
    """Read-only diagnostics: pod status, container states, events, and tail logs."""
    core_v1, _ = _load_core_and_apps()
    result: dict = {
        "namespace": namespace,
        "pod": pod_name,
        "containers": [],
        "events": [],
        "current_logs": "",
        "previous_logs": "",
    }

    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        statuses = pod.status.container_statuses or []
        for cs in statuses:
            waiting = cs.state.waiting.reason if cs.state and cs.state.waiting else None
            last_term = (
                cs.last_state.terminated.reason
                if cs.last_state and cs.last_state.terminated
                else None
            )
            last_code = (
                cs.last_state.terminated.exit_code
                if cs.last_state and cs.last_state.terminated
                else None
            )
            result["containers"].append(
                {
                    "name": cs.name,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count,
                    "waiting_reason": waiting,
                    "last_terminated_reason": last_term,
                    "last_exit_code": last_code,
                }
            )
    except ApiException as exc:
        result["pod_error"] = f"HTTP {exc.status}: {exc.reason}"
        return result

    try:
        events = core_v1.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
        result["events"] = [
            {
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "count": ev.count,
            }
            for ev in (events.items or [])[-10:]
        ]
    except ApiException as exc:
        result["events_error"] = f"HTTP {exc.status}: {exc.reason}"

    try:
        result["current_logs"] = core_v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, tail_lines=80
        )
    except ApiException:
        result["current_logs"] = "<no active container logs>"

    try:
        result["previous_logs"] = core_v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, previous=True, tail_lines=80
        )
    except ApiException:
        result["previous_logs"] = "<no previous container logs>"

    return result


def list_pods_in_namespace(namespace: str) -> list[dict]:
    """Read-only list of pods and phases in a namespace."""
    core_v1, _ = _load_core_and_apps()
    try:
        pods = core_v1.list_namespaced_pod(namespace=namespace)
        return [
            {
                "name": p.metadata.name,
                "phase": p.status.phase,
                "restarts": sum(
                    c.restart_count for c in (p.status.container_statuses or [])
                ),
            }
            for p in (pods.items or [])
        ]
    except ApiException as exc:
        return [{"error": f"HTTP {exc.status}: {exc.reason}"}]