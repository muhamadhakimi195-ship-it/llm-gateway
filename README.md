# LLM Gateway

A provider-neutral LLM gateway built with FastAPI that gives applications one chat API for compatible chat providers, Anthropic Messages, and local Ollama models. It centralizes team authentication, model access, request and token quotas, daily budget accounting, provider failover, and observability.

Applications call `POST /v1/chat/completions` with a team credential and a model alias such as `balanced`. The gateway applies the team's policy, selects an upstream provider, and returns a normalized response or server-sent event (SSE) stream.

## Features

- **Team access controls:** separate Bearer credentials, model allowlists, and optional team system prompts.
- **Traffic controls:** atomic request and token admission, with separate interactive and batch capacity pools.
- **Spending controls:** daily UTC budgets, per-attempt reservations, and integer micro-USD accounting.
- **Resilient routing:** configurable provider chains, bounded retries, deadlines, and circuit breakers.
- **Provider adapters:** compatible chat APIs, Anthropic Messages, and Ollama, including streaming normalization.
- **Persistent state:** Redis for shared policies, quotas, and spending; PostgreSQL or SQLite for metadata-only audit records.
- **Observability:** Prometheus metrics, Grafana dashboards, Alertmanager rules, and OpenTelemetry tracing.
- **Local development:** deterministic mock providers, fault scenarios, Docker Compose stacks, and automated tests.

This is a working reference service with documented deployment boundaries. The mock demo runs without a paid provider account; the Ollama stack runs real local inference. Its original design follows Project 11 in the supplied BASWE project guide; see [sources](docs/sources.md) for attribution.

## How it works

```mermaid
flowchart LR
    A[Application] --> B[Authenticate team]
    B --> C[Validate model alias and policy]
    C --> D[Apply quotas and acquire capacity]
    D --> E[Reserve attempt budget]
    E --> F[Call configured provider]
    F --> G[Settle usage and record metadata]
    G --> H[Return response or SSE stream]
    F -->|Eligible failure before output| E
```

1. **Authenticate and validate.** FastAPI checks the team Bearer token and text-only request schema. The requested alias must be allowed by the team's policy.
2. **Admit the request.** The gateway checks request and conservative token quotas atomically, then waits for capacity in the requested priority pool.
3. **Select and call a provider.** The configured alias determines the ordered provider chain. Open circuits are skipped, and each attempt reserves a budget allowance before making an upstream call.
4. **Handle failures.** Eligible transient failures can trigger bounded retries and fallback within the request deadline. Authentication and request rejections are terminal. Once streaming output begins, the gateway stays with that provider.
5. **Settle and observe.** Reported token usage settles against configured prices. Uncertain attempts retain conservative charges. The response identifies the actual provider/model and accounted cost; audit storage records metadata without prompt or completion bodies.

Memory state suits a single development process. Redis shares policy and accounting state across workers; circuit breakers and priority capacity remain per worker. See [architecture](docs/architecture.md) for the full lifecycle and accounting rules.

## Start here

For the real **Ollama + PostgreSQL + Redis** stack, start Docker Desktop and Ollama, install the model with `ollama pull qwen3:4b-instruct-2507-q4_K_M`, then run:

```powershell
# Run from the repository root.
docker compose -f compose.ollama.yaml up -d --build
```

Use `http://127.0.0.1:8080/docs` or send Postman requests to
`http://127.0.0.1:8080/v1/chat/completions`. The development team Bearer token is
`demo-team-key-change-me`. This is real local Qwen inference, not the mock below.
PostgreSQL stores audit metadata and Redis persists shared limits and policies.
The one-shot `migrate` container exiting with code 0 is normal.

Read the [step-by-step code and local stack guide](docs/local-stack-walkthrough.md)
for Postman setup, migrations, restart verification, and functions in reading order.

### Mock-only alternative

