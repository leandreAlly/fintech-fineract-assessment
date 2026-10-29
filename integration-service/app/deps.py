from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.clients.erp import ErpClient, build_erp_client
from app.clients.fineract import FineractClient, build_fineract_client
from app.config import settings
from app.db import get_session
from app.gl_sync import GlSyncService


def get_erp_client() -> Iterator[ErpClient]:
    client = build_erp_client()
    with client.http:
        yield client


def get_fineract_client() -> Iterator[FineractClient]:
    client = build_fineract_client()
    with client.http:
        yield client


def build_sync_service(session: Session, erp: ErpClient, fineract: FineractClient) -> GlSyncService:
    return GlSyncService(
        session,
        erp,
        fineract,
        max_attempts=settings.sync_max_attempts,
        backoff_base_seconds=settings.sync_backoff_base_seconds,
        backoff_max_seconds=settings.sync_backoff_max_seconds,
    )


def get_sync_service(
    session: Annotated[Session, Depends(get_session)],
    erp: Annotated[ErpClient, Depends(get_erp_client)],
    fineract: Annotated[FineractClient, Depends(get_fineract_client)],
) -> GlSyncService:
    return build_sync_service(session, erp, fineract)
