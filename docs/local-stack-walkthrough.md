# Real local gateway: run it and understand it

This is the current guide for the **Ollama + PostgreSQL + Redis** implementation. The original mock demo and Word handbook remain useful for fault scenarios, but they do not describe the new PostgreSQL path.

## 1. What you have built

Your application sends one chat request to the gateway. The gateway checks the team's permission and allowance, calls your local Qwen model through Ollama, returns the answer, and records operational metadata in PostgreSQL. Redis holds shared policies, rate allowances, and the spending ledger.

Docker does not make the model smarter. It packages the API and supporting services, connects them on a private network, orders their startup, and gives PostgreSQL and Redis persistent volumes. Ollama still runs on Windows, outside Docker.

```text
Postman / application
        |
        v
Gateway :8080 -- permission + admission --> Redis
        |
        +-- provider call --> Windows Ollama :11434 --> Qwen
        |
        +-- audit metadata --> PostgreSQL
        |
        +-- metrics --> Prometheus --> Grafana / Alertmanager
        +-- trace spans --> OpenTelemetry collector logs
```

This is a working local reference, not a production deployment or a model-quality evaluation.

## 2. Start the real stack

Start Docker Desktop and Ollama. In PowerShell:

```powershell
Set-Location -LiteralPath 'D:\Kimi.Ai\llm-gateway'
ollama list
docker compose -f compose.ollama.yaml up -d --build
docker compose -f compose.ollama.yaml ps -a
Invoke-RestMethod http://127.0.0.1:8080/health/ready
```

The configured model is `qwen3:4b-instruct-2507-q4_K_M`; `ollama list` should include it. The gateway, PostgreSQL, and Redis should become healthy. The `migrate` service should exit with code **0**: it is a one-time schema update, not a server.

Use **8080** for this stack. Port 8000 belongs to the earlier native/demo setup and can show different storage behavior.

| Component | Address on Windows | Purpose |
| --- | --- | --- |
| Gateway | `http://127.0.0.1:8080/docs` | API and interactive schema |
| Ollama | `http://127.0.0.1:11434` | Real local model inference |
| PostgreSQL | `127.0.0.1:55433` | Database `llm_gateway`, user `gateway` |
| Redis | `127.0.0.1:56379` | Shared control state |
| Grafana | `http://127.0.0.1:3001` | Three provisioned dashboards |
| Prometheus | `http://127.0.0.1:9091` | Scraped metrics and alert rules |
| Alertmanager | `http://127.0.0.1:9094` | Local alerts; no external notifications |

The container calls Ollama at `host.docker.internal:11434`. Inside a container, `127.0.0.1` means that container, not your Windows host.

All passwords below are **public local-development defaults**:

- Team token: `demo-team-key-change-me`
- Admin token: `demo-admin-key-change-me`
- Metrics token: `demo-metrics-key-change-me`
- PostgreSQL password: `local-gateway-db-change-me`
- Grafana login: `admin` / `demo-password-change-me`

Ports bind to loopback. Do not expose this stack to a network with these credentials. The existing Windows PostgreSQL service on port 5432 is separate and is not changed.

## 3. Send your first request in Postman

Create a request with these settings:

| Setting | Value |
| --- | --- |
| Method | `POST` |
| URL | `http://127.0.0.1:8080/v1/chat/completions` |
| Authorization type | Bearer Token |
| Token | `demo-team-key-change-me` — enter only the token, without another `Bearer` prefix |
| Body | raw, JSON |
| Content-Type | `application/json` |

```json
{
  "model": "balanced",
  "messages": [
    {"role": "user", "content": "Explain an API gateway in three short sentences."}
  ],
  "max_tokens": 128,
  "temperature": 0,
  "stream": false
}
```

Allow up to 180 seconds in your client for cold model startup, although the gateway's own deadline is 120 seconds. A client timeout cannot extend the gateway deadline.

Look for HTTP 200, nonempty assistant text, a response `id`, `gateway.provider: local`, the Qwen `served_model`, and token usage. `finish_reason: length` means the output hit its token limit; it is not proof of a complete answer. Output wording can vary.

`balanced` is an allowed **route alias**, not the physical model name. `config/ollama.yaml` maps it to the `local` provider. The caller cannot choose an arbitrary upstream URL.

