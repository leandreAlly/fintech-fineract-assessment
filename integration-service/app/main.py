from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import engine
from app.models import GlAccountSync


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    GlAccountSync.metadata.create_all(engine)
    yield


app = FastAPI(title="Integration Service", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
