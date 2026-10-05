# First Live Trade Template

Use this template to record the first confirmed live Kraken purchase made by
DCA-Bot. Complete every field and store the signed record in a private operations
location outside the public repository.

---

## Release and environment

| Field | Value |
|-------|-------|
| Date and time (UTC) | |
| Release version | |
| Git commit SHA | |
| Container image reference | |
| Environment | production |
| Operator | |
| Reviewer | |

## Order parameters

| Field | Value |
|-------|-------|
| Kraken pair | |
| Configured amount | |
| Calculated volume | |
| Kraken minimum | |
| Fee buffer | |
| Available balance | |

## Pre-flight checks

| Field | Value |
|-------|-------|
| Preflight command output (optional) | `python scripts/preflight_production.py` |
| Overall result | PASS / WARN / FAIL |
| `can_place_live_orders` | true / false |
| `/health/ready` status | ready / not_ready |
| Bot status | running / waiting / paused |

## Backup

| Field | Value |
|-------|-------|
| Backup created before order | yes / no |
| Backup file name | |
| Backup checksum | |
| Restore dry-run completed | yes / no |

## Order execution

| Field | Value |
|-------|-------|
| Order initiation time | |
| Confirmation phrase used | `LIVE BUY <PAIR> <AMOUNT>` |
| Dashboard user | |
| Kraken order reference (txid) | |
| Final Kraken status | closed / open / rejected / unknown |
| Local transaction-store status | filled / simulated / error |

## Verification

| Field | Value |
|-------|-------|
| Dashboard shows new transaction | yes / no |
| Telegram notification received | yes / no |
| Order appears in Kraken interface | yes / no |
| Next cycle calculated correctly | yes / no |
| No duplicate order created | yes / no |
| Recurring trading remains disabled | yes / no |

## Retry / reconciliation

| Field | Value |
|-------|-------|
| Any retry required | yes / no |
| Reconciliation result | |
| HOLD state entered | yes / no |

## Logs

Attach or reference the relevant `logs/app.json` entries for the order window.

```text
(paste observed log lines)
```

## Rollback decision

- [ ] Order verified successfully; no rollback required.
- [ ] Issue observed; rollback or reconciliation initiated.
- [ ] Escalation required.

## Final acceptance

| Field | Value |
|-------|-------|
| First live trade accepted | yes / no |
| Conditions for enabling recurring trading documented | yes / no |
| Live-trade record completed | yes / no |

## Sign-off

| Role | Name | Date | Signature |
|------|------|------|-----------|
| Operator | | | |
| Reviewer | | | |
| Security / Release approver | | | |

---

## Operator procedure

1. Deploy the approved release with `LIVE_TRADING_ENABLED=false` first and verify
   dry-run behaviour.
2. Set `LIVE_TRADING_ENABLED=true` and redeploy.
3. Verify version, commit, and image reference.
4. Create a fresh backup and record the checksum.
5. (Optional) Run `python scripts/preflight_production.py --json`.
6. Confirm `/health/ready` returns `200` and the bot status is `running` or
   `waiting`.
7. Trigger the first live order via the dashboard **Buy Now** button or wait for
   the next scheduled cycle.
8. Verify the order on Kraken, in `data/transactions.json`, in the dashboard,
    and via Telegram.
9. Verify the next-cycle calculation.
10. Leave recurring trading disabled unless separately approved and documented.
11. Complete and sign this record.
