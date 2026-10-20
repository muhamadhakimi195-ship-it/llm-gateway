# Architecture decisions

## Deterministic gateway orchestration

FastAPI and HTTPX provide the transport and lifecycle. Small typed adapters normalize three protocols. Routing and controls are deterministic Python code because policy enforcement should be inspectable and reproducible. An LLM or agent framework does not choose whether a caller is authenticated, whether money is available, or whether a partial stream may be retried.

This keeps provider formats visible for learning. A larger provider catalog could replace these adapters with a maintained aggregation library after contract and error-semantics tests are preserved.

## Redis for shared admission

Request and token checks run in one Lua script; budget debit and settlement each have their own atomic script. Per-process dictionaries are inadequate for multiple replicas. The memory implementation exists for a portable offline demo and clearly states its single-process scope.

Lua scripts are fixed and parameterized; no request is concatenated into executable script text. Related budget keys share a Redis hash tag. The client handles script-cache reloads through `register_script`.

## Conservative charging on uncertainty

A timed-out provider may still complete and bill a generation. Automatically refunding it could understate spending and allow repeated expensive failures. We predebit each attempt, settle known usage, and retain estimates when the outcome is unknown.

The tradeoff is deliberate overestimation after uncertain failures. The audit marks estimates so an operator can reconcile them against provider billing. There is no automatic financial reconciliation or hidden assertion that the estimate equals the invoice.

## One provider per visible stream

Fallback is allowed before the first output event. Once output is visible, switching models could duplicate tokens, contradict earlier content, or corrupt downstream parsing. After that point, failure is explicit and accounting stays conservative.

Streaming runs on one producer task with a bounded delivery queue. This avoids transferring tracing context and timeout ownership between the endpoint's task and Starlette's response-consumption task.

## Local circuits and priority pools

Circuit breakers operate per worker, with an owned half-open lease so an old request cannot release another request's probe. Interactive and batch work use separate capacity pools. These provide isolation within one worker without claiming a distributed fair scheduler.

Distributed circuit state and a cross-replica scheduler would add another availability and consistency boundary. They are documented future deployment extensions rather than hidden assumptions.

## Metadata observability

Prometheus labels use configured team/provider identifiers and finite outcomes. Request IDs appear in traces and audits, not metric labels. Prompt and response bodies are excluded from normal tracing and audit storage to limit retention of customer data and control cardinality.

SQLite WAL keeps isolated demos and tests portable. The real local stack uses PostgreSQL with SQLAlchemy's async engine and Psycopg, behind the same `AuditStore` contract. Short per-event transactions do not hold pooled database connections during model generation. Alembic runs as a separate deployment step; the API checks the migrated table and does not silently fall back to SQLite if PostgreSQL fails.

Best-effort request auditing does not discard an already paid valid completion if storage fails at runtime. This can leave missing audit rows and is not a lossless compliance design. Policy updates require an audit intent before changing state; Redis state and PostgreSQL audit storage do not form a cross-system transaction. Persistence is not a backup, so shared deployments need a retention and restore policy.
