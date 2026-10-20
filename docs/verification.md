# Verification results

## Current real-stack verification — 19 October 2026

The updated local suite passed **62 tests**, including the opt-in tests against real PostgreSQL and Redis containers. The host test interpreter was Python 3.14; the API container uses Python 3.12. Ruff lint and formatting checks passed for `src`, `tests`, `scripts`, and `migrations`; `pip check` reported no broken requirements.

The separate `compose.ollama.yaml` project was built and launched. The API is on 8080, PostgreSQL on 55433, Redis on 56379, and Ollama remains on the Windows host. Alembic reports `0001_postgres_audit (head)` and `alembic check` reports no new upgrade operations. The migration job exits 0 before the API starts. The existing Windows PostgreSQL service on 5432 was not modified.

| Executed check | Observed result | Evidence under `artifacts/` |
| --- | --- | --- |
| Real Qwen normal and streaming calls through the container API | Nonempty responses, provider `local`, reported usage, clean terminal streaming event and `[DONE]` | `compose-ollama-verification.json` |
| Audit after gateway restart | All four request/attempt rows for two calls retained | `compose-stack-verification.json` |
| Audit after PostgreSQL server restart | Same four committed rows retained | `compose-stack-verification.json` |
| Redis server restart | Uniquely namespaced policy and settled spending marker retained through AOF recovery | `compose-stack-verification.json` |
| Separate gateway process restart with Redis | Consumed one-request allowance still rejected a new request with 429 | `restart-verification.json` |
| Chat without credentials | HTTP 401 | `compose-stack-verification.json` |
| Prometheus | Gateway scrape target up; four alert rules loaded with health `ok` | `compose-stack-verification.json` |
| Grafana | API healthy; Operations, Business, and Performance dashboards provisioned | `compose-stack-verification.json` |
| Alertmanager / OpenTelemetry | Ready / collector received spans | `compose-stack-verification.json` |

These are controlled local restarts, not power-loss, replica-failover, or Redis Cluster tests. The Redis spending marker uses synthetic amounts to check storage semantics; it is not a paid inference charge. Grafana API/provisioning checks do not establish visual correctness of every panel. Collector log receipt does not establish searchable trace retention. The old 5,000-request mock benchmark below was not repeated against this real model/PostgreSQL stack.

Ollama pricing is zero metered API cost and only one real provider is configured. Consequently these real-model tests do not demonstrate spending-limit rejection at nonzero prices or failover to an independent provider. Mock tests cover those behaviors separately. Request auditing remains best effort at runtime, not a lossless compliance guarantee.

The GitHub Actions workflow now includes a real-storage integration job, but it has not run on GitHub. Its Python 3.11/3.12/3.14 unit-test matrix is intended CI coverage, not a claim that all three versions were tested locally. No hosted-provider call, GitHub repository/PR, external deployment, or Slack notification was created.

Reproduction commands and file explanations are in [the real-stack walkthrough](local-stack-walkthrough.md).

## Historical baseline — 13 October 2026

The initial verification used Python 3.14.6 on this Windows workstation. That version of the local unit/integration suite passed **49 tests**. Ruff checks and formatting passed. The dependency lock files record the resolved versions for the current implementation.

### Original automated coverage

The tests cover both memory state and Redis Lua execution under fakeredis, concurrent admission, idempotent settlement, shared policy updates, circuit cooldown and owned probes, team/admin/metrics credential separation, permitted aliases, normal and streaming responses, fallback, terminal provider errors, cancellation, deadlines, batch isolation, metadata audit, and OpenTelemetry span creation.

The original Redis tests used an emulated Redis server with its Lua engine. The current real-server checks are recorded above; neither set establishes Redis Cluster availability, replica failover, or deployed performance.

### Local HTTP workload

A real local HTTP mock workload used 5,000 requests at concurrency 16, with two team credentials and mixed priorities. Source evidence is `artifacts/load-test-5000-mixed.json`.

| Measurement | Observed value |
| --- | --- |
| Successful HTTP responses | 5,000 of 5,000 |
| Demo team requests | 4,900 |
| Analyst team requests | 100 |
| Wall time | 52.37 seconds |
| Completed throughput | 95.47 requests per second |
| Successful request P50 | 105.89 ms |
| Successful request P95 | 512.45 ms |
| Successful request P99 | 1,247.41 ms |
| Accounted request cost | 1,091,500 micro-USD with fictional mock prices |
| Ledger equality | Exact overall and for both teams |

Latency includes local mock handling, HTTP, queueing, SQLite audit work, and the gateway. No isolated overhead measurement was made, and no sub-10 ms claim is justified. The workload is an infrastructure exercise, not a model-quality evaluation or a universal capacity limit. Background activity and hardware change these results.

The benchmark predates the final metadata-only audit enrichment and the last two test additions; these do not change routing/accounting semantics, but latency should be rerun before comparing releases. Generated reports are ignored by Git so fresh measurements do not become source changes.

### Initial local real-model integration

Ollama was reachable and already contained `qwen3:4b-instruct-2507-q4_K_M`. The optional integration script successfully performed non-streaming and streaming requests with HTTP 200. The non-streaming completion reported 19 input tokens and 32 output tokens. Its finish reason was `length`, as expected for the deliberately small generation limit. Streaming reached its final marker without an error event.

This validates the configured local model path. It does not establish accuracy, faithfulness, or task quality. Local API cost is configured as zero and excludes hardware and electricity. Evidence is `artifacts/ollama-verification.json`.

### Fault demonstrations

Local network demonstrations verified primary-provider failover, team 429 responses, daily-budget rejection, and streaming delivery. The public mock scenarios make these behaviors reproducible without paid credentials. Hosted-provider formats were verified using HTTP transport fixtures; no paid provider calls were made.

### Original deployment limitation, since resolved locally

Docker was unavailable during the original 13 October build, so that deployment configuration was checked structurally only. On 19 October the new real-model stack was launched and verified as recorded above. The original mock-only `compose.yaml` was not the stack used for those current container checks.

GitHub execution, external notifications, production availability, and performance under real multi-user model traffic remain outside the executed verification.
