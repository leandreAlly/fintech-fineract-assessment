from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.gateways.erp import ErpAccount
from app.gateways.fineract import FineractRejectedError, FineractUnavailableError
from app.gl_sync import AccountNotFoundError, GlSyncService, backoff_delay
from app.models import GlAccountSync, SyncStatus
from tests.fakes import INTEREST_INCOME, LOAN_RECEIVABLE, Clock, FakeErp, FakeFineract


@pytest.fixture
def erp() -> FakeErp:
    return FakeErp(LOAN_RECEIVABLE, INTEREST_INCOME)


@pytest.fixture
def fineract() -> FakeFineract:
    return FakeFineract()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(session: Session, erp: FakeErp, fineract: FakeFineract, clock: Clock) -> GlSyncService:
    return GlSyncService(
        session,
        erp,
        fineract,
        max_attempts=3,
        backoff_base_seconds=2,
        backoff_max_seconds=60,
        clock=clock,
    )


def row_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(GlAccountSync)) or 0


def unavailable() -> FineractUnavailableError:
    return FineractUnavailableError("ConnectError: connection refused")


def test_first_sync_creates_gl_account_and_marks_synced(
    service: GlSyncService, fineract: FakeFineract
) -> None:
    row = service.sync("GL-1200")

    assert row.status == SyncStatus.SYNCED
    assert row.attempts == 1
    assert row.fineract_gl_account_id == 6
    assert fineract.calls == [
        {
            "externalId": "GL-1200",
            "glCode": "1200",
            "name": "Loan Receivable",
            "type": 1,
            "usage": 1,
            "manualEntriesAllowed": True,
        }
    ]


def test_same_account_four_times_gives_one_mapping_and_one_fineract_call(
    service: GlSyncService, session: Session, fineract: FakeFineract
) -> None:
    rows = [service.sync("GL-1200") for _ in range(4)]

    assert {row.status for row in rows} == {SyncStatus.SYNCED}
    assert {row.id for row in rows} == {rows[0].id}
    assert row_count(session) == 1
    assert len(fineract.calls) == 1


def test_unknown_reference_is_not_found(service: GlSyncService, session: Session) -> None:
    with pytest.raises(AccountNotFoundError):
        service.sync("GL-9999")

    assert row_count(session) == 0


def test_fineract_down_keeps_pending_and_schedules_retry(
    service: GlSyncService, fineract: FakeFineract, clock: Clock
) -> None:
    fineract.failures = [unavailable()]

    row = service.sync("GL-1200")

    assert row.status == SyncStatus.SYNC_PENDING
    assert row.attempts == 1
    assert row.last_error == "ConnectError: connection refused"
    assert row.next_attempt_at == clock.now + timedelta(seconds=2)


def test_retry_waits_for_backoff_then_syncs(
    service: GlSyncService, fineract: FakeFineract, clock: Clock
) -> None:
    fineract.failures = [unavailable()]
    row = service.sync("GL-1200")

    clock.advance(1)
    assert service.retry_due() == 0
    clock.advance(1)
    assert service.retry_due() == 1

    assert row.status == SyncStatus.SYNCED
    assert row.attempts == 2
    assert row.last_error is None
    assert row.next_attempt_at is None


def test_retry_gives_up_after_max_attempts(
    service: GlSyncService, fineract: FakeFineract, clock: Clock
) -> None:
    fineract.failures = [unavailable() for _ in range(3)]
    row = service.sync("GL-1200")
    clock.advance(2)
    service.retry_due()
    clock.advance(4)
    service.retry_due()

    assert row.status == SyncStatus.FAILED
    assert row.attempts == 3
    assert row.last_error is not None and row.last_error.startswith("gave up after 3 attempts")
    clock.advance(3600)
    assert service.retry_due() == 0


def test_fineract_rejection_fails_without_retry(
    service: GlSyncService, fineract: FakeFineract, clock: Clock
) -> None:
    fineract.failures = [FineractRejectedError("different payload", 409)]

    row = service.sync("GL-1200")

    assert row.status == SyncStatus.FAILED
    assert row.attempts == 1
    assert row.last_error == "Fineract rejected the account (409): different payload"
    clock.advance(3600)
    assert service.retry_due() == 0


def test_unsupported_type_fails_without_calling_fineract(
    service: GlSyncService, erp: FakeErp, fineract: FakeFineract
) -> None:
    erp.accounts["GL-7000"] = ErpAccount(
        code="7000", name="Petty Cash", type="CASH", currency="RWF", external_reference="GL-7000"
    )

    row = service.sync("GL-7000")

    assert row.status == SyncStatus.FAILED
    assert row.attempts == 0
    assert fineract.calls == []


def test_explicit_sync_of_failed_account_starts_a_fresh_round(
    service: GlSyncService, fineract: FakeFineract
) -> None:
    fineract.failures = [FineractRejectedError("Fineract misconfigured", 403)]
    service.sync("GL-1200")

    row = service.sync("GL-1200")

    assert row.status == SyncStatus.SYNCED
    assert row.attempts == 1
    assert len(fineract.calls) == 2


def test_sync_all_syncs_every_erp_account(service: GlSyncService, session: Session) -> None:
    rows = service.sync_all()

    assert [(r.external_reference, r.status) for r in rows] == [
        ("GL-1200", SyncStatus.SYNCED),
        ("GL-4000", SyncStatus.SYNCED),
    ]
    assert row_count(session) == 2


@pytest.mark.parametrize(
    ("attempts", "seconds"), [(1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 60), (10, 60)]
)
def test_backoff_doubles_and_is_capped(attempts: int, seconds: int) -> None:
    assert backoff_delay(attempts, 2, 60) == timedelta(seconds=seconds)
