from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.client_service import ClientService, CreateClientRequest
from app.gateways.fineract import FineractRejectedError, FineractUnavailableError
from app.models import Client, IdempotencyRecord
from tests.fakes import FakeFineractClients


@pytest.fixture
def fineract() -> FakeFineractClients:
    return FakeFineractClients()


@pytest.fixture
def service(session: Session, fineract: FakeFineractClients) -> ClientService:
    return ClientService(session, fineract, "OFFICE-KGL-MAIN", today=lambda: date(2026, 10, 30))


def request(**overrides: Any) -> CreateClientRequest:
    body = {"external_reference": "CLI-REF-1", "client_uid": "SME-000001", "name": "Umucyo Traders"}
    return CreateClientRequest.model_validate(body | overrides)


def count(session: Session, model: type[Client] | type[IdempotencyRecord]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_create_client_in_fineract_with_client_uid_as_external_id(
    service: ClientService, fineract: FakeFineractClients
) -> None:
    result = service.create(request())

    assert result.status_code == 201
    assert result.body == {
        "client_uid": "SME-000001",
        "external_reference": "CLI-REF-1",
        "name": "Umucyo Traders",
        "status": "ACTIVE",
    }
    assert fineract.create_calls == [
        {
            "officeId": 2,
            "legalFormId": 2,
            "fullname": "Umucyo Traders",
            "externalId": "SME-000001",
            "active": True,
            "activationDate": "2026-10-30",
            "submittedOnDate": "2026-10-30",
            "dateFormat": "yyyy-MM-dd",
            "locale": "en",
        }
    ]


def test_same_request_four_times_creates_one_client(
    service: ClientService, session: Session, fineract: FakeFineractClients
) -> None:
    results = [service.create(request()) for _ in range(4)]

    assert [r.status_code for r in results] == [201, 200, 200, 200]
    assert all(r.body == results[0].body for r in results)
    assert len(fineract.create_calls) == 1
    assert count(session, Client) == 1


def test_same_reference_different_payload_is_409(service: ClientService) -> None:
    service.create(request())

    result = service.create(request(name="Umucyo Traders Ltd"))

    assert result.status_code == 409
    assert result.body["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_new_reference_reusing_client_uid_is_409(
    service: ClientService, session: Session, fineract: FakeFineractClients
) -> None:
    service.create(request())

    result = service.create(request(external_reference="CLI-REF-2"))

    assert result.status_code == 409
    assert result.body["error"]["code"] == "CLIENT_UID_CONFLICT"
    assert len(fineract.create_calls) == 1
    assert count(session, Client) == 1


def test_new_reference_and_new_client_uid_creates_another_client(
    service: ClientService, session: Session
) -> None:
    service.create(request())

    result = service.create(
        request(external_reference="CLI-REF-2", client_uid="SME-000002", name="Inzozi Bakery")
    )

    assert result.status_code == 201
    assert count(session, Client) == 2


def test_fineract_down_persists_nothing_and_is_safe_to_retry(
    service: ClientService, session: Session, fineract: FakeFineractClients
) -> None:
    fineract.failures = [FineractUnavailableError("ConnectError: connection refused")]

    with pytest.raises(FineractUnavailableError):
        service.create(request())

    assert count(session, IdempotencyRecord) == 0
    assert count(session, Client) == 0
    assert service.create(request()).status_code == 201


def test_timeout_after_fineract_created_client_is_recovered_by_lookup(
    service: ClientService, session: Session, fineract: FakeFineractClients
) -> None:
    fineract.fail_after_create = FineractUnavailableError("ReadTimeout: timed out")

    with pytest.raises(FineractUnavailableError):
        service.create(request())
    result = service.create(request())

    assert result.status_code == 201
    assert len(fineract.create_calls) == 1
    assert len(fineract.clients) == 1
    assert count(session, Client) == 1


def test_client_uid_taken_in_fineract_by_another_name_is_409(
    service: ClientService, fineract: FakeFineractClients
) -> None:
    fineract.create_client({"externalId": "SME-000001", "fullname": "Someone Else"})
    fineract.create_calls.clear()

    result = service.create(request())

    assert result.status_code == 409
    assert fineract.create_calls == []


def test_fineract_validation_error_is_422_and_replayed(
    service: ClientService, fineract: FakeFineractClients
) -> None:
    fineract.failures = [FineractRejectedError("activation date is in the future", 400)]

    first = service.create(request())
    replay = service.create(request())

    assert first.status_code == replay.status_code == 422
    assert first.body == replay.body
    assert first.body["error"]["code"] == "FINERACT_REJECTED"
    assert len(fineract.create_calls) == 1


def test_get_validates_exists_active_and_name(service: ClientService) -> None:
    service.create(request())

    result = service.get("SME-000001")

    assert result.status_code == 200
    assert result.body["status"] == "ACTIVE"
    assert result.body["office"] == "Kigali Main Office"
    assert result.body["activation_date"] == "2026-10-30"
    assert result.body["checks"] == {"exists": True, "active": True, "name_matches": True}
    assert "id" not in result.body and "fineract_client_id" not in result.body


def test_get_reports_inactive_or_renamed_client(
    service: ClientService, fineract: FakeFineractClients
) -> None:
    service.create(request())
    fineract.clients["SME-000001"] |= {"active": False, "displayName": "Renamed"}

    checks = service.get("SME-000001").body["checks"]

    assert checks == {"exists": True, "active": False, "name_matches": False}


def test_get_reports_client_missing_in_fineract(
    service: ClientService, fineract: FakeFineractClients
) -> None:
    service.create(request())
    fineract.clients.clear()

    assert service.get("SME-000001").body["checks"]["exists"] is False


def test_get_unknown_client_uid_is_404(service: ClientService) -> None:
    assert service.get("SME-999999").status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [{"client_uid": "SME-1"}, {"client_uid": "000001"}, {"name": ""}, {"external_reference": ""}],
)
def test_invalid_request_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        request(**overrides)
