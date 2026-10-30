from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import accounts, journal
from app.db import Base, SessionLocal, engine
from app.observability import CorrelationIdMiddleware, configure_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        accounts.seed_accounts(session)
    yield


configure_logging("mock-erp")

app = FastAPI(title="Mock ERP", lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
app.include_router(accounts.router)
app.include_router(journal.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