For streaming, change only `"stream": true`. Inspect the `data:` events, the final usage/finish event, and `[DONE]`. There must be **no error record**. A 200 status or `[DONE]` alone is not enough to call a stream successful. If your client buffers events, use the verification script in section 6 to check the protocol.

## 4. Find the same request in PostgreSQL

Copy the normal response's `id`. In Postman, send:

```text
GET http://127.0.0.1:8080/admin/audit
Authorization: Bearer demo-admin-key-change-me
```

Find entries with the same `request_id`. A successful call with one provider attempt normally produces two rows: `attempt` and `request`. Retries can add attempt rows. The endpoint returns only the latest 100 events; it is not a complete historical search API.

Inspect the database directly without installing another SQL client:

```powershell
docker compose -f compose.ollama.yaml exec postgres psql -U gateway -d llm_gateway
```

At the `psql` prompt:

```sql
SELECT id, timestamp, kind, request_id, team, payload
FROM audit_events
ORDER BY id DESC
LIMIT 10;
```

To find one request, replace the example identifier with your copied ID:

```sql
SELECT kind, payload
FROM audit_events
WHERE request_id = 'paste-your-request-id-here'
ORDER BY id;
```

Exit with `\q`. Audit rows contain operational metadata, not the email/chat content, model answer, or credentials. The small verification reports under `artifacts/` do include answers to their fixed test question; do not treat those files as a customer-data archive.

## 5. Read the code file by file

Read in this order. Do not try to memorize every line: follow one request and explain what each boundary protects.

### A. Configuration and input contract

**`config/ollama.yaml`** defines the provider, physical model, alias, team defaults, timeout, retry count, and prices. Docker overrides its base URL with `OLLAMA_BASE_URL`. Ollama has zero metered API prices here; this excludes hardware and electricity.

**`src/gateway/schemas.py`** defines `Message`, `ChatRequest`, `Usage`, `StreamEvent`, and `TeamPolicy`. `ChatRequest.bounded_prompt()` validates message ordering and size. Invalid input is rejected before model inference. Unknown fields are not silently accepted.

**`src/gateway/config.py`** loads and validates the server-controlled configuration. Provider credentials come from named environment variables, not from the request body.

### B. Startup and HTTP boundary

**`src/gateway/cli.py` → `main()`** starts the server. **`src/gateway/runtime.py` → `loop_factory()`** supplies a Windows SelectorEventLoop because Psycopg's async connections cannot use Windows' default Proactor loop. `run_async()` applies the same choice to migrations and command-line entry points that need PostgreSQL.

**`src/gateway/app.py` → `create_app()`** builds the FastAPI application. Its `lifespan()` opens the shared state, HTTP client, audit store, metrics, and tracer. `AsyncExitStack` closes owned resources, including if later startup fails. It does not open a new connection pool for every request.

The main HTTP functions are:

| Function | Responsibility |
| --- | --- |
| `request_identifier()` | Assign a correlation ID |
| `team_auth()` | Resolve a team credential |
| `admin_auth()` / `metrics_auth()` | Keep privileged access separate |
| `ready()` | Check both control-state and audit storage; not model quality |
| `chat()` | Accept the typed request and produce JSON or SSE |
| `audit()` | Return recent metadata for authorized inspection |

### C. One orchestration path for normal and streaming calls

**`src/gateway/service.py` → `GatewayService.events()`** is the main orchestration function:

1. Read the team policy and check the model alias.
2. Add any team system prompt, then validate the result again.
3. Estimate a conservative token bound and call `store.admit()`.
4. Acquire the appropriate interactive/batch capacity slot.
5. Check the provider circuit and reserve the planned spending.
6. Call the selected provider and handle permitted retries/fallback.
7. Settle known usage or retain a conservative estimate on uncertainty.
8. Write attempt/request metadata and release capacity.

`token_bound()` is a planning bound, not a real tokenizer. `cost_microusd()` performs decimal price arithmetic. `settle()` reconciles the reservation and records attempt usage/cost. `audit_event()` logs an audit-write failure rather than discarding an already-paid valid completion.

**`src/gateway/providers.py` → `ProviderAdapter.request_parts()`** translates the common request into the provider's protocol. `generate()` handles one response; `stream()` translates upstream events into `StreamEvent`. Ollama streams NDJSON; the public gateway streams SSE. These are different wire formats.

