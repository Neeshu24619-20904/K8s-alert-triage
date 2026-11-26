# Runbook: CrashLoopBackOff

## Common Causes
- Missing or invalid environment variables or secrets
- Bad entrypoint command or uncaught startup exception
- Failing liveness/readiness probes killing a slow-starting container

## Safe Remediation
- Inspect previous container logs (`previous=True`) and pod events
- If caused by a bad image/config rollout, roll back the Deployment to the previous revision
- If transient failure after config fix, restart the Deployment rollout