import uuid
from collections.abc import Iterator

import httpx
import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from app.models import Client, Loan, LoanTransaction, TransactionStatus, TransactionType
from app.observability import (
    CORRELATION_HEADER,
    CorrelationIdMiddleware,
    correlation_id_from,
    current_correlation_id,
    forward_correlation_id,
)
from app.state_machine import transition


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/echo")
    def echo() -> dict[str, str | None]:
        return {"correlation_id": current_correlation_id()}

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_context() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def test_new_correlation_id_is_returned_and_visible_to_the_endpoint(client: TestClient) -> None:
    response = client.get("/echo")

    correlation_id = response.headers[CORRELATION_HEADER]
    assert uuid.UUID(correlation_id)
    assert response.json() == {"correlation_id": correlation_id}


def test_caller_correlation_id_is_kept(client: TestClient) -> None:
    response = client.get("/echo", headers={CORRELATION_HEADER: "teller-42.req-7"})

    assert response.headers[CORRELATION_HEADER] == "teller-42.req-7"
    assert response.json() == {"correlation_id": "teller-42.req-7"}


@pytest.mark.parametrize("value", [None, "", "has spaces", "x" * 101, "semi;colon"])
def test_missing_or_unsafe_correlation_id_is_replaced(value: str | None) -> None:
    assert uuid.UUID(correlation_id_from(value))


def test_404_still_carries_a_correlation_id(client: TestClient) -> None:
    assert CORRELATION_HEADER in client.get("/nope").headers


def test_outgoing_calls_forward_the_correlation_id() -> None:
    structlog.contextvars.bind_contextvars(correlation_id="abc-123")
    request = httpx.Request("POST", "http://erp/api/journal-entries")

    forward_correlation_id(request)

    assert request.headers[CORRELATION_HEADER] == "abc-123"


def test_no_header_without_a_correlation_id() -> None:
    request = httpx.Request("POST", "http://erp/api/journal-entries")

    forward_correlation_id(request)

    assert CORRELATION_HEADER not in request.headers


def test_status_change_log_carries_the_tracing_fields(session: Session) -> None:
    client = Client(client_uid="SME-000001", external_reference="C", name="U", fineract_client_id=7)
    loan = Loan(
        loan_reference="LN-2026-000001", external_reference="APP-1", client=client, principal=1
    )
    txn = LoanTransaction(
        external_reference="RPY-1",
        loan=loan,
        type=TransactionType.REPAYMENT,
        status=TransactionStatus.ERP_PENDING,
        fineract_transaction_id=15,
        erp_transaction_id="JE-000002",
    )

    with capture_logs() as logs:
        transition(txn, TransactionStatus.COMPLETED)

    assert logs == [
        {
            "event": "transaction_status_changed",
            "log_level": "info",
            "from_status": "ERP_PENDING",
            "external_reference": "RPY-1",
            "transaction_type": "REPAYMENT",
            "loan_reference": "LN-2026-000001",
            "client_uid": "SME-000001",
            "fineract_transaction_id": 15,
            "erp_transaction_id": "JE-000002",
            "status": "COMPLETED",
        }
    ]