After completing [Fresh setup](#fresh-setup), run from PowerShell:

```powershell
# Run from the repository root.
.\.venv\Scripts\python.exe -m gateway.cli demo
```

The demo starts the gateway at `http://127.0.0.1:8000` and a deterministic mock provider at `http://127.0.0.1:9001`. Open `http://127.0.0.1:8000/docs` for the interactive API reference. The mock produces labeled sample responses, not LLM-generated answers. Press Ctrl+C to stop both services.

In another terminal in the same project directory:

```powershell
.\.venv\Scripts\python.exe scripts/demo_client.py ask
.\.venv\Scripts\python.exe scripts/demo_client.py stream
.\.venv\Scripts\python.exe scripts/demo_client.py failover
.\.venv\Scripts\python.exe scripts/demo_client.py rate-limit
.\.venv\Scripts\python.exe scripts/demo_client.py budget
```

Fault scenarios restore the original policy and mock failure setting. Circuit cooldown and already consumed quotas remain in effect. Restart the memory-backed demo before a clean load test.

## Fresh setup

Use Python 3.11 or later. Run these commands from the repository root in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests scripts migrations
.\.venv\Scripts\ruff.exe format --check src tests scripts migrations
```

On Linux or macOS, create the environment with `python3 -m venv .venv` and use `.venv/bin/python` and `.venv/bin/ruff` in place of the Windows paths. The runtime and development lock files record resolved package versions; `pyproject.toml` declares compatibility ranges. GitHub Actions configures checks on Python 3.11, 3.12, and 3.14, plus a separate PostgreSQL/Redis integration job.

## Engineering stack

| Component | Library or tool | Responsibility |
| --- | --- | --- |
| HTTP API | FastAPI and Uvicorn | Async endpoints, dependencies, lifecycle, OpenAPI |
| Contracts | Pydantic | Strict requests, normalized responses, configuration validation |
| Provider transport | HTTPX | Connection pooling, timeouts, streaming, injectable test transport |
| Distributed controls | Redis and redis-py | Atomic token buckets, spending debits, shared team policies |
| Tracing | OpenTelemetry SDK and OTLP exporter | Metadata spans with optional collector export |
| Metrics and alerts | prometheus-client, Prometheus, Alertmanager | Counters, histograms, alert evaluation and local alert UI |
| Dashboards | Grafana | Operations, spending, and performance panels |
| Audit storage | PostgreSQL, SQLAlchemy, Psycopg; SQLite alternative | Async pooled, metadata-only audit writes; lightweight SQLite for isolated demos/tests |
| Schema changes | Alembic | Explicit versioned PostgreSQL migrations, separate from API startup |
| Verification | pytest, pytest-asyncio, fakeredis with Lua, Ruff | Functional checks, concurrency checks, real Lua execution in an emulated Redis server, linting |
| Packaging | Docker Compose and GitHub Actions | Repeatable service topology and CI checks |

## Request example

The public development credential is `demo-team-key-change-me`. It is only for loopback demos.

```powershell
$headers = @{ Authorization = 'Bearer demo-team-key-change-me' }
$body = @{
  model = 'balanced'
  messages = @(@{ role = 'user'; content = 'Explain circuit breakers briefly.' })
  max_tokens = 64
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8080/v1/chat/completions' -Headers $headers -ContentType 'application/json' -Body $body
```

`balanced` is a policy alias. The gateway chooses its permitted provider chain; callers cannot supply arbitrary upstream URLs or physical model IDs. Responses include final-provider usage, the actual served provider/model, a request ID, and total accounted request cost in integer micro-USD. `cost_estimated` is true when any attempt used conservative accounting.

This example targets the real Docker stack on 8080. Use 8000 for the native/mock demo. The mock config also has an `analyst` team with a separate public development key, `demo-analyst-key-change-me`, a smaller quota, and a separate spending ledger; the Ollama config contains only `demo`.

## Use your local Qwen model

Make sure Ollama is running and install the model configured in `config/ollama.yaml`:

```powershell
ollama pull qwen3:4b-instruct-2507-q4_K_M
```

Set process environment variables explicitly; the service does not automatically read `.env` files.

```powershell
$env:GATEWAY_ADMIN_KEY = 'demo-admin-key-change-me'
$env:DEMO_TEAM_KEY = 'demo-team-key-change-me'
.\.venv\Scripts\python.exe -m gateway.cli serve --config config/ollama.yaml
```

Then run the `ask` or `stream` client. For the optional direct local-inference check:

```powershell
.\.venv\Scripts\python.exe scripts/verify_local_ollama.py
```

Local model pricing is zero metered API cost in this example; it does not account for hardware or electricity. Hosted-provider integration requires your own model IDs, credentials, and verified prices in `config/providers.example.yaml`. No paid provider calls were made during verification.

## Real local deployment stack

```powershell
docker compose -f compose.ollama.yaml up -d --build
.\.venv\Scripts\python.exe scripts/verify_live_gateway.py --url http://127.0.0.1:8080
.\.venv\Scripts\python.exe scripts/verify_compose_stack.py --restart
```

The last command temporarily restarts only this project's API, PostgreSQL, and Redis.
Do not run it while another person is using this local stack. It checks committed
audit records, Redis AOF recovery, live metrics scraping, dashboards, and trace receipt.
It never deletes database/Redis volumes or flushes a Redis database.

| Service | Local address |
| --- | --- |
| Gateway API and Swagger | http://127.0.0.1:8080/docs |
| PostgreSQL | 127.0.0.1:55433, database `llm_gateway`, user `gateway` |
| Redis | 127.0.0.1:56379 |
| Grafana | http://127.0.0.1:3001 |
| Prometheus | http://127.0.0.1:9091 |
| Alertmanager | http://127.0.0.1:9094 |

The stack's PostgreSQL password is `local-gateway-db-change-me`. Grafana uses
`admin` / `demo-password-change-me`. All are PUBLIC LOCAL DEVELOPMENT defaults,
not production secrets. Published ports bind only to loopback. Ollama stays on the
Windows host; the container reaches it through `host.docker.internal:11434`.
Your existing Windows PostgreSQL service on 5432 is not modified.

To stop this stack while preserving its database and Redis volumes:

```powershell
docker compose -f compose.ollama.yaml down
```

Do not add `-v`: deleting volumes destroys audit and quota history. Persistence is
not a backup; configure database backups and restricted runtime credentials before
any shared deployment.

## Original mock deployment stack

```powershell
docker compose up --build
```

Local URLs: API `8000`, mock control API `9001`, Grafana `3000`, Prometheus `9090`, Alertmanager `9093`. Grafana's development login is `admin` / `demo-password-change-me`. The stack uses Redis-backed quotas and spending controls, provisioned dashboards, and an OTLP collector. Collector traces are visible in `docker compose logs otel`.

All published ports bind to loopback. Replace development credentials and add TLS and network controls before deployment. Redis uses AOF persistence and `noeviction`; losing Redis data loses quota and budget history. This original configuration still uses mock providers and SQLite. Use `compose.ollama.yaml` for the real model/PostgreSQL stack; see the verification document for actually executed checks.

Alertmanager's default receiver displays local alerts without sending external notifications. An optional Slack configuration template is provided; enabling it requires supplying a webhook as a mounted secret and selecting that configuration.

## Documents and code map

- [Real-stack walkthrough](docs/local-stack-walkthrough.md): current PostgreSQL/Redis setup, Postman testing, and file-by-file function explanations.
- [Word engineering handbook](docs/LLM_Gateway_Engineering_Guide.docx): original mock/SQLite setup and architecture; use the real-stack walkthrough for the PostgreSQL additions.
- [Engineering guide](docs/engineering-guide.md): setup, design, contracts, accounting, and reading exercises.
- [Architecture](docs/architecture.md): request flow, state ownership, invariants, and boundaries.
- [Runbook](docs/runbook.md): operational checks, policy changes, provider failures, and recovery.
- [API reference](docs/api.md): endpoints, payloads, status codes, and stream semantics.
- [Decisions](docs/decisions.md): rationale and tradeoffs.
- [Verification](docs/verification.md): measured results and what remains unverified.
- [Sources](docs/sources.md): primary documentation and guide attribution.

Core implementation files are `src/gateway/app.py`, `service.py`, `providers.py`, `state.py`, and `circuit.py`. Comments explain concurrency, accounting, and lifecycle choices. Tests cover behavior rather than simply duplicating the code.

## Load test

Start a fresh local demo, then run:

```powershell
.\.venv\Scripts\python.exe scripts/load_test.py --requests 5000 --concurrency 16
```

The script mixes team credentials and priorities, records success and failure counts, and compares response cost totals with the ledger. It refuses non-loopback hosts. Results are written to `artifacts/load-test.json`. Run it without other simultaneous traffic if you want exact accounting equality.

The documented latency includes provider handling, HTTP, queueing, SQLite audit writes, and the gateway. It is not isolated proxy overhead and does not establish a sub-10 ms SLA.

## Deployment boundaries

The memory backend is for one development process. Redis shares quotas, policies, and spending across workers, but circuit breakers, priority pools, and Prometheus counters are per worker. The default deployment is one gateway worker; horizontal deployment needs a deliberate metrics strategy and centralized audit storage. Tools, images, structured-output enforcement, response caching, and idempotent generation are outside this chat contract.

Financial enforcement uses declared prices and conservative token planning. If an upstream provider reports usage above the planning estimate, the ledger records the actual higher cost rather than hiding it. This service is not a guarantee about external invoices. Read the accounting section before assigning production budgets.
