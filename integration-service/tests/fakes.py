from datetime import UTC, datetime, timedelta
from typing import Any

from app.gateways.erp import ErpAccount, ErpUnavailableError
from app.gateways.fineract import FineractRejectedError, FineractUnavailableError

LOAN_RECEIVABLE = ErpAccount(
    code="1200",
    name="Loan Receivable",
    type="ASSET",
    currency="RWF",
    external_reference="GL-1200",
)
INTEREST_INCOME = ErpAccount(
    code="4000",
    name="Interest Income",
    type="INCOME",
    currency="RWF",
    external_reference="GL-4000",
)


class FakeErp:
    def __init__(self, *accounts: ErpAccount) -> None:
        self.accounts = {a.external_reference: a for a in accounts}
        self.down = False

    def list_accounts(self) -> list[ErpAccount]:
        if self.down:
            raise ErpUnavailableError("connection refused")
        return list(self.accounts.values())

    def get_account_by_reference(self, external_reference: str) -> ErpAccount | None:
        if self.down:
            raise ErpUnavailableError("connection refused")
        return self.accounts.get(external_reference)


class FakeFineract:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures: list[Exception] = []
        self.gl_account_id = 6

    def sync_gl_account(self, payload: dict[str, Any]) -> int:
        self.calls.append(payload)
        if self.failures:
            raise self.failures.pop(0)
        return self.gl_account_id


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeFineractClients:
    def __init__(self) -> None:
        self.clients: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.failures: list[Exception] = []
        self.fail_after_create: Exception | None = None
        self.unavailable = False

    def office_id_by_external_id(self, external_id: str) -> int | None:
        return 2 if external_id == "OFFICE-KGL-MAIN" else None

    def create_client(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        if self.failures:
            raise self.failures.pop(0)
        client_id = len(self.clients) + 1
        self.clients[payload["externalId"]] = {
            "id": client_id,
            "externalId": payload["externalId"],
            "displayName": payload["fullname"],
            "active": True,
            "status": {"value": "Active"},
            "officeName": "Kigali Main Office",
            "activationDate": [2026, 10, 30],
        }
        if self.fail_after_create is not None:
            error, self.fail_after_create = self.fail_after_create, None
            raise error
        return client_id

    def client_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        if self.unavailable:
            raise FineractUnavailableError("ConnectError: connection refused")
        return self.clients.get(external_id)

    def client(self, client_id: int) -> dict[str, Any]:
        if self.unavailable:
            raise FineractUnavailableError("ConnectError: connection refused")
        for found in self.clients.values():
            if found["id"] == client_id:
                return found
        raise FineractRejectedError("client not found", 404)
