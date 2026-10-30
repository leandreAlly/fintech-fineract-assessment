from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import get_loan_service
from app.gateways.fineract import FineractUnavailableError
from app.loan_service import LoanService
from app.main import app
from app.models import Client, LoanTransaction, TransactionStatus
from app.retry import RetryPolicy
from tests.fakes import FakeErpJournal, FakeFineractLoans
from tests.test_loan_service import disbursed_loan, repay

S = TransactionStatus


class SimulatedCrashError(Exception):
    pass


class MovingClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> MovingClock:
    return MovingClock()


@pytest.fixture
def fineract() -> FakeFineractLoans:
    return FakeFineractLoans()


@pytest.fixture
def erp() -> FakeErpJournal:
    return FakeErpJournal()


@pytest.fixture
def service(
    session: Session, fineract: FakeFineractLoans, erp: FakeErpJournal, clock: MovingClock
) -> LoanService:
    session.add(
        Client(client_uid="SME-000001", external_reference="CLI-1", name="U", fineract_client_id=7)
    )
    session.commit()
    return LoanService(
        session,
        fineract,
        erp,
        product_external_id="LP-SME-ASSET-FINANCE",
        payment_type_id=1,
        retry=RetryPolicy(max_attempts=3, backoff_base_seconds=2, abandon_after_seconds=900),
        today=lambda: date(2026, 10, 30),
        clock=clock,
    )


def txn(session: Session, reference: str) -> LoanTransaction:
    return session.scalars(
        select(LoanTransaction)
        .where(LoanTransaction.external_reference == reference)
        .execution_options(populate_existing=True)
    ).one()


def received_at(session: Session, reference: str, at: datetime) -> None:
    session.execute(
        update(LoanTransaction)
        .where(LoanTransaction.external_reference == reference)
        .values(created_at=at)
    )
    session.commit()


def test_erp_retry_waits_for_its_backoff(
    service: LoanService, session: Session, erp: FakeErpJournal, clock: MovingClock
) -> None:
    loan = disbursed_loan(service)
    erp.down = True
    repay(service, loan, "RPY-1", 600_000)
    erp.down = False

    assert service.recover()["checked"] == 0
    clock.advance(2)
    report = service.recover()

    assert report["transactions"][0] | {"note": None} == {
        "external_reference": "RPY-1",
        "from": S.ERP_PENDING,
        "to": S.COMPLETED,
        "erp_attempts": 2,
        "note": None,
    }
    assert list(erp.entries) == ["DSB-1", "RPY-1"]


def test_erp_backoff_doubles(
    service: LoanService, session: Session, erp: FakeErpJournal, clock: MovingClock
) -> None:
    loan = disbursed_loan(service)
    erp.down = True
    repay(service, loan, "RPY-1", 600_000)
    first = txn(session, "RPY-1").next_attempt_at
    clock.advance(2)
    service.recover()
    second = txn(session, "RPY-1").next_attempt_at

    assert first is not None and second is not None
    assert first.replace(tzinfo=UTC) == datetime(2026, 10, 30, 12, 0, 2, tzinfo=UTC)
    assert second.replace(tzinfo=UTC) == datetime(2026, 10, 30, 12, 0, 6, tzinfo=UTC)


def test_erp_down_past_max_attempts_needs_reconciliation(
    service: LoanService, session: Session, erp: FakeErpJournal, clock: MovingClock
) -> None:
    loan = disbursed_loan(service)
    erp.down = True
    repay(service, loan, "RPY-1", 600_000)
    for _ in range(5):
        clock.advance(60)
        service.recover()

    row = txn(session, "RPY-1")
    assert row.status == S.NEEDS_RECONCILIATION
    assert row.erp_attempts == 3
    assert "after 3 attempts" in (row.last_error or "")
    assert row.next_attempt_at is None


def test_crash_after_fineract_applied_is_completed_by_the_sweep(
    service: LoanService, session: Session, fineract: FakeFineractLoans, erp: FakeErpJournal
) -> None:
    loan = disbursed_loan(service)
    fineract.fail_after["repay_loan"] = SimulatedCrashError()

    with pytest.raises(SimulatedCrashError):
        repay(service, loan, "RPY-1", 600_000)
    assert txn(session, "RPY-1").status == S.FINERACT_PENDING

    report = service.recover()

    assert [(t["from"], t["to"]) for t in report["transactions"]] == [
        (S.FINERACT_PENDING, S.COMPLETED)
    ]
    assert fineract.calls["repay_loan"] == 1
    assert list(erp.entries) == ["DSB-1", "RPY-1"]
    assert txn(session, "RPY-1").principal_portion == 100_000


