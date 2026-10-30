from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.gateways.erp import ErpRejectedError, ErpUnavailableError
from app.gateways.fineract import FineractUnavailableError
from app.loan_service import (
    ApprovalRequest,
    DisbursementRequest,
    LoanApplicationRequest,
    LoanService,
    RepaymentRequest,
    loan_status,
    money,
)
from app.models import Client, LoanTransaction, TransactionStatus
from tests.fakes import FakeErpJournal, FakeFineractLoans


@pytest.fixture
def fineract() -> FakeFineractLoans:
    return FakeFineractLoans()


@pytest.fixture
def erp() -> FakeErpJournal:
    return FakeErpJournal()


@pytest.fixture
def service(session: Session, fineract: FakeFineractLoans, erp: FakeErpJournal) -> LoanService:
    session.add(
        Client(
            client_uid="SME-000001",
            external_reference="CLI-1",
            name="Umucyo Traders",
            fineract_client_id=7,
        )
    )
    session.commit()
    return LoanService(
        session,
        fineract,
        erp,
        product_external_id="LP-SME-ASSET-FINANCE",
        payment_type_id=1,
        today=lambda: date(2026, 10, 30),
    )


def apply(service: LoanService, **overrides: Any) -> str:
    body = {"external_reference": "APP-1", "client_uid": "SME-000001", "principal": 5_000_000}
    result = service.apply(LoanApplicationRequest.model_validate(body | overrides))
    assert result.status_code in (200, 201), result.body
    return str(result.body["loan_reference"])


def approve(service: LoanService, loan: str, ref: str = "APR-1") -> int:
    return service.approve(loan, ApprovalRequest(external_reference=ref)).status_code


def disburse(service: LoanService, loan: str, ref: str = "DSB-1") -> int:
    return service.disburse(loan, DisbursementRequest(external_reference=ref)).status_code


def repay(service: LoanService, loan: str, ref: str, amount: int) -> dict[str, Any]:
    result = service.repay(loan, RepaymentRequest(external_reference=ref, amount=amount))
    return {"status_code": result.status_code} | result.body


def disbursed_loan(service: LoanService) -> str:
    loan = apply(service)
    approve(service, loan)
    disburse(service, loan)
    return loan


def transaction_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(LoanTransaction)) or 0


def unavailable() -> FineractUnavailableError:
    return FineractUnavailableError("ReadTimeout: timed out")


def test_application_issues_loan_reference_and_uses_it_as_fineract_external_id(
    service: LoanService, fineract: FakeFineractLoans
) -> None:
    result = service.apply(
        LoanApplicationRequest(
            external_reference="APP-1", client_uid="SME-000001", principal=5_000_000
        )
    )

    assert result.status_code == 201
    assert result.body["loan_reference"] == "LN-2026-000001"
    assert result.body["status"] == "SUBMITTED_AND_PENDING_APPROVAL"
    payload = fineract.loans[1]["payload"]
    assert payload["externalId"] == "LN-2026-000001"
    assert (payload["clientId"], payload["productId"], payload["principal"]) == (7, 1, 5_000_000)


def test_application_for_unknown_client_is_422(service: LoanService) -> None:
    result = service.apply(
        LoanApplicationRequest(external_reference="APP-1", client_uid="SME-999999", principal=1)
    )

    assert result.status_code == 422
    assert result.body["error"]["code"] == "UNKNOWN_CLIENT"


def test_same_application_four_times_creates_one_fineract_loan(
    service: LoanService, fineract: FakeFineractLoans
) -> None:
    references = {apply(service) for _ in range(4)}

    assert references == {"LN-2026-000001"}
    assert fineract.calls["create_loan"] == 1


def test_application_timeout_after_fineract_created_loan_is_recovered(
    service: LoanService, fineract: FakeFineractLoans
) -> None:
    fineract.fail_after["create_loan"] = unavailable()

    with pytest.raises(FineractUnavailableError):
        apply(service)
    loan = apply(service)

    assert loan == "LN-2026-000001"
    assert fineract.calls["create_loan"] == 1
    assert len(fineract.loans) == 1


def test_approval_and_its_replay(service: LoanService) -> None:
    loan = apply(service)

    assert approve(service, loan) == 200
    assert approve(service, loan) == 200
    assert service.get(loan).body["status"] == "APPROVED"


def test_second_approval_with_another_reference_is_409(
    service: LoanService, fineract: FakeFineractLoans
) -> None:
    loan = apply(service)
    approve(service, loan)

    result = service.approve(loan, ApprovalRequest(external_reference="APR-2"))

    assert result.status_code == 409
    assert result.body["error"]["code"] == "LOAN_ALREADY_APPROVED"
    assert fineract.calls["approve_loan"] == 1


