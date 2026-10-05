# Operations Runbooks

Step-by-step operational procedures for running the bot in production.

| Runbook | When to use |
|---------|-------------|
| [`GO_LIVE_CHECKLIST.md`](GO_LIVE_CHECKLIST.md) | Before enabling live trading for the first time |
| [`PRODUCTION_PREFLIGHT.md`](PRODUCTION_PREFLIGHT.md) | Understanding the preflight safety gate |
| [`BACKUP_RESTORE_RUNBOOK.md`](BACKUP_RESTORE_RUNBOOK.md) | Backing up or restoring data |
| [`MONITORING.md`](MONITORING.md) | Day-to-day health monitoring |
| [`HEALTHCHECKS.md`](HEALTHCHECKS.md) | Container and process health checks |
| [`INCIDENT_RUNBOOK.md`](INCIDENT_RUNBOOK.md) | Something is wrong — triage and recovery |
| [`ORDER_FAILURE_RUNBOOK.md`](ORDER_FAILURE_RUNBOOK.md) | An order failed or is in an unknown state |
| [`FIRST_LIVE_TRADE_TEMPLATE.md`](FIRST_LIVE_TRADE_TEMPLATE.md) | Validate the first real order |
| [`VALIDATION_RUNBOOK.md`](VALIDATION_RUNBOOK.md) | Validate a deployment or change |
| [`RELEASE_ACCEPTANCE.md`](RELEASE_ACCEPTANCE.md) | Accept a release into production |

See also: [`../operations.md`](../operations.md) (general operations guide) and [`../RELEASE.md`](../RELEASE.md) (staging → production release workflow).
