# LLM Gateway Engineering Guide

This guide explains how to run, inspect, and extend the gateway built from Project 11 of the supplied BASWE project guide. Start with the local mock demo, follow one request through the code, and then use the existing Ollama model. The aim is to understand the engineering decisions behind reliable model access and to operate the implementation using measured evidence.

For the current real **Ollama + PostgreSQL + Redis** deployment, use the [real-stack walkthrough](local-stack-walkthrough.md). It includes Postman setup, migrations, persistence tests, and the new audit functions. The mock/native steps below remain separate learning paths on port 8000; the real Docker API uses 8080.

## 1 Project purpose

Applications should not each implement provider credentials, quotas, retry policy, and outage handling independently. This gateway centralizes those responsibilities. A team sends a typed chat request to one endpoint; a server-controlled route maps its model alias to an ordered provider chain.

The implementation combines FastAPI, Pydantic, HTTPX, Redis, OpenTelemetry, Prometheus, Grafana, Docker, and pytest. These libraries handle API boundaries, shared controls, transport, observability, packaging, and verification. Provider adapters support compatible chat APIs, Anthropic Messages, and Ollama.

## 2 Start the working demo

The project's directory is `D:\Kimi.Ai\llm-gateway`. An isolated `.venv` is already installed. Run these PowerShell commands:

```powershell
Set-Location -LiteralPath 'D:\Kimi.Ai\llm-gateway'
.\.venv\Scripts\python.exe -m gateway.cli demo
```

Expected startup behavior: the API listens on port 8000 and the mock provider listens on port 9001, both on loopback. Open `http://127.0.0.1:8000/docs` to view and try the API. Use `demo-team-key-change-me` as the team Bearer credential when working with this local demo.

In a second PowerShell terminal, run:

```powershell
Set-Location -LiteralPath 'D:\Kimi.Ai\llm-gateway'
.\.venv\Scripts\python.exe scripts/demo_client.py ask
```

Expected result: HTTP 200, a labeled mock response, token usage, `provider: primary`, and a non-estimated accounted cost. IDs and timestamps vary. Mock pricing is fictional and is only used to verify arithmetic.

The analyst team has a separate key and smaller quota. Public development credentials must be replaced before deployment. Environment-variable examples are not automatically read from `.env` files.

## 3 Read the code in this order

| Module | Responsibility | Question to answer while reading |
| --- | --- | --- |
| `schemas.py` | Request, usage, and policy contracts | What input is rejected before any provider call? |
| `config.py` | Providers, routes, team references, and secrets | Can a caller choose an arbitrary upstream URL? |
| `state.py` | Atomic admission and budget settlement | What prevents two concurrent requests spending the same allowance? |
| `circuit.py` | Open and half-open provider state | Who owns the single recovery probe? |
| `providers.py` | Vendor protocol translation | Which error types permit a retry? |
| `service.py` | Orchestration and accounting | What changes once stream output becomes visible? |
| `app.py` | HTTP endpoints and resource lifecycle | Why does a stream use a dedicated producer task? |
| `audit.py`, `postgres_audit.py`, `database.py`, and `telemetry.py` | Operational evidence | Why are prompt bodies excluded, and where are request IDs used? |

Comments explain decisions around cancellation, atomicity, spending estimates, and task ownership. The service never uses a model to make authentication or budget decisions.

## 4 Understand the request lifecycle

Authentication resolves the caller to a configured team. The team policy restricts model aliases and supplies optional system-prompt enrichment. The gateway revalidates the enriched prompt, then admits the request against two token buckets: requests and a conservative token allowance.

Interactive and batch work acquire separate per-worker capacity pools. Before each provider attempt, the gateway checks the circuit and predebits spending using configured prices and planned input/output bounds. A successful call settles the debit using reported token counts. Transient errors can trigger a bounded retry or a different permitted provider.

All calls have an overall deadline. No retry starts without another budget reservation. Policy changes affect subsequent policy snapshots; they do not rewrite a request already in progress.

## 5 Verify failure behavior

Run these scenarios against the local mock demo:

```powershell
.\.venv\Scripts\python.exe scripts/demo_client.py failover
.\.venv\Scripts\python.exe scripts/demo_client.py rate-limit
.\.venv\Scripts\python.exe scripts/demo_client.py budget
```

Expected results:

| Scenario | Expected result | Engineering meaning |
| --- | --- | --- |
| Primary outage | HTTP 200 from secondary | Retry and permitted fallback recover the request |
| Team quota exhausted | HTTP 429 with Retry After | Admission rejects excess traffic atomically |
| No remaining allowance | HTTP 402 | A provider call cannot start without a debit |
| Provider authentication failure | HTTP 502 without fallback | A credential error is not hidden by routing elsewhere |

The scripts restore the original policy and mock scenario. Existing circuit cooldown and consumed quota remain. Restart a memory demo when you need a clean, independent scenario or load measurement.

## 6 Understand cost accounting

One micro-USD is one millionth of a US dollar. Prices are configured as USD per million tokens, which is numerically micro-USD per token. The service uses decimal arithmetic and rounds fractional amounts upward.

For example, 29 input tokens at 5 micro-USD each plus 12 output tokens at 10 micro-USD each gives 265 micro-USD. The rates in the mock config are demonstration values, not commercial pricing.

A timeout does not prove the provider did no work. Unknown usage, ambiguous failures, and interrupted calls therefore retain a conservative debit. Every retry has its own debit. A response's request cost includes prior attempts; its usage fields describe the final completion. The estimate flag applies when any attempt was estimated.

