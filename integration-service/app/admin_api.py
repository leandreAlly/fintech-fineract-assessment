from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.deps import get_loan_service
from app.faults import Fault, injector
from app.idempotency import error_body
from app.loan_service import LoanService, transaction_body
from app.models import LoanTransaction, TransactionStatus

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/recovery")
def run_recovery(service: Annotated[LoanService, Depends(get_loan_service)]) -> JSONResponse:
    return JSONResponse(status_code=200, content=service.recover())


@router.get("/transactions")
def list_transactions(
    session: Annotated[Session, Depends(get_session)],
    status: Annotated[list[TransactionStatus] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    query = select(LoanTransaction).order_by(LoanTransaction.id.desc()).limit(limit)
    if status:
        query = query.where(LoanTransaction.status.in_(status))
    return [transaction_body(txn) for txn in session.scalars(query)]


def faults_disabled() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content=error_body(
            "FAULT_INJECTION_DISABLED", "set FAULT_INJECTION_ENABLED=true to inject failures"
        ),
    )


def active_faults() -> list[dict[str, Any]]:
    return [fault.model_dump(mode="json") for fault in injector.active()]


@router.post("/faults", status_code=status.HTTP_201_CREATED)
def arm_fault(fault: Fault) -> JSONResponse:
    if not settings.fault_injection_enabled:
        return faults_disabled()
    injector.arm(fault)
    return JSONResponse(status_code=201, content=active_faults())


@router.get("/faults")
def list_faults() -> JSONResponse:
    if not settings.fault_injection_enabled:
        return faults_disabled()
    return JSONResponse(status_code=200, content=active_faults())


@router.delete("/faults", status_code=status.HTTP_204_NO_CONTENT)
def clear_faults() -> Response:
    if not settings.fault_injection_enabled:
        return faults_disabled()
    injector.clear()
    return Response(status_code=204)