def test_crash_between_fineract_success_and_erp_call_is_completed_by_the_sweep(
    service: LoanService, session: Session, fineract: FakeFineractLoans, erp: FakeErpJournal
) -> None:
    loan = disbursed_loan(service)
    erp.fail_before = SimulatedCrashError()

    with pytest.raises(SimulatedCrashError):
        repay(service, loan, "RPY-1", 600_000)
    assert txn(session, "RPY-1").status == S.FINERACT_DONE
    assert "RPY-1" not in erp.entries

    report = service.recover()

    assert [(t["from"], t["to"]) for t in report["transactions"]] == [
        (S.FINERACT_DONE, S.COMPLETED)
    ]
    assert fineract.calls["repay_loan"] == 1
    assert list(erp.entries) == ["DSB-1", "RPY-1"]


def test_sweep_never_sends_to_fineract_it_only_looks_up(
    service: LoanService, session: Session, fineract: FakeFineractLoans, clock: MovingClock
) -> None:
    loan = disbursed_loan(service)
    fineract.fail_before["repay_loan"] = FineractUnavailableError("ConnectError: refused")
    with pytest.raises(FineractUnavailableError):
        repay(service, loan, "RPY-1", 600_000)

    clock.advance(600)
    report = service.recover()

    assert report["transactions"][0]["to"] == S.FINERACT_PENDING
    assert fineract.calls["repay_loan"] == 1
    assert "RPY-1" not in fineract.transactions[1]


def test_never_applied_transaction_is_abandoned_after_the_window(
    service: LoanService, session: Session, fineract: FakeFineractLoans, clock: MovingClock
) -> None:
    loan = disbursed_loan(service)
    fineract.fail_before["repay_loan"] = FineractUnavailableError("ConnectError: refused")
    with pytest.raises(FineractUnavailableError):
        repay(service, loan, "RPY-1", 600_000)
    received_at(session, "RPY-1", clock())

    clock.advance(901)
    service.recover()

    row = txn(session, "RPY-1")
    assert row.status == S.FAILED
    assert "never applied" in (row.last_error or "")


def test_caller_retry_within_the_window_still_applies_it(
    service: LoanService, fineract: FakeFineractLoans, clock: MovingClock
) -> None:
    loan = disbursed_loan(service)
    fineract.fail_before["repay_loan"] = FineractUnavailableError("ConnectError: refused")
    with pytest.raises(FineractUnavailableError):
        repay(service, loan, "RPY-1", 600_000)
    service.recover()

    assert repay(service, loan, "RPY-1", 600_000)["status"] == "COMPLETED"
    assert fineract.calls["repay_loan"] == 2


def test_fineract_down_during_the_sweep_leaves_state_unchanged(
    service: LoanService, session: Session, fineract: FakeFineractLoans
) -> None:
    loan = disbursed_loan(service)
    fineract.fail_after["repay_loan"] = SimulatedCrashError()
    with pytest.raises(SimulatedCrashError):
        repay(service, loan, "RPY-1", 600_000)

    original = fineract.loan_transaction_by_external_id

    def down(loan_id: int, external_id: str) -> None:
        raise FineractUnavailableError("ConnectError: refused")

    fineract.loan_transaction_by_external_id = down  # type: ignore[method-assign]
    report = service.recover()
    fineract.loan_transaction_by_external_id = original  # type: ignore[method-assign]

    assert report["transactions"][0]["to"] == S.FINERACT_PENDING
    assert "refused" in report["transactions"][0]["note"]
    assert service.recover()["transactions"][0]["to"] == S.COMPLETED


def test_finished_transactions_are_not_swept(service: LoanService) -> None:
    loan = disbursed_loan(service)
    repay(service, loan, "RPY-1", 600_000)

    assert service.recover() == {"checked": 0, "transactions": []}


@pytest.fixture
def admin(session: Session, service: LoanService) -> Iterator[TestClient]:
    app.dependency_overrides[get_loan_service] = lambda: service
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_admin_recovery_and_pending_list(
    admin: TestClient, service: LoanService, erp: FakeErpJournal, clock: MovingClock
) -> None:
    loan = disbursed_loan(service)
    erp.down = True
    repay(service, loan, "RPY-1", 600_000)

    pending = admin.get("/api/admin/transactions", params={"status": "ERP_PENDING"}).json()
    erp.down = False
    clock.advance(2)
    report = admin.post("/api/admin/recovery").json()

    assert [t["external_reference"] for t in pending] == ["RPY-1"]
    assert report["transactions"][0]["to"] == "COMPLETED"
    assert admin.get("/api/admin/transactions", params={"status": "ERP_PENDING"}).json() == []
