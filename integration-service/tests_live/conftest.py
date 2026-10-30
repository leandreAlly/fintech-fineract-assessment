import os
import random
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
import pytest

API_URL = os.environ.get("INTEGRATION_URL", "http://localhost:8080")
API_KEY = os.environ.get("INTEGRATION_API_KEY", "dev-teller-key")
FINERACT_URL = os.environ.get("FINERACT_URL", "https://localhost:8443/fineract-provider/api/v1")
FINERACT_AUTH = (
    os.environ.get("FINERACT_USERNAME", "integration_service"),
    os.environ.get("FINERACT_PASSWORD", "Integr4tion-Svc-26!"),
)
DB_URL = os.environ.get("LIVE_DB_URL", "postgresql://postgres:postgres@localhost:5432")


@dataclass(frozen=True)
class Reply:
    status_code: int
    body: dict[str, Any]


class Api:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def post(self, path: str, body: dict[str, Any]) -> Reply:
        response = self.http.post(path, json=body)
        return Reply(response.status_code, response.json())

    def get(self, path: str) -> dict[str, Any]:
        body: dict[str, Any] = self.http.get(path).raise_for_status().json()
        return body


def fineract_http() -> httpx.Client:
    return httpx.Client(
        base_url=FINERACT_URL,
        auth=FINERACT_AUTH,
        headers={"Fineract-Platform-TenantId": "default"},
        verify=False,
        timeout=60,
    )


def unique_suffix() -> str:
    return f"{int(time.time() * 1000) % 10**8}{random.randint(100, 999)}"


def scalar(database: str, sql: str, *params: Any) -> Any:
    with psycopg.connect(f"{DB_URL}/{database}") as connection:
        row = connection.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def fineract_transactions(loan_reference: str, type_enum: int) -> int:
    return int(
        scalar(
            "fineract_default",
            "SELECT count(*) FROM m_loan_transaction t JOIN m_loan l ON l.id = t.loan_id "
            "WHERE l.external_id = %s AND t.transaction_type_enum = %s AND NOT t.is_reversed",
            loan_reference,
            type_enum,
        )
    )


def erp_entries(loan_reference: str, transaction_type: str) -> int:
    return int(
        scalar(
            "erp",
            "SELECT count(*) FROM journal_entries WHERE loan_reference = %s "
            "AND transaction_type = %s",
            loan_reference,
            transaction_type,
        )
    )


def integration_transactions(loan_reference: str, type_: str) -> int:
    return int(
        scalar(
            "integration",
            "SELECT count(*) FROM loan_transactions t JOIN loans l ON l.id = t.loan_id "
            "WHERE l.loan_reference = %s AND t.type = %s AND t.status <> 'FAILED'",
            loan_reference,
            type_,
        )
    )


def fire(count: int, call: Callable[[int], Reply]) -> list[Reply]:
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(call, range(count)))


def report(title: str, rows: dict[str, object]) -> None:
    width = max(len(label) for label in rows) + 2
    print(f"\n== {title}")
    for label, value in rows.items():
        print(f"   {label + ':':<{width}} {value}")


@pytest.fixture(scope="session")
def api() -> Iterator[Api]:
    with httpx.Client(base_url=API_URL, headers={"X-API-Key": API_KEY}, timeout=60) as http:
        try:
            http.get("/health").raise_for_status()
        except httpx.HTTPError:
            pytest.skip(f"integration service not reachable at {API_URL}")
        yield Api(http)


@pytest.fixture
def approved_loan(api: Api) -> str:
    suffix = unique_suffix()
    client_uid = f"SME-{random.randint(0, 999_999):06d}"
    client = api.post(
        "/api/clients",
        {"external_reference": f"CLI-{suffix}", "client_uid": client_uid, "name": "Parallel Ltd"},
    )
    assert client.status_code == 201, client.body
    loan = api.post(
        "/api/loans",
        {"external_reference": f"APP-{suffix}", "client_uid": client_uid, "principal": 5_000_000},
    )
    assert loan.status_code == 201, loan.body
    loan_reference = str(loan.body["loan_reference"])
    approval = api.post(
        f"/api/loans/{loan_reference}/approve", {"external_reference": f"APR-{suffix}"}
    )
    assert approval.status_code == 200, approval.body
    return loan_reference


@pytest.fixture
def active_loan(api: Api, approved_loan: str) -> str:
    disbursed = api.post(
        f"/api/loans/{approved_loan}/disburse", {"external_reference": f"DSB-{unique_suffix()}"}
    )
    assert disbursed.status_code == 201, disbursed.body
    return approved_loan
