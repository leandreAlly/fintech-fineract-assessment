from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.deps import get_sync_service
from app.gateways.fineract import FineractRejectedError, FineractUnavailableError
from app.gl_sync import GlSyncService
from app.main import app
from tests.fakes import INTEREST_INCOME, LOAN_RECEIVABLE, Clock, FakeErp, FakeFineract


@pytest.fixture
def erp() -> FakeErp:
    return FakeErp(LOAN_RECEIVABLE, INTEREST_INCOME)


@pytest.fixture
def fineract() -> FakeFineract:
    return FakeFineract()


@pytest.fixture
def client(session: Session, erp: FakeErp, fineract: FakeFineract) -> Iterator[TestClient]:
    service = GlSyncService(
        session,
        erp,
        fineract,
        max_attempts=3,
        backoff_base_seconds=2,
        backoff_max_seconds=60,
        clock=Clock(),
    )
    app.dependency_overrides[get_sync_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def sync(client: TestClient, external_reference: str = "GL-1200") -> tuple[int, dict[str, object]]:
    response = client.post("/api/accounts/sync", json={"external_reference": external_reference})
    return response.status_code, response.json()


def test_synced_account_returns_200_without_fineract_id(client: TestClient) -> None:
    status_code, body = sync(client)

    assert status_code == 200
    assert body["status"] == "SYNCED"
    assert "fineract_gl_account_id" not in body
    assert "id" not in body


def test_fineract_down_returns_202_pending(client: TestClient, fineract: FakeFineract) -> None:
    fineract.failures = [FineractUnavailableError("ReadTimeout: timed out")]

    status_code, body = sync(client)

    assert status_code == 202
    assert body["status"] == "SYNC_PENDING"
    assert body["next_attempt_at"] is not None


def test_rejected_account_returns_422_failed(client: TestClient, fineract: FakeFineract) -> None:
    fineract.failures = [FineractRejectedError("different payload", 409)]

    status_code, body = sync(client)

    assert status_code == 422
    assert body["status"] == "FAILED"


def test_unknown_reference_returns_404(client: TestClient) -> None:
    assert sync(client, "GL-9999")[0] == 404


def test_erp_down_returns_503(client: TestClient, erp: FakeErp) -> None:
    erp.down = True

    assert sync(client)[0] == 503
    assert client.post("/api/accounts/sync/all").status_code == 503


def test_status_endpoints(client: TestClient) -> None:
    assert client.get("/api/accounts/sync/GL-1200").status_code == 404

    client.post("/api/accounts/sync/all")

    assert client.get("/api/accounts/sync/GL-1200").json()["status"] == "SYNCED"
    assert [r["erp_code"] for r in client.get("/api/accounts/sync").json()] == ["1200", "4000"]


def test_invalid_request_returns_422(client: TestClient) -> None:
    assert client.post("/api/accounts/sync", json={"external_reference": ""}).status_code == 422
