# Autonomous Kubernetes & Alert Triage Agent

An event-driven on-call copilot for Kubernetes clusters. It ingests alert webhooks (standard JSON or Prometheus Alertmanager payloads), inspects failing workloads using read-only Kubernetes, Git, and Markdown runbook RAG tools, synthesizes a structured root-cause brief, and queues state-mutating remediations behind a human-in-the-loop approval gate with persistent SQLite and JSONL audit logging.

---

## Architecture & Safety Model

1. **Webhook Ingestion & Deduplication:** Accepts `POST /webhook/alert` and `POST /webhook/alertmanager` (filtering `firing` alerts). Computes a SHA-256 incident fingerprint from `(alert_name, namespace, deployment/pod)` to suppress duplicate pending actions during alert storms.
2. **Bounded Tool-Calling Agent Loop:** Drives an LLM (local Ollama or OpenAI-compatible API) through read-only forensic tools (`get_pod_diagnostics`, `list_pods_in_namespace`, `search_runbooks`, `get_recent_git_diff`) with multi-JSON/text-tool-call recovery and a deterministic diagnostic fallback.
3. **Runbook Retrieval (RAG):** Chunks Markdown runbooks under `runbooks/` by heading and ranks sections via Ollama embeddings (`/api/embed`) and cosine similarity, falling back cleanly to keyword overlap when offline.
4. **Human-Gated Remediation & Audit:** Proposes a `TriageReport` and stores any `restart_deployment` action as `pending_approval` in `data/triage.db` (SQLite) + `data/audit.jsonl`. Cluster mutations remain blocked until an operator approves via `POST /actions/{action_id}/decide` (with `DRY_RUN_REMEDIATION=true` enabled by default).

---

## Project Structure

```text
.
├── app/
│   ├── config.py          # Pydantic settings (.env loader)
│   ├── db.py              # SQLite persistence, incident fingerprinting & JSONL audit log
│   ├── git_tool.py        # Read-only recent git log & config/code diff inspection
│   ├── k8s_tools.py       # Read-only Kubernetes pod, event, and log diagnostics
│   ├── llm_agent.py       # Bounded LLM tool-calling loop, JSON recovery & fallback
│   ├── main.py            # FastAPI routes & optional Slack/Discord webhook notifier
│   ├── rag_store.py       # Markdown runbook chunking & vector/keyword retrieval
│   └── remediate.py       # Approval-gated dry-run / live rollout restart executor
├── deploy/k8s/
│   └── rbac-and-deployment.yaml  # Namespace, ServiceAccount, RBAC, PVC, Deployment & Service
├── runbooks/
│   ├── crashloopbackoff.md
│   └── oomkilled.md
├── tests/
│   ├── sample_alert.json
│   └── test_triage_agent.py
├── Dockerfile
└── requirements.txt
```

---

## Prerequisites

- **Python:** `3.14.4` (or `3.12+`)
- **Kubernetes:** Local cluster (`minikube` or `kind`) with `kubectl` configured
- **LLM Runtime:** Local [Ollama](https://ollama.com/) (`qwen2.5-coder:3b` or `qwen2.5-coder:7b`) or an OpenAI-compatible endpoint
- **Docker:** For building the container image and loading it into Minikube

---

## Quick Start (Local Development)

### 1. Create Virtual Environment & Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Create a `.env` file in the project root:
```bash
cat << 'EOF' > .env
KUBE_CONTEXT=minikube
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5-coder:3b
DRY_RUN_REMEDIATION=true
GIT_REPO_PATH=.
WEBHOOK_NOTIFY_URL=
EOF
```

### 3. Pull Ollama Model (Optional if already pulled)
```bash
ollama pull qwen2.5-coder:3b
# Optional embedding model for semantic runbook search:
ollama pull nomic-embed-text
```

### 4. Start the FastAPI Server
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Verify health in another terminal:
```bash
curl -s http://127.0.0.1:8080/health | jq .
```

---

## End-to-End Usage & Demo Commands

### 1. Spin Up a Failing Workload in Minikube
```bash
kubectl create deployment demo-crasher --image=busybox:1.36 -- /bin/sh -c "echo 'missing DB_URL config'; exit 1"
POD_NAME=$(kubectl get pods -l app=demo-crasher -o jsonpath='{.items[0].metadata.name}')
echo "Target pod: $POD_NAME"
```

### 2. Send an Alert Webhook
```bash
curl -s -X POST http://127.0.0.1:8080/webhook/alert \
  -H "Content-Type: application/json" \
  -d "{
    \"alert_name\": \"KubePodCrashLooping\",
    \"namespace\": \"default\",
    \"pod\": \"$POD_NAME\",
    \"deployment\": \"demo-crasher\",
    \"summary\": \"Pod restarting repeatedly in default namespace\"
  }" | jq .
```

*(You can also send native Alertmanager payloads to `POST /webhook/alertmanager`.)*

### 3. Inspect Pending Approvals & Decide
List pending actions:
```bash
curl -s http://127.0.0.1:8080/actions/pending | jq .
```

Approve an action (replace `<ACTION_ID>` with the `action_id` returned above):
```bash
curl -s -X POST http://127.0.0.1:8080/actions/<ACTION_ID>/decide \
  -H "Content-Type: application/json" \
  -d '{"approved": true}' | jq .
```

Or reject an action:
```bash
curl -s -X POST http://127.0.0.1:8080/actions/<ACTION_ID>/decide \
  -H "Content-Type: application/json" \
  -d '{"approved": false}' | jq .
```

### 4. View Audit Trail & Clean Up
```bash
curl -s http://127.0.0.1:8080/audit | jq .
kubectl delete deployment demo-crasher
```

---

## Running Automated Tests

Run the `pytest` suite (uses isolated temporary directories and mocked LLM/webhook calls):
```bash
PYTHONPATH=. pytest -v
```

---

## Deploying to Kubernetes (Minikube)

### 1. Build & Load the Docker Image
```bash
docker build -t k8s-triage-agent:0.2.0 .
minikube image load k8s-triage-agent:0.2.0
```

### 2. Apply RBAC, PVC, Deployment & Service Manifests
```bash
kubectl apply -f deploy/k8s/rbac-and-deployment.yaml
kubectl -n triage-system rollout status deployment/k8s-triage-agent --timeout=120s
kubectl -n triage-system get pods,svc,pvc
```

### 3. Port-Forward the In-Cluster Service
```bash
kubectl -n triage-system port-forward svc/k8s-triage-agent 8080:8080
```

> **Note on Live Remediation:** Set `DRY_RUN_REMEDIATION=false` in `.env` (or in `deploy/k8s/rbac-and-deployment.yaml`) when you want approved actions to patch `kubectl.kubernetes.io/restartedAt` on target Deployments in the cluster.