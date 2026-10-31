# Fintech Fineract Assessment

This repo connects Apache Fineract (the core banking system) to an ERP. The two have to agree on the general ledger, and money must never be posted twice.

There are three moving parts:

- **Fineract 1.15.0 with a small Java patch.** It adds an idempotent `POST /glaccounts/sync` endpoint, so the ERP can own the chart of accounts and Fineract just mirrors it.
- **Integration Service (Python, FastAPI).** This is the API a teller app or partner system calls. It creates clients and loans in Fineract, moves loans through their lifecycle, and posts the accounting for every disbursement and repayment to the ERP. Duplicates, crashes, outages and reconciliation are handled here.
- **Mock ERP (Python, FastAPI).** It stands in for the real ERP: chart of accounts, balanced journal entries and account balances. It enforces its own uniqueness and doesn't trust the caller.

Everything runs locally with Docker Compose.

## Tiers completed

| Tier | Parts | Status |
|---|---|---|
| 1. Core | A–G | Done |
| 2. Reliability | H–K | Done |
| 3. Stretch | L | Partly, see below |

For Part L I didn't build anything separate. A few things on the list came naturally while doing H–K:

- fees, penalties and overpayments in the accounting mapping
- repayment reversals, mirrored in the ERP during reconciliation repair
- a view of stuck transactions (`GET /api/admin/transactions?status=NEEDS_RECONCILIATION`), which works like a small dead-letter queue

Reconciliation runs on demand, not on a schedule, and there are no Prometheus metrics or webhooks.

## Pinned versions

| Component | Version |
|---|---|
| Apache Fineract | tag `1.15.0` (upstream commit `d563684`) |
| JDK | Eclipse Temurin 21.0.11 |
| Gradle | 8.14.5 (Fineract's wrapper) |
| PostgreSQL | 18.3 |
| Python | 3.12, dependencies locked with uv (`uv.lock` in each service) |
| FastAPI, SQLAlchemy, httpx, structlog | minimums in `pyproject.toml`, exact versions in `uv.lock` |
| Lint and types | ruff, mypy in strict mode |

## Getting started

You need Docker, `make`, `curl` and `jq`. For the Python tests you also need [uv](https://docs.astral.sh/uv/), and for the Java tests JDK 21.

```bash
make up       # docker compose up --build -d
make setup    # configure Fineract: RWF, Kigali office, API user, GL accounts, loan product
```

The first `make up` compiles Fineract from source with the patch applied. That takes 10 to 15 minutes; after that the Gradle cache makes it much faster. The integration service waits until Fineract is healthy before it starts.

`make setup` only needs to run once per fresh database. It's safe to run again.

| Service | URL |
|---|---|
| Fineract | https://localhost:8443/fineract-provider/api/v1 (self-signed cert, tenant `default`) |
| Integration Service | http://localhost:8080, Swagger UI at `/docs` |
| Mock ERP | http://localhost:8081, Swagger UI at `/docs` |

### API keys

Every `/api/*` call on the integration service needs an `X-API-Key` header. The local defaults are:

- `dev-teller-key` for normal calls
- `dev-admin-key` for `/api/admin/*` (recovery, stuck transactions, fault injection)

You can change them in `.env` (see `.env.example`).

```bash
curl -s localhost:8080/api/accounts/sync -H 'X-API-Key: dev-teller-key' | jq
```

### Starting over

```bash
make reset    # docker compose down -v, deletes every database
make up && make setup
```

## Tests

One command per language:

| What | Command | Notes |
|---|---|---|
| Python, both services | `make py-lint py-test` | ruff, ruff format, mypy, then pytest. 250 tests in the integration service, 32 in the mock ERP |
| Java unit tests | `make fineract-checkout fineract-test` | JUnit tests for the GL sync handler |
| Java integration test | `make fineract-it` | runs against the Fineract in Docker, so `make up` first |
| Parallel and concurrency (Part I) | `make test-live` | Python tests that fire parallel requests at the running stack |

`make test` runs lint, the Python tests and the Java unit tests together.

## Simulations

Each part has a script that prints the state before and after, so you can see what happened, not just a pass or fail. The output from my last run is in `evidence/`, and `SIMULATIONS.md` walks through each one.

| Command | Shows |
|---|---|
| `make demo-a` | Part A: the sync endpoint in Fineract (persisted, unique, 409 on a different payload, permission check) |
| `make demo-a-sync` | Part A: the Python side of GL sync, including Fineract going down mid-sync |
| `make demo-clients` | Part C: creating and finding clients, validation, idempotency |
| `make demo-loan` | Part D: a loan from application to closed |
| `make demo-erp` | Part G: journal entries and balances in the mock ERP |
| `make test-live` | Part I: duplicate and concurrent requests |
| `make demo-failures` | Part J: five failure scenarios. **This stops containers and kills the integration service on purpose** |
| `make demo-reconciliation` | Part K: every reconciliation outcome, then a repair |
| `make demo-trace` | Following one repayment through the logs |

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md): components, data flow, data model, state machine, GL sync design
- [API.md](API.md): endpoints, examples, errors and the idempotency contract
- [SIMULATIONS.md](SIMULATIONS.md): every simulation in the evidence template
- [DECISIONS.md](DECISIONS.md): why I built things the way I did, what I left out, known weaknesses
- `docs/openapi.json`: the integration service's OpenAPI spec (`make openapi` regenerates it)

## Repository layout

```
fineract-customization/   Dockerfile and patches/erp-gl-sync.patch (diff against tag 1.15.0)
integration-service/      app/, tests/ (unit), tests_live/ (against the running stack), Dockerfile
mock-erp/                 app/, tests/, Dockerfile
fineract/configuration/   JSON used by make setup (accounting mapping, loan product, API user permissions)
scripts/                  setup, simulations, Fineract checkout/patch helpers, DB init
evidence/                 test output and simulation logs, one folder per part
docs/                     exported OpenAPI spec
```

## Working on the Fineract patch

The Fineract source isn't committed, only the patch.

1. `make fineract-checkout` clones tag 1.15.0 into `fineract-customization/fineract` (git-ignored) and applies the patch.
2. Change the Java code there and run `make fineract-test`.
3. `make fineract-patch` writes your changes back to `fineract-customization/patches/erp-gl-sync.patch`. That file is what gets committed.
4. `make fineract-image` rebuilds the Docker image with the new patch.

## Use of AI tools

_To be written._
