from collections.abc import Iterator
from typing import NoReturn

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.faults import (
    Fault,
    FaultInjectingTransport,
    FaultInjector,
    FaultMode,
    FaultOperation,
    injector,
    operation_of,
)
from app.gateways.erp import ErpClient, ErpUnavailableError
from app.gateways.fineract import FineractClient, FineractUnavailableError
from app.main import app


class ProcessExitedError(Exception):
    pass


def exit_process(code: int) -> NoReturn:
    raise ProcessExitedError(code)


class Server:
    def __init__(self) -> None:
        self.received: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        return httpx.Response(200, json={"id": 1, "external_reference": "R"})


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def faults() -> FaultInjector:
    return FaultInjector(exit_process=exit_process)


def fineract(server: Server, faults: FaultInjector) -> FineractClient:
    transport = FaultInjectingTransport(httpx.MockTransport(server), faults, "fineract")
    return FineractClient(httpx.Client(base_url="https://f/api/v1", transport=transport))


def erp(server: Server, faults: FaultInjector) -> ErpClient:
    transport = FaultInjectingTransport(httpx.MockTransport(server), faults, "erp")
    return ErpClient(httpx.Client(base_url="http://erp", transport=transport))


def arm(faults: FaultInjector, operation: FaultOperation, mode: FaultMode, times: int = 1) -> None:
    faults.arm(Fault(operation=operation, mode=mode, times=times))


@pytest.mark.parametrize(
    ("target", "method", "url", "expected"),
    [
        ("fineract", "POST", "https://f/loans/1?command=disburse", "fineract.disburse"),
        (
            "fineract",
            "POST",
            "https://f/loans/1/transactions?command=repayment",
            "fineract.repayment",
        ),
        ("fineract", "POST", "https://f/loans/1?command=approve", None),
        ("fineract", "GET", "https://f/loans/1?command=disburse", None),
        ("erp", "POST", "http://erp/api/journal-entries", "erp.journal_entry"),
        ("erp", "GET", "http://erp/api/journal-entries", None),
    ],
)
def test_operation_matching(target: str, method: str, url: str, expected: str | None) -> None:
    assert operation_of(target, httpx.Request(method, url)) == expected


def test_fail_before_never_reaches_the_server(server: Server, faults: FaultInjector) -> None:
    arm(faults, FaultOperation.FINERACT_REPAYMENT, FaultMode.FAIL_BEFORE)

    with pytest.raises(FineractUnavailableError, match="ConnectError"):
        fineract(server, faults).repay_loan(1, {})

    assert server.received == []


def test_timeout_after_reaches_the_server_and_loses_the_response(
    server: Server, faults: FaultInjector
) -> None:
    arm(faults, FaultOperation.ERP_JOURNAL_ENTRY, FaultMode.TIMEOUT_AFTER)

    with pytest.raises(ErpUnavailableError, match="ReadTimeout"):
        erp(server, faults).post_journal_entry({"external_reference": "R"})

    assert len(server.received) == 1


def test_crash_after_reaches_the_server_then_exits(server: Server, faults: FaultInjector) -> None:
    arm(faults, FaultOperation.FINERACT_DISBURSE, FaultMode.CRASH_AFTER)

    with pytest.raises(ProcessExitedError):
        fineract(server, faults).disburse_loan(1, {})

    assert len(server.received) == 1


def test_fault_fires_the_requested_number_of_times(server: Server, faults: FaultInjector) -> None:
    arm(faults, FaultOperation.FINERACT_REPAYMENT, FaultMode.FAIL_BEFORE, times=2)
    client = fineract(server, faults)

    for _ in range(2):
        with pytest.raises(FineractUnavailableError):
            client.repay_loan(1, {})
    client.repay_loan(1, {})

    assert len(server.received) == 1
    assert faults.active() == []


def test_other_operations_pass_through(server: Server, faults: FaultInjector) -> None:
    arm(faults, FaultOperation.FINERACT_REPAYMENT, FaultMode.FAIL_BEFORE)

    fineract(server, faults).disburse_loan(1, {})

    assert len(server.received) == 1
    assert len(faults.active()) == 1


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "fault_injection_enabled", True)
    yield TestClient(app)
    injector.clear()


def test_admin_arms_lists_and_clears_faults(admin: TestClient) -> None:
    body = {"operation": "erp.journal_entry", "mode": "timeout_after", "times": 2}

    armed = admin.post("/api/admin/faults", json=body)

    assert armed.status_code == 201
    assert admin.get("/api/admin/faults").json() == [body]
    assert admin.delete("/api/admin/faults").status_code == 204
    assert admin.get("/api/admin/faults").json() == []


def test_admin_rejects_unknown_fault(admin: TestClient) -> None:
    body = {"operation": "erp.journal_entry", "mode": "explode"}

    assert admin.post("/api/admin/faults", json=body).status_code == 422


def test_fault_endpoints_are_off_by_default() -> None:
    response = TestClient(app).post(
        "/api/admin/faults", json={"operation": "erp.journal_entry", "mode": "fail_before"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FAULT_INJECTION_DISABLED"