def test_approval_timeout_after_fineract_approved_is_recovered(
    service: LoanService, fineract: FakeFineractLoans
) -> None:
    loan = apply(service)
    fineract.fail_after["approve_loan"] = unavailable()

    with pytest.raises(FineractUnavailableError):
        approve(service, loan)

    assert approve(service, loan) == 200
    assert fineract.calls["approve_loan"] == 1


def test_disbursement_records_fineract_transaction(service: LoanService) -> None:
    loan = disbursed_loan(service)

    body = service.get(loan).body
    assert body["status"] == "ACTIVE"
    assert body["transactions"][0]["type"] == "DISBURSEMENT"
    assert body["transactions"][0]["amount"] == 5_000_000
    assert body["transactions"][0]["status"] == "COMPLETED"


def test_same_disbursement_four_times_disburses_once(
    service: LoanService, session: Session, fineract: FakeFineractLoans
) -> None:
    loan = apply(service)
    approve(service, loan)

    assert [disburse(service, loan) for _ in range(4)] == [201, 200, 200, 200]
    assert fineract.calls["disburse_loan"] == 1
    assert transaction_count(session) == 1


def test_disbursement_with_another_reference_is_409(
    service: LoanService, fineract: FakeFineractLoans
) -> None:
    loan = disbursed_loan(service)

    result = service.disburse(loan, DisbursementRequest(external_reference="DSB-2"))

    assert result.status_code == 409
    assert result.body["error"]["code"] == "LOAN_ALREADY_DISBURSED"
    assert fineract.calls["disburse_loan"] == 1


def test_rejected_disbursement_does_not_block_a_later_one(
    service: LoanService, session: Session
) -> None:
    loan = apply(service)

    assert disburse(service, loan, "DSB-EARLY") == 422
    assert session.scalar(select(LoanTransaction.status)) == TransactionStatus.FAILED
    approve(service, loan)
    assert disburse(service, loan, "DSB-1") == 201


def test_fineract_down_during_disbursement_keeps_it_pending_and_retryable(
    service: LoanService, session: Session, fineract: FakeFineractLoans
) -> None:
    loan = apply(service)
    approve(service, loan)
    fineract.fail_before["disburse_loan"] = FineractUnavailableError("ConnectError: refused")

    with pytest.raises(FineractUnavailableError):
        disburse(service, loan)

    assert session.scalar(select(LoanTransaction.status)) == TransactionStatus.FINERACT_PENDING
    assert disburse(service, loan, "DSB-OTHER") == 409
    assert disburse(service, loan) == 201


def test_disbursement_timeout_after_fineract_posted_is_recovered_by_lookup(
    service: LoanService, fineract: FakeFineractLoans
) -> None:
    loan = apply(service)
    approve(service, loan)
    fineract.fail_after["disburse_loan"] = unavailable()

    with pytest.raises(FineractUnavailableError):
        disburse(service, loan)

    assert disburse(service, loan) == 201
    assert fineract.calls["disburse_loan"] == 1
    assert len(fineract.transactions[1]) == 1


def test_repayment_stores_fineract_breakdown(service: LoanService) -> None:
    loan = disbursed_loan(service)

    body = repay(service, loan, "RPY-1", 600_000)

    assert body["status_code"] == 201
    assert (body["principal_portion"], body["interest_portion"]) == (100_000, 500_000)


def test_duplicate_repayment_applies_once(
    service: LoanService, session: Session, fineract: FakeFineractLoans
) -> None:
    loan = disbursed_loan(service)

    first = repay(service, loan, "RPY-1", 600_000)
    second = repay(service, loan, "RPY-1", 600_000)

    assert (first["status_code"], second["status_code"]) == (201, 200)
    assert fineract.calls["repay_loan"] == 1
    assert service.get(loan).body["balances"]["total_repaid"] == 600_000


def test_many_repayments_with_different_references_all_apply(
    service: LoanService, fineract: FakeFineractLoans
) -> None:
    loan = disbursed_loan(service)

    for i in range(3):
        repay(service, loan, f"RPY-{i}", 100_000)

    assert fineract.calls["repay_loan"] == 3
    assert service.get(loan).body["balances"]["total_repaid"] == 300_000


def test_reference_used_by_a_disbursement_cannot_be_a_repayment(service: LoanService) -> None:
    loan = disbursed_loan(service)

    body = repay(service, loan, "DSB-1", 100_000)

    assert body["status_code"] == 409
    assert body["error"]["code"] == "TRANSACTION_REFERENCE_CONFLICT"


