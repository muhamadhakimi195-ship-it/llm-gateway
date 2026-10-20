# Sources and attribution

The project choice is based on **Project 11, LLM Gateway with Rate Limiting, Fallback Routing, and Observability**, in the user-supplied `BASWE_15_AI_Engineering_Projects_Guide.pdf`, pages 31 through 34. The guide is a source blueprint. Its promotional material, publishing suggestions, and claims about jobs or latency are not implementation requirements or measured results.

Primary technical references used while implementing and reviewing this project:

- [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/): resource ownership at service startup and shutdown.
- [HTTPX timeouts](https://www.python-httpx.org/advanced/timeouts/): bounded connect, pool, read, and write behavior.
- [Redis Lua scripting](https://redis.io/docs/latest/develop/programmability/eval-intro/): atomic script execution and explicit key arguments.
- [Ollama chat protocol](https://docs.ollama.com/api/chat): non-streaming responses, NDJSON streaming, options, and usage fields.
- [Pydantic models](https://pydantic.dev/docs/validation/latest/concepts/models/): strict model validation and nested contracts.
- [OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/): spans and OTLP export.
- [Prometheus instrumentation](https://prometheus.io/docs/practices/instrumentation/): counters, gauges, and histogram metrics.
- [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/): reproducible data sources and dashboard loading.
- [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html): async engines, connection ownership, transactions, and disposal.
- [SQLAlchemy Psycopg dialect](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg): PostgreSQL driver selection.
- [Psycopg async operations](https://www.psycopg.org/psycopg3/docs/advanced/async.html): Windows SelectorEventLoop requirement.
- [Alembic asyncio cookbook](https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic): versioned migrations with an async connection.
- [Docker Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/): health and migration completion dependencies.
- [Docker Desktop host networking](https://docs.docker.com/desktop/features/networking/networking-how-tos/): host.docker.internal for the Windows Ollama service.
- [PostgreSQL official container image](https://hub.docker.com/_/postgres): PostgreSQL 18 volume parent-directory change.
- [Anthropic Messages](https://platform.claude.com/docs/en/api/messages): intended public protocol reference. Its full live documentation could not be fetched by the web reader during this build; this adapter was verified using protocol fixtures, not a paid live API call.

HTTP provider fixtures establish protocol behavior, not a vendor's current commercial model availability or prices. Replace template model IDs and prices using the documentation and account information for your deployment.
