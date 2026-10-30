from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.deps import get_loan_service
from app.gateways.fineract import FineractUnavailableError
from app.loan_service import LoanService
from app.main import app
from app.models import Client
from tests.fakes import FakeFineractLoans


@pytest.fixture
def fineract() -> FakeFineractLoans:
    return FakeFineractLoans()


@pytest.fixture
def client(session: Session, fineract: FakeFineractLoans) -> Iterator[TestClient]:
    session.add(
        Client(client_uid="SME-000001", external_reference="C", name="U", fineract_client_id=7)
    )
    session.commit()
    service = LoanService(
        session,
        fineract,
        product_external_id="LP-SME-ASSET-FINANCE",
        payment_type_id=1,
        today=lambda: date(2026, 10, 30),
    )
    app.dependency_overrides[get_loan_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_lifecycle_endpoints(client: TestClient) -> None:
    created = client.post(
        "/api/loans",
        json={"external_reference": "APP-1", "client_uid": "SME-000001", "principal": 1_000_000},
    )
    loan = created.json()["loan_reference"]

    assert created.status_code == 201
    assert (
        client.post(f"/api/loans/{loan}/approve", json={"external_reference": "A"}).status_code
        == 200
    )
    assert (
        client.post(f"/api/loans/{loan}/disburse", json={"external_reference": "D"}).status_code
        == 201
    )
    repaid = client.post(
        f"/api/loans/{loan}/repayments", json={"external_reference": "R", "amount": 100_000}
    )
    assert repaid.status_code == 201
    assert client.get("/api/transactions/R").json()["loan_reference"] == loan
    assert client.get(f"/api/loans/{loan}").json()["status"] == "ACTIVE"


def test_fineract_down_returns_503(client: TestClient, fineract: FakeFineractLoans) -> None:
    fineract.fail_before["create_loan"] = FineractUnavailableError("ConnectError: refused")

    response = client.post(
        "/api/loans",
        json={"external_reference": "APP-1", "client_uid": "SME-000001", "principal": 1},
    )

    assert response.status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"external_reference": "APP-1", "client_uid": "SME-000001", "principal": 0},
        {"external_reference": "", "client_uid": "SME-000001", "principal": 1},
        {"external_reference": "APP-1", "client_uid": "bad", "principal": 1},
    ],
)
def test_invalid_application_is_422(client: TestClient, body: dict[str, object]) -> None:
    assert client.post("/api/loans", json=body).status_code == 422
