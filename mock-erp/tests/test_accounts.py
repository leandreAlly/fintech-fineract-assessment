from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import accounts
from app.models import Account
from app.schemas import AccountIn


def loan_receivable(**overrides: Any) -> dict[str, Any]:
    body = {
        "code": "1200",
        "name": "Loan Receivable",
        "type": "ASSET",
        "currency": "RWF",
        "external_reference": "GL-1200",
    }
    return body | overrides


def account_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Account)) or 0


def test_create_returns_201(client: TestClient) -> None:
    response = client.post("/api/accounts", json=loan_receivable())

    assert response.status_code == 201
    assert response.json()["external_reference"] == "GL-1200"


def test_same_reference_same_payload_four_times_creates_one_account(
    client: TestClient, session: Session
) -> None:
    statuses = [client.post("/api/accounts", json=loan_receivable()).status_code for _ in range(4)]

    assert statuses == [201, 200, 200, 200]
    assert account_count(session) == 1


def test_same_reference_different_payload_returns_409(client: TestClient) -> None:
    client.post("/api/accounts", json=loan_receivable())

    response = client.post("/api/accounts", json=loan_receivable(name="Loans Receivable"))

    assert response.status_code == 409


def test_same_code_different_reference_returns_409(client: TestClient, session: Session) -> None:
    client.post("/api/accounts", json=loan_receivable())

    response = client.post("/api/accounts", json=loan_receivable(external_reference="GL-OTHER"))

    assert response.status_code == 409
    assert account_count(session) == 1


@pytest.mark.parametrize(
    "overrides",
    [{"type": "CASH"}, {"currency": "rwf"}, {"code": ""}, {"external_reference": ""}],
)
def test_invalid_payload_returns_422(client: TestClient, overrides: dict[str, Any]) -> None:
    response = client.post("/api/accounts", json=loan_receivable(**overrides))

    assert response.status_code == 422


def test_concurrent_insert_of_same_account_resolves_to_existing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = AccountIn.model_validate(loan_receivable())
    accounts.create_account(session, data)
    original = accounts.resolve_existing
    calls: list[str] = []

    def stale_first_read(s: Session, d: AccountIn) -> Account | None:
        calls.append(d.external_reference)
        return None if len(calls) == 1 else original(s, d)

    monkeypatch.setattr(accounts, "resolve_existing", stale_first_read)

    account, created = accounts.create_account(session, data)

    assert created is False
    assert len(calls) == 2
    assert account.external_reference == "GL-1200"
    assert account_count(session) == 1


def test_list_and_get_by_code(client: TestClient) -> None:
    client.post("/api/accounts", json=loan_receivable())

    assert [a["code"] for a in client.get("/api/accounts").json()] == ["1200"]
    assert client.get("/api/accounts/1200").json()["name"] == "Loan Receivable"
    assert client.get("/api/accounts/9999").status_code == 404


def test_seed_is_idempotent(session: Session) -> None:
    accounts.seed_accounts(session)
    accounts.seed_accounts(session)

    assert account_count(session) == len(accounts.SEED_ACCOUNTS) == 10