def test_full_lifecycle_ends_closed(service: LoanService) -> None:
    loan = disbursed_loan(service)
    for i, amount in enumerate([458_400, 300_000, 1_000_000]):
        repay(service, loan, f"RPY-{i}", amount)
    payoff = service.get(loan).body["balances"]["total_outstanding"]

    repay(service, loan, "RPY-FINAL", payoff)

    body = service.get(loan).body
    assert body["status"] == "CLOSED_OBLIGATIONS_MET"
    assert body["balances"]["total_outstanding"] == 0
    repaid_principal = sum(
        t["principal_portion"] for t in body["transactions"] if t["type"] == "REPAYMENT"
    )
    assert repaid_principal == 5_000_000


def test_transaction_lookup_by_external_reference(service: LoanService) -> None:
    loan = disbursed_loan(service)
    repay(service, loan, "RPY-1", 600_000)

    found = service.get_transaction("RPY-1")

    assert found.status_code == 200
    assert found.body["loan_reference"] == loan
    assert "fineract_transaction_id" not in found.body
    assert service.get_transaction("RPY-404").status_code == 404


def test_unknown_loan_is_404(service: LoanService) -> None:
    assert service.get("LN-2026-999999").status_code == 404
    assert approve(service, "LN-2026-999999") == 404
    assert disburse(service, "LN-2026-999999") == 404


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("loanStatusType.submitted.and.pending.approval", "SUBMITTED_AND_PENDING_APPROVAL"),
        ("loanStatusType.active", "ACTIVE"),
        ("loanStatusType.closed.obligations.met", "CLOSED_OBLIGATIONS_MET"),
    ],
)
def test_loan_status_from_fineract_code(code: str, expected: str) -> None:
    assert loan_status({"status": {"code": code}}) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("458400.000000", 458400), (None, 0), (75000, 75000)]
)
def test_money_parses_fineract_amounts(value: object, expected: int) -> None:
    assert money(value) == expected


def test_disbursement_posts_one_balanced_journal_entry(
    service: LoanService, erp: FakeErpJournal
) -> None:
    disbursed_loan(service)

    entry = erp.entries["DSB-1"]
    assert entry["transaction_type"] == "DISBURSEMENT"
    assert [(x["account_code"], x["debit"], x["credit"]) for x in entry["lines"]] == [
        ("1200", 5_000_000, 0),
        ("1100", 0, 5_000_000),
    ]


def test_repayment_entry_uses_fineract_split_not_our_own(
    service: LoanService, erp: FakeErpJournal, fineract: FakeFineractLoans
) -> None:
    loan = disbursed_loan(service)

    repay(service, loan, "RPY-1", 600_000)

    recorded = fineract.transactions[1]["RPY-1"]
    lines = erp.entries["RPY-1"]["lines"]
    assert [(x["account_code"], x["debit"], x["credit"]) for x in lines] == [
        ("1100", 600_000, 0),
        ("1200", 0, recorded["principalPortion"]),
        ("4000", 0, recorded["interestPortion"]),
    ]
    assert (recorded["principalPortion"], recorded["interestPortion"]) == (100_000, 500_000)


def test_same_disbursement_four_times_writes_one_journal_entry(
    service: LoanService, erp: FakeErpJournal, fineract: FakeFineractLoans
) -> None:
    loan = apply(service)
    approve(service, loan)

    for _ in range(4):
        disburse(service, loan)

    assert fineract.calls["disburse_loan"] == 1
    assert erp.calls == 1
    assert list(erp.entries) == ["DSB-1"]


def test_erp_down_keeps_fineract_result_and_returns_202(
    service: LoanService, session: Session, erp: FakeErpJournal, fineract: FakeFineractLoans
) -> None:
    loan = apply(service)
    approve(service, loan)
    erp.down = True

    result = service.disburse(loan, DisbursementRequest(external_reference="DSB-1"))

    assert result.status_code == 202
    assert result.body["status"] == "ERP_PENDING"
    assert "ERP unavailable" in result.body["last_error"]
    assert service.get(loan).body["status"] == "ACTIVE"
    assert erp.entries == {}


def test_retry_after_erp_recovers_posts_to_erp_without_touching_fineract_again(
    service: LoanService, session: Session, erp: FakeErpJournal, fineract: FakeFineractLoans
) -> None:
    loan = apply(service)
    approve(service, loan)
    erp.down = True
    disburse(service, loan)
    erp.down = False

    assert disburse(service, loan) == 201
    assert fineract.calls["disburse_loan"] == 1
    assert list(erp.entries) == ["DSB-1"]
    txn = session.scalars(select(LoanTransaction)).one()
    assert (txn.status, txn.erp_attempts) == (TransactionStatus.COMPLETED, 2)


