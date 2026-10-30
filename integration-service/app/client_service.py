from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.gateways.fineract import FineractRejectedError
from app.idempotency import ApiResult, Finished, IdempotencyStore, error_body
from app.models import Client

CREATE_CLIENT = "CREATE_CLIENT"

log = structlog.get_logger()


class CreateClientRequest(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    client_uid: str = Field(pattern=r"^SME-[0-9]{6}$")
    name: str = Field(min_length=1, max_length=100)


class FineractClients(Protocol):
    def office_id_by_external_id(self, external_id: str) -> int | None: ...

    def create_client(self, payload: dict[str, Any]) -> int: ...

    def client_by_external_id(self, external_id: str) -> dict[str, Any] | None: ...

    def client(self, client_id: int) -> dict[str, Any]: ...


class OfficeNotConfiguredError(Exception):
    pass


def client_uid_conflict(client_uid: str) -> ApiResult:
    return ApiResult(
        409,
        error_body("CLIENT_UID_CONFLICT", f"client_uid {client_uid} already belongs to a client"),
    )


def fineract_date(value: Any) -> str | None:
    if isinstance(value, list) and len(value) == 3:
        return date(*value).isoformat()
    return None


class ClientService:
    def __init__(
        self,
        session: Session,
        fineract: FineractClients,
        office_external_id: str,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.session = session
        self.fineract = fineract
        self.office_external_id = office_external_id
        self.today = today
        self.idempotency = IdempotencyStore(session)

    def create(self, request: CreateClientRequest) -> ApiResult:
        outcome = self.idempotency.begin(
            CREATE_CLIENT, request.external_reference, request.model_dump()
        )
        if isinstance(outcome, Finished):
            return outcome.result
        try:
            result = self._create(request)
        except Exception:
            self.idempotency.release(outcome.record)
            raise
        self.idempotency.complete(outcome.record, result)
        return result

    def get(self, client_uid: str) -> ApiResult:
        client = self.find(client_uid)
        if client is None:
            return ApiResult(404, error_body("CLIENT_NOT_FOUND", f"no client {client_uid}"))
        try:
            fineract_client: dict[str, Any] | None = self.fineract.client(client.fineract_client_id)
        except FineractRejectedError as error:
            if error.status_code != 404:
                raise
            fineract_client = None
        if fineract_client is None:
            return ApiResult(
                200,
                {
                    "client_uid": client.client_uid,
                    "name": client.name,
                    "checks": {"exists": False, "active": False, "name_matches": False},
                },
            )
        active = bool(fineract_client.get("active"))
        fineract_name = fineract_client.get("displayName")
        return ApiResult(
            200,
            {
                "client_uid": client.client_uid,
                "external_reference": client.external_reference,
                "name": fineract_name,
                "status": str(fineract_client.get("status", {}).get("value", "")).upper(),
                "office": fineract_client.get("officeName"),
                "activation_date": fineract_date(fineract_client.get("activationDate")),
                "checks": {
                    "exists": True,
                    "active": active,
                    "name_matches": fineract_name == client.name,
                },
            },
        )

    def find(self, client_uid: str) -> Client | None:
        return self.session.scalar(select(Client).where(Client.client_uid == client_uid))

    def _create(self, request: CreateClientRequest) -> ApiResult:
        if self.find(request.client_uid) is not None:
            return client_uid_conflict(request.client_uid)
        existing = self.fineract.client_by_external_id(request.client_uid)
        if existing is not None:
            if existing.get("displayName") != request.name:
                return client_uid_conflict(request.client_uid)
            fineract_client_id = int(existing["id"])
        else:
            try:
                fineract_client_id = self.fineract.create_client(self._fineract_payload(request))
            except FineractRejectedError as error:
                if self.fineract.client_by_external_id(request.client_uid) is not None:
                    return client_uid_conflict(request.client_uid)
                return ApiResult(422, error_body("FINERACT_REJECTED", error.message))
        client = Client(
            client_uid=request.client_uid,
            external_reference=request.external_reference,
            name=request.name,
            fineract_client_id=fineract_client_id,
        )
        self.session.add(client)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            return client_uid_conflict(request.client_uid)
        log.info(
            "client_created",
            external_reference=request.external_reference,
            client_uid=client.client_uid,
            adopted_existing=existing is not None,
        )
        return ApiResult(
            201,
            {
                "client_uid": client.client_uid,
                "external_reference": client.external_reference,
                "name": client.name,
                "status": "ACTIVE",
            },
        )

    def _fineract_payload(self, request: CreateClientRequest) -> dict[str, Any]:
        office_id = self.fineract.office_id_by_external_id(self.office_external_id)
        if office_id is None:
            raise OfficeNotConfiguredError(f"Fineract office {self.office_external_id} not found")
        today = self.today().isoformat()
        return {
            "officeId": office_id,
            "legalFormId": 2,
            "fullname": request.name,
            "externalId": request.client_uid,
            "active": True,
            "activationDate": today,
            "submittedOnDate": today,
            "dateFormat": "yyyy-MM-dd",
            "locale": "en",
        }
