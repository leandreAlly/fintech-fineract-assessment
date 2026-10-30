from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import get_loan_service
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