**`src/gateway/circuit.py`** tracks repeated provider failures per worker. Fallback is allowed before output is visible; after stream output begins, a later failure must be explicit rather than mixing two providers' answers.

### D. Redis: fast shared control state

**`src/gateway/state.py`** defines the `StateStore` contract and two implementations. `MemoryStore` is for one development process. `RedisStore` is selected by this Compose stack.

| Function | Meaning |
| --- | --- |
| `seed_policy()` | Create defaults only if no policy exists; do not reset policy on restart |
| `policy()` / `update_policy()` | Read or replace the shared team policy |
| `admit()` | Check request and token buckets together, atomically in Lua |
| `reserve()` | Debit a provider attempt before calling it |
| `settle()` | Apply final usage once; repeated settlement cannot double-refund |
| `spent()` | Read the current UTC-day spending ledger |

Why Lua? Separate read/check/write requests would let concurrent callers spend the same allowance. Atomic scripts keep each control operation together. Redis does not store the SQL audit history. The Compose Redis volume and AOF retain its data across controlled restarts; they are not backups or a proof against every crash/disk-loss scenario.

### E. PostgreSQL: durable audit metadata

**`src/gateway/audit.py` → `AuditStore`** is a Python Protocol: it describes `write()`, `recent()`, `health()`, and `close()`. `GatewayService` depends on this behavior instead of a specific database class. That is why PostgreSQL can replace SQLite without rewriting the routing logic.

`open_audit()` selects the backend. PostgreSQL requires `DATABASE_URL` and a migrated table. A failed PostgreSQL startup does **not** silently switch to SQLite.

**`src/gateway/database.py`** defines the SQLAlchemy table and `database_url()`. The table contains an identity ID, timezone-aware timestamp, kind, request ID, team, and JSONB payload. The metadata definition describes the schema; it does not create it on API startup.

**`src/gateway/postgres_audit.py` → `PostgresAuditLog`** implements the contract:

| Function | Why it exists |
| --- | --- |
| `__init__()` | Build a bounded async connection pool with timeouts |
| `write()` | Commit one short event transaction; never hold a DB connection while the model generates |
| `recent()` | Return a bounded, newest-first query |
| `health()` | Check the actual audit table, not just database connectivity |
| `close()` | Dispose the pool during shutdown |

**`migrations/versions/0001_postgres_audit.py` → `upgrade()`** creates the table and indexes. **`migrations/env.py`** connects Alembic to the async driver. **`compose.ollama.yaml` → `migrate`** runs `alembic upgrade head` before allowing the API to start.

Why not `create_all()` during every API startup? A reviewed, versioned migration records which schema change was applied and keeps deployment changes separate from request serving.

Check migration state:

```powershell
docker compose -f compose.ollama.yaml exec -T gateway python -m alembic current
docker compose -f compose.ollama.yaml exec -T gateway python -m alembic check
```

Expected: revision `0001_postgres_audit (head)` and no new upgrade operations. A migration downgrade can delete data; do not use it as a routine restart command.

### F. Packaging and observability

**`Dockerfile`** packages Python, dependencies, application code, configuration, and migrations into a non-root runtime image. **`compose.ollama.yaml`** supplies the local service topology and named storage volumes. **`monitoring/`** supplies scrape configuration, alert rules, dashboards, and collector configuration.

**`src/gateway/telemetry.py`** creates metrics and traces. Request IDs belong in audit/traces, not unbounded Prometheus labels. The collector currently logs receipt; it is not a searchable trace-retention backend.

## 6. Repeat the verification

These scripts use small real local model generations. Run them from the project directory with the stack running:

```powershell
.\.venv\Scripts\python.exe scripts/verify_live_gateway.py --url http://127.0.0.1:8080
.\.venv\Scripts\python.exe scripts/verify_compose_stack.py --restart
```

The second script temporarily restarts **this project's gateway, PostgreSQL, and Redis**. Do not run it during another person's work. It checks the same audit IDs after restarts, a uniquely namespaced Redis policy/spending marker after Redis restart, authentication, Prometheus scraping/rules, Grafana provisioning, Alertmanager readiness, and collector receipt. It cleans only its own Redis marker keys, not application state.

