import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import accounts_sync, admin_api, clients_api, loans_api, reconciliation_api
from app.config import settings
from app.db import engine
from app.models import GlAccountSync
from app.observability import CorrelationIdMiddleware, configure_logging
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

app = FastAPI(title="Integration Service", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
app.include_router(accounts_sync.router)
app.include_router(clients_api.router)
app.include_router(loans_api.router)
app.include_router(admin_api.router)
app.include_router(reconciliation_api.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
