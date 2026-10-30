from datetime import UTC, datetime, timedelta
from typing import Any

from app.gateways.erp import ErpAccount, ErpRejectedError, ErpUnavailableError
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


class FakeErpJournal:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self.down = False
        self.fail_before: Exception | None = None
        self.fail_after: Exception | None = None
        self.reject: ErpRejectedError | None = None

    def post_journal_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.fail_before is not None:
            error, self.fail_before = self.fail_before, None
            raise error
        if self.down:
            raise ErpUnavailableError("ConnectError: connection refused")
        if self.reject is not None:
            raise self.reject
        existing = self.entries.get(payload["external_reference"])
        if existing is not None and existing != payload:
            raise ErpRejectedError("different payload for the same reference", 409)
        self.entries[payload["external_reference"]] = payload
        if self.fail_after is not None:
            error, self.fail_after = self.fail_after, None
            raise error
        return self.as_erp_entry(payload)

    def as_erp_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        number = list(self.entries).index(payload["external_reference"]) + 1
        return payload | {
            "entry_id": f"JE-{number:06d}",
            "total_amount": f"{sum(line['debit'] for line in payload['lines'])}.00",
        }

    def journal_entries(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self.down:
            raise ErpUnavailableError("connection refused")
        references = params.get("external_reference")
        return [
            self.as_erp_entry(p)
            for p in self.entries.values()
            if (references is None or p["external_reference"] in references)
            and (params.get("from_date") is None or p["entry_date"] >= params["from_date"])
            and (params.get("to_date") is None or p["entry_date"] <= params["to_date"])
        ]


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


STATUS_CODES = {
    "SUBMITTED": "loanStatusType.submitted.and.pending.approval",
    "APPROVED": "loanStatusType.approved",
    "ACTIVE": "loanStatusType.active",
    "CLOSED": "loanStatusType.closed.obligations.met",
}


class FakeFineractLoans:
    def __init__(self) -> None:
        self.loans: dict[int, dict[str, Any]] = {}
        self.transactions: dict[int, dict[str, dict[str, Any]]] = {}
        self.calls: dict[str, int] = {}
        self.fail_before: dict[str, Exception] = {}
        self.fail_after: dict[str, Exception] = {}
        self.interest_rate = 0.1

    def _enter(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.fail_before:
            raise self.fail_before.pop(name)

    def _exit(self, name: str) -> None:
        if name in self.fail_after:
            raise self.fail_after.pop(name)

    def _status(self, loan_id: int) -> str:
        code = self.loans[loan_id]["status"]["code"]
        return next(k for k, v in STATUS_CODES.items() if v == code)

    def _set_status(self, loan_id: int, status: str) -> None:
        self.loans[loan_id]["status"] = {"code": STATUS_CODES[status]}

    def loan_product_id_by_external_id(self, external_id: str) -> int | None:
        return 1 if external_id == "LP-SME-ASSET-FINANCE" else None

    def create_loan(self, payload: dict[str, Any]) -> int:
        self._enter("create_loan")
        loan_id = len(self.loans) + 1
        self.loans[loan_id] = {
            "id": loan_id,
            "externalId": payload["externalId"],
            "payload": payload,
            "status": {"code": STATUS_CODES["SUBMITTED"]},
            "timeline": {"submittedOnDate": [2026, 10, 30]},
            "summary": None,
        }
        self.transactions[loan_id] = {}
        self._exit("create_loan")
        return loan_id

    def loan_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        return next((x for x in self.loans.values() if x["externalId"] == external_id), None)

    def loan(self, loan_id: int) -> dict[str, Any]:
        return self.loans[loan_id]

    def approve_loan(self, loan_id: int, payload: dict[str, Any]) -> None:
        self._enter("approve_loan")
        if self._status(loan_id) != "SUBMITTED":
            raise FineractRejectedError("loan is not pending approval", 403)
        self._set_status(loan_id, "APPROVED")
        self.loans[loan_id]["timeline"]["approvedOnDate"] = [2026, 10, 30]
        self._exit("approve_loan")

    def disburse_loan(self, loan_id: int, payload: dict[str, Any]) -> None:
        self._enter("disburse_loan")
        if self._status(loan_id) != "APPROVED":
            raise FineractRejectedError("loan cannot be disbursed in its current state", 403)
        principal = int(payload["transactionAmount"])
        self._add_transaction(loan_id, payload["externalId"], principal, None, None, 1)
        interest = int(principal * self.interest_rate)
        self.loans[loan_id]["summary"] = {
            "principalDisbursed": principal,
            "principalOutstanding": principal,
            "interestOutstanding": interest,
            "totalOutstanding": principal + interest,
            "totalRepayment": 0,
            "totalExpectedRepayment": principal + interest,
        }
        self._set_status(loan_id, "ACTIVE")
        self._exit("disburse_loan")

    def repay_loan(self, loan_id: int, payload: dict[str, Any]) -> None:
        self._enter("repay_loan")
        if self._status(loan_id) != "ACTIVE":
            raise FineractRejectedError("loan is not active", 403)
        if payload["externalId"] in self.transactions[loan_id]:
            raise FineractRejectedError("externalId already used", 403)
        summary = self.loans[loan_id]["summary"]
        amount = int(payload["transactionAmount"])
        interest = min(amount, summary["interestOutstanding"])
        principal = min(amount - interest, summary["principalOutstanding"])
        summary["interestOutstanding"] -= interest
        summary["principalOutstanding"] -= principal
        summary["totalOutstanding"] -= interest + principal
        summary["totalRepayment"] += interest + principal
        self._add_transaction(loan_id, payload["externalId"], amount, principal, interest, 2)
        if summary["totalOutstanding"] == 0:
            self._set_status(loan_id, "CLOSED")
        self._exit("repay_loan")

    def loan_transaction_by_external_id(
        self, loan_id: int, external_id: str
    ) -> dict[str, Any] | None:
        return self.transactions[loan_id].get(external_id)

    def loan_transactions(self, loan_id: int) -> list[dict[str, Any]]:
        return list(self.transactions[loan_id].values())

    def _add_transaction(
        self,
        loan_id: int,
        external_id: str,
        amount: int,
        principal: int | None,
        interest: int | None,
        type_id: int,
    ) -> None:
        self.transactions[loan_id][external_id] = {
            "id": sum(len(t) for t in self.transactions.values()) + 1,
            "type": {"id": type_id},
            "date": [2026, 10, 30],
            "manuallyReversed": False,
            "externalId": external_id,
            "amount": f"{amount}.000000",
            "principalPortion": principal,
            "interestPortion": interest,
            "feeChargesPortion": 0,
            "penaltyChargesPortion": 0,
        }