The planning estimate is not a universal tokenizer guarantee. If a provider reports more usage than planned, the ledger records the higher actual amount. Prices, provider behavior, external fees, and persistence must be reviewed before relying on a cap. A crash retains a debit; operators reconcile uncertain entries against billing rather than silently refunding them.

## 7 Inspect streaming

```powershell
.\.venv\Scripts\python.exe scripts/demo_client.py stream
```

Expected result: `data:` events with content deltas, a final event with usage/accounting, and `[DONE]`. The adapter translates compatible SSE, Anthropic SSE, and Ollama NDJSON into one internal event contract.

Fallback is permitted before output reaches the client. After that, the stream is committed to one provider. A later failure emits an error event instead of appending another model's answer. Clients must inspect error records; HTTP 200 and `[DONE]` alone do not establish success.

The API first runs a producer on one task and uses a bounded queue to deliver events. This keeps timeout and tracing contexts on their owning task while Starlette consumes the response separately. Client cancellation closes the upstream iterator and retains conservative accounting.

## 8 Run a real local model

The Ollama configuration uses the installed `qwen3:4b-instruct-2507-q4_K_M` model. Export development credentials and start this configuration:

```powershell
$env:GATEWAY_ADMIN_KEY = 'demo-admin-key-change-me'
$env:DEMO_TEAM_KEY = 'demo-team-key-change-me'
.\.venv\Scripts\python.exe -m gateway.cli serve --config config/ollama.yaml
```

Stop the mock demo first if it occupies port 8000. The same `ask` and `stream` clients can now call the local model. The optional `scripts/verify_local_ollama.py` performs normal and streaming inference without starting another public API server.

Real verification returned HTTP 200 in both modes with provider-reported token usage. A deliberately small 32-token limit produced `finish_reason: length`, correctly identifying truncated output. This confirms the integration path; it does not establish task accuracy or model quality. Local cost is configured as zero metered API fee and excludes electricity and hardware.

## 9 Run verification and inspect evidence

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests scripts migrations
.\.venv\Scripts\ruff.exe format --check src tests scripts migrations
```

Tests exercise credentials, allowlists, atomic concurrent rate checks, budget settlement, retry/fallback, half-open probe ownership, streaming failures, cancellation, priority isolation, and metadata-only observability. Deterministic Redis tests use fakeredis and its Lua engine. Opt-in tests also run against real PostgreSQL and Redis when their test URLs are supplied; see the real-stack walkthrough. Neither is a distributed cluster availability test.

Run a fresh demo and measure local HTTP traffic:

```powershell
.\.venv\Scripts\python.exe scripts/load_test.py --requests 5000 --concurrency 16
```

The verified mixed-team workload completed 5,000 requests successfully at concurrency 16. All response cost totals matched both team ledgers. Reported latency includes provider handling, auditing, HTTP, and the gateway. It is not isolated gateway overhead or a hosted-model benchmark. The detailed verification document records measured numbers and remaining unverified components.

## 10 Operate the deployment stack

The original `compose.yaml` supplies the gateway, Redis, mock provider, Prometheus, Grafana, Alertmanager, and an OpenTelemetry collector. Grafana has Operations, Business, and Performance dashboards. Default alerts remain local; a Slack template is optional and requires a separately supplied webhook secret.

```powershell
docker compose up --build
```

The real-model alternative is `docker compose -f compose.ollama.yaml up -d --build`. It uses host Ollama, PostgreSQL audit storage and migrations, and Redis. That stack was launched and tested on 19 October 2026, including controlled database and Redis restarts. The original mock Compose configuration was not used for those container checks. Published ports bind to loopback and use public development credentials.

The memory backend is single-process development state. Redis shares policies, rate buckets, and spending. Circuits, priority pools, and Prometheus registries remain per worker. PostgreSQL supplies centralized audit metadata in the real stack; multiple replicas still need a metrics/capacity strategy. Provider definitions and credential mappings load at startup.

## 11 Diagnose common problems

| Symptom | Likely explanation | Next action |
| --- | --- | --- |
| Missing or short credential at startup | Required environment value was not exported | Read the config's named secret variables and set them |
| HTTP 422 token bound exceeds quota | One request is larger than the entire token bucket | Reduce prompt/output planning or review the quota |
| HTTP 429 | Team bucket is exhausted | Respect Retry After; avoid retry storms |
| HTTP 402 after an outage | Failed attempts retained conservative spending | Inspect audit estimates and reconcile provider billing |
| Circuit stays open after recovery | Cooldown has not ended or a probe failed | Inspect provider state and configured cooldown |
| Stream returned 200 but contains an error | Failure occurred after response headers were sent | Preserve partial status and do not call it a success |
| Redis failure | Shared controls are unavailable | Restore the store; the service does not switch silently to memory |

## 12 Continue learning

Reproduce one failure scenario and find its request ID in `/admin/audit`. Explain why the same failed call can be retryable but still retain a debit. Read the half-open ownership test and explain why an old request cannot release a newer probe.

Then compare the memory and Redis state implementations. Explain why separate request and token checks would be unsafe if a failed token check still consumed a request allowance. Extend tests before extending behavior.

Consult the architecture, API, runbook, decisions, and sources documents for details. Primary references include FastAPI lifespan, Redis Lua scripting, HTTPX timeouts, Ollama chat, OpenTelemetry Python, Prometheus instrumentation, and Grafana provisioning. The source blueprint is Project 11 in the user-supplied BASWE guide, pages 31 through 34.
