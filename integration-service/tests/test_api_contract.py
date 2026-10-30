from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.deps import get_loan_service
from app.loan_service import LoanService
from app.main import app
from app.models import Client
from tests.fakes import FakeErpJournal, FakeFineractLoans

pytestmark = pytest.mark.auth

TELLER = {"X-API-Key": "teller-key"}
ADMIN = {"X-API-Key": "admin-key"}


@pytest.fixture
def client(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "api_keys", "teller-key, other-key")
    monkeypatch.setattr(settings, "admin_api_keys", "admin-key")
    session.add(
        Client(client_uid="SME-000001", external_reference="C", name="U", fineract_client_id=7)
    )
    session.commit()
    service = LoanService(
        session,
        FakeFineractLoans(),
        FakeErpJournal(),
        product_external_id="LP-SME-ASSET-FINANCE",
        payment_type_id=1,
        today=lambda: date(2026, 10, 30),
    )
    app.dependency_overrides[get_loan_service] = lambda: service
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def application(reference: str = "APP-1") -> dict[str, object]:
    return {"external_reference": reference, "client_uid": "SME-000001", "principal": 1_000_000}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_requests_without_a_valid_key_are_401(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/api/loans", json=application(), headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["WWW-Authenticate"] == "ApiKey"


@pytest.mark.parametrize("headers", [TELLER, {"X-API-Key": "other-key"}, ADMIN])
def test_any_configured_key_is_accepted(client: TestClient, headers: dict[str, str]) -> None:
    assert client.post("/api/loans", json=application(), headers=headers).status_code == 201


def test_no_configured_keys_means_every_request_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "api_keys", "")
    monkeypatch.setattr(settings, "admin_api_keys", "")

    assert client.post("/api/loans", json=application(), headers=TELLER).status_code == 401


def test_admin_endpoints_need_an_admin_key(client: TestClient) -> None:
    teller = client.get("/api/admin/transactions", headers=TELLER)

    assert teller.status_code == 403
    assert teller.json()["error"]["code"] == "FORBIDDEN"
    assert client.get("/api/admin/transactions", headers=ADMIN).status_code == 200


def test_health_and_docs_are_open(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_validation_errors_use_the_common_format(client: TestClient) -> None:
    response = client.post(
        "/api/loans",
        json={"external_reference": "", "client_uid": "bad", "principal": -5},
        headers=TELLER | {"X-Correlation-ID": "req-1"},
    )

    error = response.json()["error"]
    assert response.status_code == 422
    assert error["code"] == "VALIDATION_ERROR"
    assert error["correlation_id"] == "req-1"
    assert {d["field"] for d in error["details"]} == {
        "body.external_reference",
        "body.client_uid",
        "body.principal",
    }


def test_unknown_route_uses_the_common_format(client: TestClient) -> None:
    response = client.get("/api/nope", headers=TELLER)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_business_errors_carry_the_correlation_id(client: TestClient) -> None:
    response = client.get(
        "/api/loans/LN-2026-999999", headers=TELLER | {"X-Correlation-ID": "req-2"}
    )

    assert response.json()["error"] == {
        "code": "LOAN_NOT_FOUND",
        "message": "no loan LN-2026-999999",
        "correlation_id": "req-2",
    }


def test_unexpected_errors_are_500_with_the_common_format(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object) -> None:
        raise RuntimeError("database exploded")

    monkeypatch.setattr(LoanService, "get", boom)

    response = client.get("/api/loans/LN-2026-000001", headers=TELLER | {"X-Correlation-ID": "r3"})

    assert response.status_code == 500
    assert response.headers["X-Correlation-ID"] == "r3"
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["correlation_id"] == "r3"
    assert "exploded" not in response.text


def test_openapi_documents_the_api_key_and_error_format(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    assert spec["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "API key issued to the calling system. Admin endpoints need an admin key.",
    }
    repayments = spec["paths"]["/api/loans/{loan_reference}/repayments"]["post"]
    assert repayments["security"] == [{"APIKeyHeader": []}]
    assert {"401", "409", "422", "503"} <= set(repayments["responses"])
    assert "ErrorResponse" in spec["components"]["schemas"]
