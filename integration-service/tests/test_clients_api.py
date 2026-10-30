from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.client_service import ClientService
from app.deps import get_client_service
from app.main import app
from tests.fakes import FakeFineractClients

BODY = {"external_reference": "CLI-REF-1", "client_uid": "SME-000001", "name": "Umucyo Traders"}


@pytest.fixture
def fineract() -> FakeFineractClients:
    return FakeFineractClients()


@pytest.fixture
def client(session: Session, fineract: FakeFineractClients) -> Iterator[TestClient]:
    service = ClientService(session, fineract, "OFFICE-KGL-MAIN", today=lambda: date(2026, 10, 30))
    app.dependency_overrides[get_client_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_create_then_replay(client: TestClient) -> None:
    first = client.post("/api/clients", json=BODY)
    second = client.post("/api/clients", json=BODY)

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()


def test_get_client(client: TestClient) -> None:
    client.post("/api/clients", json=BODY)

    response = client.get("/api/clients/SME-000001")

    assert response.status_code == 200
    assert response.json()["checks"] == {"exists": True, "active": True, "name_matches": True}


def test_fineract_down_returns_503(client: TestClient, fineract: FakeFineractClients) -> None:
    fineract.unavailable = True

    response = client.post("/api/clients", json=BODY)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "FINERACT_UNAVAILABLE"


def test_invalid_client_uid_returns_422(client: TestClient) -> None:
    assert client.post("/api/clients", json=BODY | {"client_uid": "SME-1"}).status_code == 422
