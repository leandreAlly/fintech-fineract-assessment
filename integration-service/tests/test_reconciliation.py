from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting import Breakdown
from app.deps import get_reconciliation_service
from app.gateways.erp import ErpRejectedError
from app.loan_service import LoanService
from app.main import app
from app.models import Client, LoanTransaction, TransactionStatus
from app.reconciliation import (
    ErpSide,
    FineractSide,
    ReconciliationRequest,
    ReconciliationService,
    ReconciliationStatus,
    RepairRequest,
    compare,
    erp_sides,
    fineract_side,
)
from tests.fakes import FakeErpJournal, FakeFineractLoans
from tests.test_loan_service import disbursed_loan, repay

R = ReconciliationStatus
DAY = date(2026, 10, 30)


def fin(amount: int = 100_000, reversed_: bool = False, type_: str = "REPAYMENT") -> FineractSide:
    return FineractSide("RPY-1", "LN-2026-000001", type_, amount, DAY, reversed_)


def erp(amount: int = 100_000, reversal: int = 0, type_: str | None = "REPAYMENT") -> ErpSide:
    return ErpSide("RPY-1", "LN-2026-000001", type_, amount, DAY, "JE-000001", None, reversal)


@pytest.mark.parametrize(
    ("fineract", "ledger", "status", "difference"),
    [
        (fin(), erp(), R.MATCHED, 0),
        (fin(), None, R.MISSING_IN_ERP, 100_000),
        (None, erp(), R.MISSING_IN_FINERACT, -100_000),
        (fin(100_000), erp(90_000), R.AMOUNT_MISMATCH, 10_000),
        (fin(type_="DISBURSEMENT"), erp(), R.TYPE_MISMATCH, 0),
        (fin(reversed_=True), erp(reversal=100_000), R.MATCHED, 0),
        (fin(reversed_=True), None, R.MATCHED, 0),
        (fin(reversed_=True), erp(), R.AMOUNT_MISMATCH, -100_000),
    ],
)
def test_compare(
    fineract: FineractSide | None,
    ledger: ErpSide | None,
    status: ReconciliationStatus,
    difference: int,
) -> None:
    item = compare(fineract, ledger)

    assert (item.status, item.difference) == (status, difference)


def test_same_amount_with_a_different_split_is_a_split_mismatch() -> None:
    fineract = FineractSide(
        "RPY-1",
        "LN-1",
        "REPAYMENT",
        100_000,
        DAY,
        False,
        Breakdown(principal=25_000, interest=75_000),
    )
    ledger = ErpSide(
        "RPY-1", "LN-1", "REPAYMENT", 100_000, DAY, "JE-1", credits={"1200": 36_588, "4000": 63_412}
    )

    item = compare(fineract, ledger)

    assert item.status == R.SPLIT_MISMATCH
    assert item.difference == 0
    assert item.note == "1200: Fineract 25000, ERP 36588; 4000: Fineract 75000, ERP 63412"


def test_same_split_matches() -> None:
    fineract = FineractSide(
        "RPY-1",
        "LN-1",
        "REPAYMENT",
        100_000,
        DAY,
        False,
        Breakdown(principal=25_000, interest=75_000),
    )
    ledger = ErpSide(
        "RPY-1", "LN-1", "REPAYMENT", 100_000, DAY, "JE-1", credits={"1200": 25_000, "4000": 75_000}
    )

    assert compare(fineract, ledger).status == R.MATCHED


def test_reversal_notes_explain_the_outcome() -> None:
    assert compare(fin(reversed_=True), erp(reversal=100_000)).note == "reversed in both systems"
    assert "no reversal" in (compare(fin(reversed_=True), erp()).note or "")


def test_erp_reversal_entries_net_onto_the_original() -> None:
    entries = [
        {
            "external_reference": "RPY-1",
            "transaction_type": "REPAYMENT",
            "total_amount": "100000.00",
            "entry_date": "2026-10-30",
            "entry_id": "JE-000001",
            "loan_reference": "LN-1",
        },
        {
            "external_reference": "REV-RPY-1",
            "transaction_type": "REPAYMENT_REVERSAL",
            "total_amount": "100000.00",
            "entry_date": "2026-11-02",
            "entry_id": "JE-000002",
            "loan_reference": "LN-1",
        },
    ]

    side = erp_sides(entries)["RPY-1"]

    assert (side.amount, side.reversal_amount, side.net()) == (100_000, 100_000, 0)
    assert side.reversal_entry_id == "JE-000002"


def test_fineract_side_keeps_only_disbursements_and_repayments() -> None:
    accrual = {"id": 9, "type": {"id": 10}, "date": [2026, 10, 30], "amount": 500799}
    repayment = {
        "id": 10,
        "type": {"id": 2},
        "date": [2026, 10, 30],
        "amount": 458400.0,
        "externalId": "RPY-1",
        "manuallyReversed": True,
    }

    assert fineract_side(accrual, "LN-1") is None
    side = fineract_side(repayment, "LN-1")

    assert side is not None
    assert (side.external_reference, side.type, side.amount, side.reversed) == (
        "RPY-1",
        "REPAYMENT",
        458_400,
        True,
    )


