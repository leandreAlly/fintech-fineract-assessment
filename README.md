# Fintech Fineract Assessment

Apache Fineract (Java/Spring Boot) customization for idempotent ERP GL-account sync, plus a Python Mock ERP and Integration Service, run together with Docker Compose.

> Work in progress. Tiers completed: _none yet_.

## Pinned versions

| Component | Version |
|---|---|
| Apache Fineract | tag `1.15.0` (upstream commit `d563684`) |
| JDK | Eclipse Temurin / OpenJDK 21.0.11 |
| Build tool | Gradle 8.14.5 (Fineract wrapper) |
| Database | PostgreSQL 18.3 |
| Python | 3.12, dependencies locked with uv 0.13.5 (`uv.lock`) |
| Python web framework | FastAPI (generated OpenAPI docs at `/docs`) |

## Quick start

```bash
docker compose up --build      # or: make up
```

The first build compiles Fineract from source and takes about 10–15 minutes; later builds reuse the Gradle cache.

| Service | URL |
|---|---|
| Fineract | https://localhost:8443/fineract-provider/api/v1 (self-signed TLS, tenant `default`) |
| Integration Service | http://localhost:8080 (OpenAPI: `/docs`) |
| Mock ERP | http://localhost:8081 (OpenAPI: `/docs`) |

`make reset` deletes all data so Fineract starts from an empty database.

## Tests

| Language | Command |
|---|---|
| Python (both services) | `make py-lint py-test` |
| Java (Fineract customization) | `make fineract-checkout fineract-test` |

## Repository layout

```
fineract-customization/   Dockerfile, patches/erp-gl-sync.patch (diff against the 1.15.0 tag)
integration-service/      Python: partner API, sync, idempotency, recovery, reconciliation
mock-erp/                 Python: chart of accounts, journal entries
fineract/configuration/   scripted Fineract setup (office, product, accounting, API user)
scripts/                  checkout/patch helpers, DB init, simulations
evidence/                 build logs and simulation output
```

## Fineract development loop

1. `make fineract-checkout` clones tag 1.15.0 into `fineract-customization/fineract` (git-ignored) and applies the patch.
2. Change Java code there, run `make fineract-test`.
3. `make fineract-patch` writes the diff to `fineract-customization/patches/erp-gl-sync.patch`, which is what we commit.
4. `make fineract-image` rebuilds the image with the patch applied.
