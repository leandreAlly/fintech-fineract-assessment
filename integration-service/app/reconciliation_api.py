from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.clients_api import fineract_unavailable, respond
from app.deps import get_reconciliation_service
from app.gateways.erp import ErpUnavailableError
from app.gateways.fineract import FineractUnavailableError
from app.idempotency import error_body
from app.reconciliation import ReconciliationRequest, ReconciliationService, RepairRequest

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])

Service = Annotated[ReconciliationService, Depends(get_reconciliation_service)]


def erp_unavailable(error: ErpUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=503, content=error_body("ERP_UNAVAILABLE", f"ERP is unavailable: {error}")
    )


@router.post("")
def run_reconciliation(request: ReconciliationRequest, service: Service) -> Any:
    try:
        return service.run(request)
    except FineractUnavailableError as error:
        return fineract_unavailable(error)
    except ErpUnavailableError as error:
        return erp_unavailable(error)


@router.post("/repairs")
def repair(request: RepairRequest, service: Service) -> JSONResponse:
    try:
        return respond(service.repair(request))
    except FineractUnavailableError as error:
        return fineract_unavailable(error)
    except ErpUnavailableError as error:
        return erp_unavailable(error)
