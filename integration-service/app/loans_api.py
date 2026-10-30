from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.clients_api import fineract_unavailable, respond
from app.deps import get_loan_service
from app.gateways.fineract import FineractUnavailableError
from app.idempotency import ApiResult
from app.loan_service import (
    ApprovalRequest,
    DisbursementRequest,
    LoanApplicationRequest,
    LoanService,
    RepaymentRequest,
)

router = APIRouter(tags=["loans"])

Service = Annotated[LoanService, Depends(get_loan_service)]


def call(run: Callable[[], ApiResult]) -> JSONResponse:
    try:
        return respond(run())
    except FineractUnavailableError as error:
        return fineract_unavailable(error)


@router.post("/api/loans", status_code=201)
def apply_for_loan(request: LoanApplicationRequest, service: Service) -> JSONResponse:
    return call(lambda: service.apply(request))


@router.get("/api/loans/{loan_reference}")
def get_loan(loan_reference: str, service: Service) -> JSONResponse:
    return call(lambda: service.get(loan_reference))


@router.post("/api/loans/{loan_reference}/approve")
def approve_loan(loan_reference: str, request: ApprovalRequest, service: Service) -> JSONResponse:
    return call(lambda: service.approve(loan_reference, request))


@router.post("/api/loans/{loan_reference}/disburse", status_code=201)
def disburse_loan(
    loan_reference: str, request: DisbursementRequest, service: Service
) -> JSONResponse:
    return call(lambda: service.disburse(loan_reference, request))


@router.post("/api/loans/{loan_reference}/repayments", status_code=201)
def repay_loan(loan_reference: str, request: RepaymentRequest, service: Service) -> JSONResponse:
    return call(lambda: service.repay(loan_reference, request))


@router.get("/api/transactions/{external_reference}")
def get_transaction(external_reference: str, service: Service) -> JSONResponse:
    return call(lambda: service.get_transaction(external_reference))