For the unit and real-storage integration suite:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://gateway:local-gateway-db-change-me@127.0.0.1:55433/llm_gateway'
$env:TEST_REDIS_URL = 'redis://127.0.0.1:56379/0'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check src tests scripts migrations
.\.venv\Scripts\ruff.exe format --check src tests scripts migrations
```

Use only this dedicated local database for these integration tests: they apply migrations and append synthetic audit rows. Without the two test URLs, real-storage tests skip. The verified full run passed **62 tests** on 19 October 2026.

`scripts/verify_restart.py` is an additional API-process restart test. It starts only its own child API on 8081, uses `DATABASE_URL` for PostgreSQL, and uses `TEST_REDIS_URL` when set. With Redis it consumes a one-request allowance, restarts its child, and verifies the next request still receives 429. It does not restart the normal 8080 stack. See [verification results](verification.md) for recorded evidence and limits.

To repeat that additional test against the Docker database after setting the test URLs above:

```powershell
$env:DATABASE_URL = $env:TEST_DATABASE_URL
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
.\.venv\Scripts\python.exe scripts/verify_restart.py
```

That child has a unique Redis namespace, so its temporary one-request policy does not replace the normal stack's `demo` policy.

## 7. Common errors

| Symptom | Check |
| --- | --- |
| Connection refused at 8080 | Docker engine and `docker compose -f compose.ollama.yaml ps -a` |
| Gateway does not start | `docker compose -f compose.ollama.yaml logs --tail 80 migrate gateway` |
| Migration container exits 0 | Normal; inspect errors only for nonzero exit |
| Model connection fails | Ollama running, configured model installed, container host URL correct |
| 401 | Correct team token for chat; separate admin token for audit |
| 422 | Request contract or planned tokens exceed team capacity |
| 429 after restart | Expected if the persistent token bucket has not refilled; respect Retry-After |
| Editing YAML seems ineffective | Rebuild/recreate with `up -d --build`; existing Redis policies are not overwritten by new defaults |
| Old results on 8000 | Use 8080 for this real Docker stack |

Do not fix database errors by deleting volumes or resetting Docker Desktop. Inspect the specific service logs first. Do not paste secrets or private prompts from logs into a public issue.

## 8. Your next learning checkpoint

Do this before adding another feature:

1. Send one normal Postman request and save its ID.
2. Find its `attempt` and `request` rows through both the admin API and SQL.
3. Explain why the model answer itself is absent from those rows.
4. Explain the difference between Redis's ledger and PostgreSQL's audit history.
5. Follow `app.py: chat()` → `service.py: events()` → `providers.py: generate()` → `service.py: settle()` → `postgres_audit.py: write()`.

Your deliverable is the request ID and a short explanation of those five functions. You do not need to modify the code for this checkpoint.

## 9. Stop safely and know the boundaries

```powershell
docker compose -f compose.ollama.yaml down
```

This stops the project containers and preserves named volumes. **Do not add `-v`** unless you intentionally want to destroy the stored audit and quota data. Ollama and your existing Windows PostgreSQL service are unaffected.

An optional native test cluster was also created by `scripts/local_postgres.ps1` on port 55432. It is a separate database from Docker's 55433. The script's `start`, `status`, and `stop` actions target only that project-owned cluster and keep its data. Prefer the Docker stack above for this lesson so you do not mix histories.

Before shared/production use, add restricted database roles, private secrets, Redis authentication/network controls, TLS, backups and restore drills, retention, and deployment-specific capacity testing. The development PostgreSQL role is privileged. Request audits are best effort at runtime; Redis settlement and SQL audit writes are not one distributed transaction. Readiness checks do not guarantee every concurrent request will be audited. This is not a lossless compliance ledger.

With zero local prices, the real Ollama setup cannot demonstrate a paid spending-limit rejection. With one provider it cannot demonstrate independent-provider failover. Use the original mock scenarios for those deterministic tests; do not pretend they prove a real second-provider deployment.

## References

Use the primary links in [sources](sources.md). For this lesson, start with [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/), [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html), [Alembic asyncio](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic), and [Redis Lua](https://redis.io/docs/latest/develop/programmability/eval-intro/). Read the relevant section after following that file locally; you do not need to study the whole library first.
