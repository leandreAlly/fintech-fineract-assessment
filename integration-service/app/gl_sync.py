from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.gateways.erp import ErpAccount
from app.gateways.fineract import FineractRejectedError, FineractUnavailableError
from app.gl_mapping import UnsupportedAccountTypeError, fineract_gl_payload
from app.models import GlAccountSync, SyncStatus


class ErpAccounts(Protocol):
    def list_accounts(self) -> list[ErpAccount]: ...

    def get_account_by_reference(self, external_reference: str) -> ErpAccount | None: ...


class FineractGlAccounts(Protocol):
    def sync_gl_account(self, payload: dict[str, Any]) -> int: ...


class AccountNotFoundError(Exception):
    def __init__(self, external_reference: str) -> None:
        super().__init__(f"ERP has no account with external_reference {external_reference}")
        self.external_reference = external_reference


def utc_now() -> datetime:
    return datetime.now(UTC)


def backoff_delay(attempts: int, base_seconds: float, max_seconds: float) -> timedelta:
    return timedelta(seconds=min(max_seconds, base_seconds * 2 ** (attempts - 1)))


class GlSyncService:
    def __init__(
        self,
        session: Session,
        erp: ErpAccounts,
        fineract: FineractGlAccounts,
        *,
        max_attempts: int,
        backoff_base_seconds: float,
        backoff_max_seconds: float,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.session = session
        self.erp = erp
        self.fineract = fineract
        self.max_attempts = max_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.clock = clock

    def sync(self, external_reference: str) -> GlAccountSync:
        row = self.find(external_reference)
        if row is None:
            account = self.erp.get_account_by_reference(external_reference)
            if account is None:
                raise AccountNotFoundError(external_reference)
            row = self._get_or_create(account)
        locked = self._lock(row.id)
        if locked.status == SyncStatus.FAILED:
            locked.status = SyncStatus.SYNC_PENDING
            locked.attempts = 0
        if locked.status == SyncStatus.SYNC_PENDING:
            self._attempt(locked)
        self.session.commit()
        return locked

    def sync_all(self) -> list[GlAccountSync]:
        return [self.sync(account.external_reference) for account in self.erp.list_accounts()]

    def retry_due(self, limit: int = 100) -> int:
        processed = 0
        while processed < limit:
            row = self.session.scalar(
                select(GlAccountSync)
                .where(GlAccountSync.status == SyncStatus.SYNC_PENDING)
                .where(
                    or_(
                        GlAccountSync.next_attempt_at.is_(None),
                        GlAccountSync.next_attempt_at <= self.clock(),
                    )
                )
                .order_by(GlAccountSync.next_attempt_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                break
            self._attempt(row)
            self.session.commit()
            processed += 1
        return processed

    def find(self, external_reference: str) -> GlAccountSync | None:
        return self.session.scalar(
            select(GlAccountSync).where(GlAccountSync.external_reference == external_reference)
        )

    def _get_or_create(self, account: ErpAccount) -> GlAccountSync:
        row = GlAccountSync(
            external_reference=account.external_reference,
            erp_code=account.code,
            erp_name=account.name,
            erp_type=account.type,
            currency=account.currency,
            status=SyncStatus.SYNC_PENDING,
            attempts=0,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.find(account.external_reference)
            if existing is None:
                raise
            return existing
        return row

    def _lock(self, row_id: int) -> GlAccountSync:
        return self.session.execute(
            select(GlAccountSync)
            .where(GlAccountSync.id == row_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

    def _attempt(self, row: GlAccountSync) -> None:
        account = ErpAccount(
            code=row.erp_code,
            name=row.erp_name,
            type=row.erp_type,
            currency=row.currency,
            external_reference=row.external_reference,
        )
        try:
            payload = fineract_gl_payload(account)
        except UnsupportedAccountTypeError as error:
            self._fail(row, str(error))
            return
        row.attempts += 1
        try:
            row.fineract_gl_account_id = self.fineract.sync_gl_account(payload)
        except FineractUnavailableError as error:
            self._schedule_retry(row, error.message)
        except FineractRejectedError as error:
            self._fail(row, f"Fineract rejected the account ({error.status_code}): {error.message}")
        else:
            row.status = SyncStatus.SYNCED
            row.last_error = None
            row.next_attempt_at = None

    def _schedule_retry(self, row: GlAccountSync, message: str) -> None:
        if row.attempts >= self.max_attempts:
            self._fail(row, f"gave up after {row.attempts} attempts: {message}")
            return
        row.status = SyncStatus.SYNC_PENDING
        row.last_error = message
        row.next_attempt_at = self.clock() + backoff_delay(
            row.attempts, self.backoff_base_seconds, self.backoff_max_seconds
        )

    def _fail(self, row: GlAccountSync, message: str) -> None:
        row.status = SyncStatus.FAILED
        row.last_error = message
        row.next_attempt_at = None
