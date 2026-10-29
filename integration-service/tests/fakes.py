from datetime import UTC, datetime, timedelta
from typing import Any

from app.clients.erp import ErpAccount, ErpUnavailableError

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
