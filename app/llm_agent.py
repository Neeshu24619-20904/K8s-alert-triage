import json
import httpx
from pydantic import BaseModel, ValidationError
from app.config import settings
from app.k8s_tools import get_pod_diagnostics, list_pods_in_namespace
from app.runbook_tool import search_runbooks

KNOWN_TOOLS = {
    "get_pod_diagnostics",
    "list_pods_in_namespace",
    "search_runbooks",
}


class TriageReport(BaseModel):
    alert_name: str
    namespace: str
    pod: str
    root_cause: str
    evidence: list[str]
    runbook_used: str | None = None
    recommended_action: str
    target_deployment: str | None = None
    action_type: str = "none"  # "restart_deployment" | "none"


TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_pod_diagnostics",
            "description": "Fetch pod status, events, current logs, and previous logs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod_name": {"type": "string"},
                },
                "required": ["namespace", "pod_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pods_in_namespace",
            "description": "List pods and restart counts in a Kubernetes namespace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                },
                "required": ["namespace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_runbooks",
            "description": "Search incident runbooks for matching failure modes and remediation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
]


def _execute_tool(name: str, args: dict) -> str:
    try:
        if name == "get_pod_diagnostics":
            out = get_pod_diagnostics(args["namespace"], args["pod_name"])
        elif name == "list_pods_in_namespace":
            out = list_pods_in_namespace(args["namespace"])
        elif name == "search_runbooks":
            out = search_runbooks(args["query"])
        else:
            out = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        out = {"error": f"Tool `{name}` failed: {exc}"}
    return json.dumps(out, default=str)


def _extract_json_objects(text: str) -> list[dict]:
    """Extract valid JSON dict objects from text without failing on extra data."""
    if not text:
        return []
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    idx = text.find("{")
    while idx != -1:
        try:
            obj, end_offset = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                objects.append(obj)
                idx += max(end_offset, 1)
                idx = text.find("{", idx)
                continue
        except json.JSONDecodeError:
            pass
        idx = text.find("{", idx + 1)
    return objects


def _parse_args(raw_args: object) -> dict:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def _chat_completion(messages: list[dict]) -> dict:
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if settings.llm_provider.lower() == "openai" and settings.openai_api_key:
            resp = await client.post(
                f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": settings.openai_model,
                    "messages": messages,
                    "tools": TOOLS_SCHEMA,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]

        base_url = settings.ollama_base_url.rstrip("/")
        resp = await client.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": settings.ollama_model,
                "messages": messages,
                "tools": TOOLS_SCHEMA,
                "temperature": 0.1,
            },
        )
        if resp.status_code == 404:
            native_resp = await client.post(
                f"{base_url}/api/chat",
                json={
                    "model": settings.ollama_model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.1},
                },
            )
            native_resp.raise_for_status()
            native_data = native_resp.json()
            return native_data.get("message", {"role": "assistant", "content": ""})

        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]


def _build_fallback_report(alert_name: str, namespace: str, pod: str, summary: str) -> TriageReport:
    diag = get_pod_diagnostics(namespace, pod)
    rb = search_runbooks(f"{alert_name} {summary}")
    evidence = [
        str(ev.get("message", ""))
        for ev in diag.get("events", [])[:3]
        if isinstance(ev, dict) and ev.get("message")
    ]
    for c in diag.get("containers", []):
        if isinstance(c, dict):
            reason = c.get("waiting_reason") or c.get("last_terminated_reason")
            code = c.get("last_exit_code")
            if reason or code is not None:
                evidence.append(
                    f"Container `{c.get('name')}`: state={reason}, exit_code={code}, restarts={c.get('restart_count')}"
                )
    if not evidence:
        evidence = [f"Inspected pod `{pod}` in namespace `{namespace}`."]

    return TriageReport(
        alert_name=alert_name,
        namespace=namespace,
        pod=pod,
        root_cause=f"Automated fallback inspection for {alert_name} on {pod}.",
        evidence=evidence,
        runbook_used=rb[0]["file"] if rb else None,
        recommended_action="Verify container logs and rollout restart deployment if approved.",
        target_deployment=None,
        action_type="restart_deployment",
    )


async def run_triage_agent(alert_name: str, namespace: str, pod: str, summary: str) -> TriageReport:
    system_prompt = (
        "You are an on-call Kubernetes triage agent. Call diagnostic and runbook tools "
        "before concluding. Never invent logs or events. When finished investigating, "
        "return ONLY a single valid JSON object matching keys: alert_name, namespace, pod, "
        "root_cause, evidence (list of strings), runbook_used (string or null), "
        "recommended_action (string), target_deployment (string or null), "
        "action_type ('restart_deployment' or 'none')."
    )
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Alert: {alert_name}\nNamespace: {namespace}\nPod: {pod}\n"
                f"Summary: {summary}\nInvestigate pod diagnostics and runbooks now."
            ),
        },
    ]

    for _ in range(4):
        try:
            msg = await _chat_completion(messages)
        except (httpx.ReadTimeout, httpx.HTTPError):
            break

        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        json_objs = _extract_json_objects(content)

        # Handle structured tool_calls first
        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            for idx, tc in enumerate(tool_calls):
                fn = tc.get("function") or {}
                tool_name = fn.get("name", "")
                parsed_args = _parse_args(fn.get("arguments"))
                tool_output = _execute_tool(tool_name, parsed_args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id") or f"call_{idx}",
                        "content": tool_output,
                    }
                )
            continue

        # Handle text-encoded JSON tool calls if model emitted them in content
        text_tool_calls = [
            obj for obj in json_objs if obj.get("name") in KNOWN_TOOLS
        ]
        if text_tool_calls:
            messages.append({"role": "assistant", "content": content})
            results_text = []
            for tc_obj in text_tool_calls:
                tool_name = tc_obj["name"]
                args = _parse_args(tc_obj.get("arguments") or tc_obj.get("parameters"))
                tool_output = _execute_tool(tool_name, args)
                results_text.append(f"Tool `{tool_name}` output:\n{tool_output}")
            messages.append(
                {
                    "role": "user",
                    "content": "\n\n".join(results_text)
                    + "\n\nNow return ONLY a single valid TriageReport JSON object.",
                }
            )
            continue

        # Look for a TriageReport JSON object among parsed JSON blocks
        for obj in reversed(json_objs):
            if "root_cause" in obj:
                candidate = dict(obj)
                candidate.setdefault("alert_name", alert_name)
                candidate.setdefault("namespace", namespace)
                candidate.setdefault("pod", pod)
                if not isinstance(candidate.get("evidence"), list):
                    ev = candidate.get("evidence")
                    candidate["evidence"] = [str(ev)] if ev else []
                candidate.setdefault(
                    "recommended_action",
                    "Inspect container logs and rollout restart deployment if approved.",
                )
                if candidate.get("action_type") not in {"restart_deployment", "none"}:
                    candidate["action_type"] = "none"
                try:
                    return TriageReport(**candidate)
                except (ValidationError, TypeError, ValueError):
                    continue

    return _build_fallback_report(alert_name, namespace, pod, summary)