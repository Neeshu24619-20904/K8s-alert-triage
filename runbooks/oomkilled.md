# Runbook: OOMKilled (Exit Code 137)

## Common Causes
- Container memory usage exceeded `resources.limits.memory`
- Memory leak or unbounded batch/cache allocation under load

## Safe Remediation
- Check `lastState.terminated.reason` and current memory limit
- Increase memory limit in the Deployment/Helm values or roll back a memory-regressive release