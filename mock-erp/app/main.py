from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import accounts
from app.db import Base, SessionLocal, engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        accounts.seed_accounts(session)
    yield


app = FastAPI(title="Mock ERP", lifespan=lifespan)
app.include_router(accounts.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