def test_request_rejects_reversed_dates() -> None:
    with pytest.raises(ValidationError):
        ReconciliationRequest(from_date=date(2026, 11, 1), to_date=date(2026, 10, 1))


@pytest.fixture
def fineract() -> FakeFineractLoans:
    return FakeFineractLoans()


@pytest.fixture
def ledger() -> FakeErpJournal:
    return FakeErpJournal()


@pytest.fixture
def loans(session: Session, fineract: FakeFineractLoans, ledger: FakeErpJournal) -> LoanService:
    session.add(
        Client(client_uid="SME-000001", external_reference="CLI-1", name="U", fineract_client_id=7)
    )
    session.commit()
    return LoanService(
        session,
        fineract,
        ledger,
        product_external_id="LP-SME-ASSET-FINANCE",
        payment_type_id=1,
        today=lambda: DAY,
    )


@pytest.fixture
def service(
    session: Session, fineract: FakeFineractLoans, ledger: FakeErpJournal
) -> ReconciliationService:
    return ReconciliationService(session, fineract, ledger)


def direct_fineract_repayment(
    fineract: FakeFineractLoans, ref: str, amount: int, interest: int = 0, reversed_: bool = False
) -> None:
    fineract.transactions[1][ref] = {
        "id": 100 + len(fineract.transactions[1]),
        "type": {"id": 2},
        "date": [2026, 10, 30],
        "externalId": ref,
        "amount": f"{amount}.000000",
        "principalPortion": amount - interest,
        "interestPortion": interest,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
        "manuallyReversed": reversed_,
    }


def direct_erp_entry(
    ledger: FakeErpJournal, ref: str, amount: int, entry_date: str = "2026-10-30"
) -> None:
    ledger.entries[ref] = {
        "external_reference": ref,
        "entry_date": entry_date,
        "currency": "RWF",
        "description": "manual",
        "loan_reference": "LN-2026-000001",
        "transaction_type": "REPAYMENT",
        "lines": [
            {"account_code": "1100", "debit": amount, "credit": 0},
            {"account_code": "1200", "debit": 0, "credit": amount},
        ],
    }


@pytest.fixture
def seeded(loans: LoanService, fineract: FakeFineractLoans, ledger: FakeErpJournal) -> str:
    loan = disbursed_loan(loans)
    repay(loans, loan, "RPY-1", 600_000)
    direct_fineract_repayment(fineract, "DIRECT-1", 100_000, interest=20_000)
    direct_erp_entry(ledger, "MANUAL-1", 50_000)
    direct_fineract_repayment(fineract, "DIRECT-2", 100_000)
    direct_erp_entry(ledger, "DIRECT-2", 90_000)
    return loan


def statuses(report: dict[str, Any]) -> dict[str, str]:
    return {item["external_reference"]: item["status"] for item in report["items"]}


def test_report_covers_all_four_outcomes(service: ReconciliationService, seeded: str) -> None:
    report = service.run(ReconciliationRequest(from_date=DAY, to_date=DAY))

    assert statuses(report) == {
        "DSB-1": "MATCHED",
        "RPY-1": "MATCHED",
        "DIRECT-1": "MISSING_IN_ERP",
        "MANUAL-1": "MISSING_IN_FINERACT",
        "DIRECT-2": "AMOUNT_MISMATCH",
    }
    assert report["summary"] == {
        "MATCHED": 2,
        "MISSING_IN_ERP": 1,
        "MISSING_IN_FINERACT": 1,
        "AMOUNT_MISMATCH": 1,
        "TYPE_MISMATCH": 0,
        "SPLIT_MISMATCH": 0,
    }


def test_report_exposes_no_fineract_ids(service: ReconciliationService, seeded: str) -> None:
    report = service.run(ReconciliationRequest())

    assert all("id" not in (item["fineract"] or {}) for item in report["items"])


def test_scope_by_reference(service: ReconciliationService, seeded: str) -> None:
    report = service.run(ReconciliationRequest(external_references=["DIRECT-2"]))

    assert statuses(report) == {"DIRECT-2": "AMOUNT_MISMATCH"}


def test_scope_by_date_excludes_other_days(service: ReconciliationService, seeded: str) -> None:
    report = service.run(ReconciliationRequest(from_date=date(2026, 11, 1)))

    assert report["items"] == []


def test_counterpart_outside_the_date_range_is_still_found(
    service: ReconciliationService,
    loans: LoanService,
    fineract: FakeFineractLoans,
    ledger: FakeErpJournal,
) -> None:
    disbursed_loan(loans)
    direct_fineract_repayment(fineract, "LATE-1", 100_000)
    direct_erp_entry(ledger, "LATE-1", 100_000, entry_date="2026-11-15")

    report = service.run(ReconciliationRequest(from_date=DAY, to_date=DAY))

    assert statuses(report)["LATE-1"] == "MATCHED"


