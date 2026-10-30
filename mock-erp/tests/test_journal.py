from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import accounts
from app.models import JournalEntry, JournalLine


@pytest.fixture(autouse=True)
def seeded(session_factory: sessionmaker[Session]) -> Iterator[None]:
    with session_factory() as session:
        accounts.seed_accounts(session)
    yield


def disbursement(**overrides: Any) -> dict[str, Any]:
    body = {
        "external_reference": "DISB-LN-2026-000001",
        "entry_date": "2026-10-30",
        "currency": "RWF",
        "description": "Disbursement LN-2026-000001",
        "loan_reference": "LN-2026-000001",
        "transaction_type": "DISBURSEMENT",
        "lines": [
            {"account_code": "1200", "debit": "5000000"},
            {"account_code": "1100", "credit": "5000000"},
        ],
    }
    return body | overrides


def repayment(**overrides: Any) -> dict[str, Any]:
    body = {
        "external_reference": "REP-1",
        "entry_date": "2026-10-30",
        "currency": "RWF",
        "loan_reference": "LN-2026-000001",
        "transaction_type": "REPAYMENT",
        "lines": [
            {"account_code": "1100", "debit": "458400"},
            {"account_code": "1200", "credit": "383400"},
            {"account_code": "4000", "credit": "75000"},
        ],
    }
    return body | overrides


def entry_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(JournalEntry)) or 0


def line_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(JournalLine)) or 0


def test_balanced_entry_is_created(client: TestClient) -> None:
    response = client.post("/api/journal-entries", json=disbursement())

    assert response.status_code == 201
    body = response.json()
    assert body["total_amount"] == "5000000.00"
    assert [(line["account_code"], line["debit"], line["credit"]) for line in body["lines"]] == [
        ("1200", "5000000.00", "0.00"),
        ("1100", "0.00", "5000000.00"),
    ]


def test_same_entry_four_times_is_written_once(client: TestClient, session: Session) -> None:
    statuses = [
        client.post("/api/journal-entries", json=disbursement()).status_code for _ in range(4)
    ]

    assert statuses == [201, 200, 200, 200]
    assert entry_count(session) == 1
    assert line_count(session) == 2


def test_equivalent_amount_formats_are_the_same_payload(client: TestClient) -> None:
    client.post("/api/journal-entries", json=disbursement())
    same = disbursement(
        lines=[
            {"account_code": "1200", "debit": "5000000.00", "credit": 0},
            {"account_code": "1100", "credit": 5000000},
        ]
    )

    assert client.post("/api/journal-entries", json=same).status_code == 200


def test_same_reference_different_payload_returns_409(client: TestClient, session: Session) -> None:
    client.post("/api/journal-entries", json=disbursement())
    different = disbursement(
        lines=[
            {"account_code": "1200", "debit": "4000000"},
            {"account_code": "1100", "credit": "4000000"},
        ]
    )

    response = client.post("/api/journal-entries", json=different)

    assert response.status_code == 409
    assert entry_count(session) == 1


def test_unbalanced_entry_is_rejected(client: TestClient, session: Session) -> None:
    unbalanced = repayment(
        lines=[
            {"account_code": "1100", "debit": "458400"},
            {"account_code": "1200", "credit": "383400"},
            {"account_code": "4000", "credit": "70000"},
        ]
    )

    response = client.post("/api/journal-entries", json=unbalanced)

    assert response.status_code == 422
    assert "not balanced" in response.text
    assert entry_count(session) == 0


@pytest.mark.parametrize(
    "line",
    [
        {"account_code": "1200", "debit": "100", "credit": "100"},
        {"account_code": "1200"},
        {"account_code": "1200", "debit": "-100"},
        {"account_code": "1200", "debit": "100.001"},
    ],
)
def test_invalid_lines_are_rejected(client: TestClient, line: dict[str, Any]) -> None:
    body = disbursement(lines=[line, {"account_code": "1100", "credit": "100"}])

    assert client.post("/api/journal-entries", json=body).status_code == 422


def test_single_line_entry_is_rejected(client: TestClient) -> None:
    body = disbursement(lines=[{"account_code": "1200", "debit": "100"}])

    assert client.post("/api/journal-entries", json=body).status_code == 422


def test_unknown_account_is_rejected(client: TestClient, session: Session) -> None:
    body = disbursement(
        lines=[
            {"account_code": "9999", "debit": "100"},
            {"account_code": "1100", "credit": "100"},
        ]
    )

    response = client.post("/api/journal-entries", json=body)

    assert response.status_code == 422
    assert "9999" in response.text
    assert entry_count(session) == 0


def test_currency_must_match_accounts(client: TestClient) -> None:
    response = client.post("/api/journal-entries", json=disbursement(currency="USD"))

    assert response.status_code == 422
    assert "not in currency USD" in response.text


def test_get_entry_by_reference(client: TestClient) -> None:
    client.post("/api/journal-entries", json=repayment())

    response = client.get("/api/journal-entries/REP-1")

    assert response.status_code == 200
    assert response.json()["transaction_type"] == "REPAYMENT"
    assert len(response.json()["lines"]) == 3


def test_get_unknown_entry_returns_404(client: TestClient) -> None:
    assert client.get("/api/journal-entries/NOPE").status_code == 404


def test_balances_follow_each_accounts_normal_side(client: TestClient) -> None:
    client.post("/api/journal-entries", json=disbursement())
    client.post("/api/journal-entries", json=repayment())

    receivable = client.get("/api/accounts/1200/balance").json()
    bank = client.get("/api/accounts/1100/balance").json()
    interest = client.get("/api/accounts/4000/balance").json()

    assert receivable["balance"] == "4616600.00"
    assert receivable["line_count"] == 2
    assert bank["balance"] == "-4541600.00"
    assert interest["balance"] == "75000.00"
    assert interest["total_credits"] == "75000.00"


def test_balance_of_unused_account_is_zero(client: TestClient) -> None:
    body = client.get("/api/accounts/5100/balance").json()

    assert body["balance"] == "0.00"
    assert body["line_count"] == 0


def test_balance_of_unknown_account_returns_404(client: TestClient) -> None:
    assert client.get("/api/accounts/9999/balance").status_code == 404


def test_entry_id_is_the_erp_transaction_id(client: TestClient) -> None:
    body = client.post("/api/journal-entries", json=disbursement()).json()

    assert body["entry_id"] == "JE-000001"


def test_list_filters_by_date_loan_and_reference(client: TestClient) -> None:
    client.post("/api/journal-entries", json=disbursement())
    client.post("/api/journal-entries", json=repayment(entry_date="2026-11-15"))
    client.post(
        "/api/journal-entries",
        json=repayment(external_reference="REP-OTHER", loan_reference="LN-2026-000002"),
    )

    def refs(**params: Any) -> list[str]:
        response = client.get("/api/journal-entries", params=params)
        return [entry["external_reference"] for entry in response.json()]

    assert refs() == ["DISB-LN-2026-000001", "REP-1", "REP-OTHER"]
    assert refs(from_date="2026-11-01") == ["REP-1"]
    assert refs(to_date="2026-10-31") == ["DISB-LN-2026-000001", "REP-OTHER"]
    assert refs(loan_reference="LN-2026-000002") == ["REP-OTHER"]
    assert refs(external_reference=["REP-1", "REP-OTHER"]) == ["REP-1", "REP-OTHER"]
