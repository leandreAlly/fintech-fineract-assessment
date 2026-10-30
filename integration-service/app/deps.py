from collections.abc import Iterator
from datetime import timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.client_service import ClientService
from app.config import settings
from app.db import engine, get_session
from app.gateways.erp import ErpClient, build_erp_client
from app.gateways.fineract import FineractClient, build_fineract_client
from app.gl_sync import GlSyncService
from app.loan_locks import PostgresLoanLocks
from app.loan_service import LoanService
from app.retry import RetryPolicy


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


def get_client_service(
    session: Annotated[Session, Depends(get_session)],
    fineract: Annotated[FineractClient, Depends(get_fineract_client)],
) -> ClientService:
    return ClientService(session, fineract, settings.fineract_office_external_id)


def build_loan_service(session: Session, fineract: FineractClient, erp: ErpClient) -> LoanService:
    return LoanService(
        session,
        fineract,
        erp,
        product_external_id=settings.fineract_loan_product_external_id,
        payment_type_id=settings.fineract_payment_type_id,
        currency=settings.currency,
        locks=PostgresLoanLocks(engine, settings.loan_lock_timeout_seconds),
        retry=RetryPolicy(
            max_attempts=settings.erp_max_attempts,
            backoff_base_seconds=settings.erp_backoff_base_seconds,
            backoff_max_seconds=settings.erp_backoff_max_seconds,
            abandon_after_seconds=settings.recovery_abandon_after_seconds,
        ),
        idempotency_lease=timedelta(seconds=settings.idempotency_lease_seconds),
    )


def get_loan_service(
    session: Annotated[Session, Depends(get_session)],
    fineract: Annotated[FineractClient, Depends(get_fineract_client)],
    erp: Annotated[ErpClient, Depends(get_erp_client)],
) -> LoanService:
    return build_loan_service(session, fineract, erp)
