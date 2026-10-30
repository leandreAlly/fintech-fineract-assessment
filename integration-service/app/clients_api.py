from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.client_service import ClientService, CreateClientRequest
from app.deps import get_client_service
from app.gateways.fineract import FineractUnavailableError
from app.idempotency import ApiResult, error_body

router = APIRouter(prefix="/api/clients", tags=["clients"])

Service = Annotated[ClientService, Depends(get_client_service)]


def respond(result: ApiResult) -> JSONResponse:
    return JSONResponse(status_code=result.status_code, content=result.body)


def fineract_unavailable(error: FineractUnavailableError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=error_body("FINERACT_UNAVAILABLE", f"Fineract is unavailable: {error.message}"),
    )


@router.post("", status_code=201)
def create_client(request: CreateClientRequest, service: Service) -> JSONResponse:
    try:
        return respond(service.create(request))
    except FineractUnavailableError as error:
        return fineract_unavailable(error)


@router.get("/{client_uid}")
def get_client(client_uid: str, service: Service) -> JSONResponse:
    try:
        return respond(service.get(client_uid))
    except FineractUnavailableError as error:
        return fineract_unavailable(error)
