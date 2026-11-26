import json
import httpx
from pydantic import BaseModel
from app.config import settings
from app.k8s_tools import get_pod_diagnostics, list_pods_in_namespace
from app.runbook_tool import search_runbooks


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
    if name == "get_pod_diagnostics":
        out = get_pod_diagnostics(args["namespace"], args["pod_name"])
    elif name == "list_pods_in_namespace":
        out = list_pods_in_namespace(args["namespace"])
    elif name == "search_runbooks":
        out = search_runbooks(args["query"])
    else:
        out = {"error": f"Unknown tool: {name}"}
    return json.dumps(out, default=str)


async def _chat_completion(messages: list[dict]) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
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
            data = resp.json()
            return data["choices"][0]["message"]

        # Default: Ollama OpenAI-compatible chat endpoint
        resp = await client.post(
            f"{settings.ollama_base_url.rstrip('/')}/v1/chat/completions",
            json={
                "model": settings.ollama_model,
                "messages": messages,
                "tools": TOOLS_SCHEMA,
                "temperature": 0.1,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]


async def run_triage_agent(alert_name: str, namespace: str, pod: str, summary: str) -> TriageReport:
    system_prompt = (
        "You are an on-call Kubernetes triage agent. Call diagnostic and runbook tools "
        "before concluding. Never invent logs or events. When finished investigating, "
        "return ONLY a valid JSON object matching keys: alert_name, namespace, pod, "
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
        msg = await _chat_completion(messages)
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            for tc in tool_calls:
                fn = tc["function"]
                raw_args = fn.get("arguments") or "{}"
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                tool_output = _execute_tool(fn["name"], parsed_args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_output,
                    }
                )
            continue

        content = (msg.get("content") or "").strip()
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            parsed = json.loads(content[start : end + 1])
            return TriageReport(**parsed)

    # Fallback deterministic synthesis if model omits JSON wrapper
    diag = get_pod_diagnostics(namespace, pod)
    rb = search_runbooks(f"{alert_name} {summary}")
    return TriageReport(
        alert_name=alert_name,
        namespace=namespace,
        pod=pod,
        root_cause=f"Automated fallback inspection for {alert_name} on {pod}.",
        evidence=[str(ev.get("message", "")) for ev in diag.get("events", [])[:3]],
        runbook_used=rb[0]["file"] if rb else None,
        recommended_action="Verify container logs and rollout restart deployment if approved.",
        target_deployment=None,
        action_type="restart_deployment",
    )