def test_erp_timeout_after_writing_the_entry_leaves_one_entry_after_retry(
    service: LoanService, erp: FakeErpJournal
) -> None:
    loan = disbursed_loan(service)
    erp.fail_after = ErpUnavailableError("ReadTimeout: timed out")

    first = repay(service, loan, "RPY-1", 600_000)
    second = repay(service, loan, "RPY-1", 600_000)

    assert (first["status"], second["status"]) == ("ERP_PENDING", "COMPLETED")
    assert erp.calls == 3
    assert list(erp.entries) == ["DSB-1", "RPY-1"]


def test_erp_rejection_routes_to_reconciliation(service: LoanService, erp: FakeErpJournal) -> None:
    loan = disbursed_loan(service)
    erp.reject = ErpRejectedError("unknown account codes: 1200", 422)

    body = repay(service, loan, "RPY-1", 600_000)

    assert body["status_code"] == 202
    assert body["status"] == "NEEDS_RECONCILIATION"
    assert "unknown account codes" in body["last_error"]


def test_inconsistent_fineract_breakdown_routes_to_reconciliation(
    service: LoanService, erp: FakeErpJournal, fineract: FakeFineractLoans
) -> None:
    loan = disbursed_loan(service)
    repay(service, loan, "RPY-1", 600_000)
    fineract.transactions[1]["RPY-2"] = {
        "id": 99,
        "externalId": "RPY-2",
        "amount": "600000.000000",
        "principalPortion": 100_000,
        "interestPortion": 400_000,
    }

    body = repay(service, loan, "RPY-2", 600_000)

    assert body["status"] == "NEEDS_RECONCILIATION"
    assert "add up to 500000" in body["last_error"]
    assert "RPY-2" not in erp.entries


def test_completed_transaction_is_replayed_without_new_erp_calls(
    service: LoanService, erp: FakeErpJournal
) -> None:
    loan = disbursed_loan(service)
    repay(service, loan, "RPY-1", 600_000)
    calls = erp.calls

    assert repay(service, loan, "RPY-1", 600_000)["status_code"] == 200
    assert erp.calls == calls


class RecordingLocks:
    def __init__(self) -> None:
        self.held: list[int] = []
        self.active = False

    @contextmanager
    def hold(self, loan_id: int) -> Iterator[None]:
        self.held.append(loan_id)
        self.active = True
        try:
            yield
        finally:
            self.active = False


def test_fineract_calls_run_while_holding_the_loan_lock(
    session: Session, fineract: FakeFineractLoans, erp: FakeErpJournal
) -> None:
    locks = RecordingLocks()
    service = LoanService(
        session,
        fineract,
        erp,
        product_external_id="LP-SME-ASSET-FINANCE",
        payment_type_id=1,
        locks=locks,
        today=lambda: date(2026, 10, 30),
    )
    session.add(
        Client(client_uid="SME-000001", external_reference="CLI-1", name="U", fineract_client_id=7)
    )
    session.commit()
    seen: list[bool] = []
    original = fineract.repay_loan

    def repay_loan(loan_id: int, payload: dict[str, Any]) -> None:
        seen.append(locks.active)
        original(loan_id, payload)

    fineract.repay_loan = repay_loan  # type: ignore[method-assign]
    loan = disbursed_loan(service)

    repay(service, loan, "RPY-1", 600_000)

    assert locks.held == [1, 1]
    assert seen == [True]
    assert not locks.active


def test_loan_locked_by_fineract_stays_pending_and_succeeds_on_retry(
    service: LoanService, session: Session, fineract: FakeFineractLoans
) -> None:
    loan = disbursed_loan(service)
    fineract.fail_before["repay_loan"] = FineractUnavailableError(
        "Loan is locked by the COB job. Loan ID: 1", 409
    )

    with pytest.raises(FineractUnavailableError):
        repay(service, loan, "RPY-1", 600_000)

    txn = session.scalars(
        select(LoanTransaction).where(LoanTransaction.external_reference == "RPY-1")
    ).one()
    assert txn.status == TransactionStatus.FINERACT_PENDING
    assert repay(service, loan, "RPY-1", 600_000)["status"] == "COMPLETED"
    assert fineract.calls["repay_loan"] == 2
    assert len([t for t in fineract.transactions[1] if t.startswith("RPY")]) == 1