def test_repair_posts_missing_entry_from_fineract_split_and_is_idempotent(
    service: ReconciliationService, seeded: str, ledger: FakeErpJournal
) -> None:
    request = RepairRequest(external_reference="DIRECT-1", loan_reference=seeded)

    first = service.repair(request)
    second = service.repair(request)

    assert (first.status_code, first.body["action"]) == (200, "posted_to_erp")
    assert first.body["item"]["status"] == "MATCHED"
    assert (second.status_code, second.body["action"]) == (200, "none")
    lines = [
        (x["account_code"], x["debit"], x["credit"]) for x in ledger.entries["DIRECT-1"]["lines"]
    ]
    assert lines == [("1100", 100_000, 0), ("1200", 0, 80_000), ("4000", 0, 20_000)]


@pytest.mark.parametrize("reference", ["DIRECT-2", "MANUAL-1"])
def test_mismatches_are_not_repaired_automatically(
    service: ReconciliationService, seeded: str, reference: str
) -> None:
    result = service.repair(RepairRequest(external_reference=reference, loan_reference=seeded))

    assert result.status_code == 409
    assert result.body["error"]["code"] == "REPAIR_NOT_SUPPORTED"


def test_unknown_reference_has_nothing_to_repair(
    service: ReconciliationService, seeded: str
) -> None:
    result = service.repair(RepairRequest(external_reference="NOPE", loan_reference=seeded))

    assert result.status_code == 404


def test_repair_completes_a_transaction_waiting_for_reconciliation(
    service: ReconciliationService, loans: LoanService, ledger: FakeErpJournal, session: Session
) -> None:
    loan = disbursed_loan(loans)
    ledger.reject = ErpRejectedError("unknown account codes: 1200", 422)
    repay(loans, loan, "RPY-1", 600_000)
    ledger.reject = None

    result = service.repair(RepairRequest(external_reference="RPY-1", loan_reference=loan))

    assert result.body["action"] == "posted_to_erp"
    txn = session.scalars(
        select(LoanTransaction).where(LoanTransaction.external_reference == "RPY-1")
    ).one()
    assert txn.status == TransactionStatus.COMPLETED


def test_fineract_reversal_is_repaired_with_a_reversing_entry(
    service: ReconciliationService,
    loans: LoanService,
    fineract: FakeFineractLoans,
    ledger: FakeErpJournal,
) -> None:
    loan = disbursed_loan(loans)
    repay(loans, loan, "RPY-1", 600_000)
    fineract.transactions[1]["RPY-1"]["manuallyReversed"] = True

    before = service.run(ReconciliationRequest(external_references=["RPY-1"]))
    result = service.repair(RepairRequest(external_reference="RPY-1", loan_reference=loan))

    assert statuses(before) == {"RPY-1": "AMOUNT_MISMATCH"}
    assert result.body["action"] == "posted_reversal_to_erp"
    assert result.body["item"]["status"] == "MATCHED"
    assert result.body["item"]["note"] == "reversed in both systems"
    reversal = ledger.entries["REV-RPY-1"]
    assert [(x["account_code"], x["debit"], x["credit"]) for x in reversal["lines"]] == [
        ("1100", 0, 600_000),
        ("1200", 100_000, 0),
        ("4000", 500_000, 0),
    ]


@pytest.fixture
def api(service: ReconciliationService) -> Iterator[TestClient]:
    app.dependency_overrides[get_reconciliation_service] = lambda: service
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_reconciliation_endpoint(api: TestClient, seeded: str) -> None:
    response = api.post("/api/reconciliation", json={"from_date": "2026-10-30"})

    assert response.status_code == 200
    assert response.json()["summary"]["MISSING_IN_ERP"] == 1


def test_reconciliation_endpoint_validates_dates(api: TestClient) -> None:
    response = api.post(
        "/api/reconciliation", json={"from_date": "2026-11-01", "to_date": "2026-10-01"}
    )

    assert response.status_code == 422


def test_repair_endpoint(api: TestClient, seeded: str) -> None:
    body = {"external_reference": "DIRECT-1", "loan_reference": seeded}

    assert api.post("/api/reconciliation/repairs", json=body).json()["action"] == "posted_to_erp"
    assert api.post("/api/reconciliation/repairs", json=body).json()["action"] == "none"


def test_fineract_reprocessing_after_a_reversal_shows_as_split_mismatch(
    service: ReconciliationService,
    loans: LoanService,
    fineract: FakeFineractLoans,
) -> None:
    loan = disbursed_loan(loans)
    repay(loans, loan, "RPY-1", 600_000)
    recorded = fineract.transactions[1]["RPY-1"]
    recorded["principalPortion"], recorded["interestPortion"] = 200_000, 400_000

    report = service.run(ReconciliationRequest(external_references=["RPY-1"]))

    assert statuses(report) == {"RPY-1": "SPLIT_MISMATCH"}
    assert (
        service.repair(RepairRequest(external_reference="RPY-1", loan_reference=loan)).status_code
        == 409
    )
