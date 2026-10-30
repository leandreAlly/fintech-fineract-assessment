from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.deps import get_sync_service
from app.gateways.erp import ErpUnavailableError
from app.gl_sync import AccountNotFoundError, GlSyncService
from app.models import GlAccountSync, SyncStatus

router = APIRouter(prefix="/api/accounts/sync", tags=["gl-account-sync"])

STATUS_CODES = {
    SyncStatus.SYNCED: status.HTTP_200_OK,
    SyncStatus.SYNC_PENDING: status.HTTP_202_ACCEPTED,
    SyncStatus.FAILED: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


class SyncRequest(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)


class SyncOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    external_reference: str
    erp_code: str
    erp_name: str
    erp_type: str
    status: SyncStatus
    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    updated_at: datetime


Service = Annotated[GlSyncService, Depends(get_sync_service)]


@router.post("", response_model=SyncOut)
def sync_account(body: SyncRequest, response: Response, service: Service) -> GlAccountSync:
    try:
        row = service.sync(body.external_reference)
    except AccountNotFoundError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except ErpUnavailableError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ERP is unavailable") from error
    response.status_code = STATUS_CODES[row.status]
    return row


@router.post("/all", response_model=list[SyncOut])
def sync_all_accounts(service: Service) -> list[GlAccountSync]:
    try:
        return service.sync_all()
    except ErpUnavailableError as error:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ERP is unavailable") from error


@router.get("", response_model=list[SyncOut])
def list_sync_status(service: Service) -> list[GlAccountSync]:
    return list(service.session.scalars(select(GlAccountSync).order_by(GlAccountSync.erp_code)))


@router.get("/{external_reference}", response_model=SyncOut)
def get_sync_status(external_reference: str, service: Service) -> GlAccountSync:
    row = service.find(external_reference)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{external_reference} was never synced")
    return row
