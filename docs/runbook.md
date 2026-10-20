# Operations runbook

## Before starting

Validate your YAML with `python -m gateway.cli check-config --config <path>`. Export the named team/admin/provider environment variables. `.env.example` is a reference, not an automatically loaded file. Never use the public demo credentials on a reachable deployment.

Select `GATEWAY_STATE_BACKEND=redis` and set `REDIS_URL` for persistent or multi-worker controls. A memory-backed restart clears policies, buckets, and spending. It must not be used to enforce a real organization budget.

## Health and diagnosis

Check `/health/live`, then `/health/ready`. Readiness verifies the control-plane and audit stores; it does not certify provider/model availability. Use `/admin/providers` to inspect per-worker circuits and `/admin/audit` to correlate a request ID with attempt outcomes and accounted amounts.

For a 429, read `Retry-After`. A token-bound request exceeding total bucket capacity returns 422; waiting will not make an oversized request fit. Reduce `max_tokens`, shorten the prompt, or review the team's policy.

For a 402, inspect `/admin/teams/{team}`. Look for retained estimates from failed calls and check configured prices. Do not simply delete ledger keys to restore funds: deletion can make prior spending invisible. Reconcile with the provider, document the decision, and update the cap through the admin API if approved.

For provider rejection, inspect credentials, model IDs, and parameter support. Authentication and invalid-request responses do not trigger fallback. A 503 after retry exhaustion means all eligible attempts failed or circuits were open.

For a stream error, preserve its partial-output status. Never label a stream successful based only on HTTP 200 or the `[DONE]` marker.

## Changing team policy

Fetch `/admin/teams/demo` with an admin credential. Submit the complete replacement policy to `PUT /admin/teams/demo/policy`. Allowed aliases must exist. The response confirms the accepted policy; audit events record intent and application.

Example fields:

```json
{
  "allowed_models": ["balanced"],
  "requests_per_minute": 60,
  "tokens_per_minute": 100000,
  "daily_budget_microusd": 5000000,
  "system_prompt": "Answer concisely."
}
```

Five million micro-USD is USD 5. Daily keys use UTC, not the local workstation timezone. Lowering a cap below already accounted spending blocks later attempts; it does not rewrite historical debits. Existing in-flight requests use their earlier policy snapshot.

Redis seeding preserves existing admin changes after a restart. To change a seeded policy, use the admin endpoint. Editing YAML alone is not a policy reset. Provider definitions and key mappings load at startup and require a controlled restart.

## Provider outage

Transient rate-limit, timeout, connection, and server failures use bounded retries and configured fallback chains. A circuit opens at the configured failure threshold within its window. After cooldown, one owned probe is admitted; success closes the circuit and failure extends cooldown.

The demo's `failover` client changes only the local mock server's mode. It restores that mode afterward, but the gateway still honors its existing cooldown. Use a fresh demo for independent measurements.

## Store and audit failures

Redis errors fail admission closed; there is no automatic memory fallback. Verify network reachability, Redis memory policy, persistence health, and available disk space. Restore Redis from an agreed recovery point and reconcile budget exposure before allowing paid traffic.

Request audit writes are best effort and emit an `audit_write_failed` log on failure. Investigate database connectivity, migrations, pool limits, filesystem permissions, and disk capacity. A successful response can exist without an audit row if storage was unavailable. Use the shared spending ledger and provider records for reconciliation.

Policy updates require writing an intent before the policy change, but Redis and audit storage are not transactionally coupled. An intent without an applied event is a signal to inspect the current policy rather than blindly replay the update.

For PostgreSQL, set `GATEWAY_AUDIT_BACKEND=postgres` and `DATABASE_URL`, then run
`python -m alembic upgrade head` before API startup. Check `python -m alembic current`
and `python -m alembic check`. Never use `create_all` at worker startup or run a
destructive downgrade to fix an unknown production database.

The real local stack is `compose.ollama.yaml`, on API port 8080. Its database is a
separate container on host port 55433. `migrate` showing `Exited (0)` is expected.
Use `docker compose -f compose.ollama.yaml logs gateway postgres migrate` to diagnose
startup. Do not reset Docker or delete volumes as a routine repair step.

## Monitoring

Grafana provides Operations, Business, and Performance dashboards. Budget utilization reads the authoritative current daily ledger; cost counters represent amounts accounted since a worker started. They are different measurements and must not be substituted for one another.

Prometheus alerts cover budget utilization at 80 percent, open circuits, error ratio, and end-to-end P99 latency. Alertmanager receives them in its local UI. No Slack message is sent by the default configuration.

To enable Slack, create a webhook using your workspace's approved process, store it outside version control under `secrets`, mount it as `/run/secrets/slack_webhook`, and replace the Alertmanager configuration with `monitoring/alertmanager.slack.example.yaml`. Restart only the affected service and verify a controlled test alert. Never paste a webhook into committed YAML.

The collector's debug exporter is a local tracing sink. Connect it to your organization's trace backend for retention and search. Traces currently cover request acceptance, team authentication, quota admission, and provider attempts.

## Stop and reset a local demo

Use Ctrl+C in the demo terminal. Starting it again resets only the memory control plane; it does not delete the SQLite audit history. The Compose volumes retain Redis and audit data after `docker compose down`. Do not remove volumes as a routine error-recovery action.
