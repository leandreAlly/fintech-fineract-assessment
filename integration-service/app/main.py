import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app import accounts_sync, admin_api, clients_api, loans_api, reconciliation_api
from app.config import settings
from app.db import engine
from app.errors import COMMON_ERRORS, documented, install_error_handlers
from app.models import GlAccountSync
from app.observability import CorrelationIdMiddleware, configure_logging
from app.security import require_admin_key, require_api_key
from app.worker import retry_loop


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    GlAccountSync.metadata.create_all(engine)
    retries = asyncio.create_task(retry_loop(settings.sync_retry_interval_seconds))
    yield
    retries.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await retries


configure_logging("integration-service")

DESCRIPTION = """
Connects Apache Fineract (core banking) with the ERP (accounting master).

* **Authentication**: send `X-API-Key`. `/api/admin/*` needs an admin key.
* **Idempotency**: every state-changing request carries an `external_reference`.
  The same reference and payload returns the original result (200), a request
  still in progress returns 202, and a different payload returns 409.
* **Correlation**: send `X-Correlation-ID` or one is created; it is returned on
  every response, included in error bodies and in every log line.
* **Errors**: always `{"error": {"code", "message", "correlation_id"}}`.
* Fineract's numeric ids are never accepted or returned.
"""

TAGS = [
    {"name": "gl-account-sync", "description": "Chart of accounts: ERP to Fineract GL sync"},
    {"name": "clients", "description": "SME clients, identified by client_uid"},
    {"name": "loans", "description": "Loan lifecycle, disbursements and repayments"},
    {"name": "reconciliation", "description": "Fineract against the ERP, and explicit repairs"},
    {"name": "admin", "description": "Recovery, pending transactions and failure injection"},
]

app = FastAPI(
    title="Integration Service",
    version="1.0.0",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)
install_error_handlers(app)

for router in (
    accounts_sync.router,
    clients_api.router,
    loans_api.router,
    reconciliation_api.router,
):
    app.include_router(router, dependencies=[Depends(require_api_key)], responses=COMMON_ERRORS)
app.include_router(
    admin_api.router,
    dependencies=[Depends(require_admin_key)],
    responses=COMMON_ERRORS | {403: documented("An admin API key is required")},
)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
