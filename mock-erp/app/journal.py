import hashlib
import json
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account, JournalEntry, JournalLine
from app.schemas import JournalEntryIn, JournalEntryOut

router = APIRouter(prefix="/api/journal-entries", tags=["journal entries"])


class JournalConflictError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class JournalValidationError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def payload_hash(data: JournalEntryIn) -> str:
    canonical = json.dumps(data.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def find_entry(session: Session, external_reference: str) -> JournalEntry | None:
    return session.scalar(
        select(JournalEntry).where(JournalEntry.external_reference == external_reference)
    )


def resolve_existing(session: Session, data: JournalEntryIn, digest: str) -> JournalEntry | None:
    existing = find_entry(session, data.external_reference)
    if existing is None:
        return None
    if existing.payload_hash != digest:
        raise JournalConflictError(
            f"external_reference {data.external_reference} already exists with a different payload"
        )
    return existing


def load_accounts(session: Session, data: JournalEntryIn) -> dict[str, Account]:
    codes = {line.account_code for line in data.lines}
    accounts = {a.code: a for a in session.scalars(select(Account).where(Account.code.in_(codes)))}
    missing = sorted(codes - accounts.keys())
    if missing:
        raise JournalValidationError(f"unknown account codes: {', '.join(missing)}")
    wrong_currency = sorted(a.code for a in accounts.values() if a.currency != data.currency)
    if wrong_currency:
        raise JournalValidationError(
            f"accounts {', '.join(wrong_currency)} are not in currency {data.currency}"
        )
    return accounts


def create_entry(session: Session, data: JournalEntryIn) -> tuple[JournalEntry, bool]:
    digest = payload_hash(data)
    existing = resolve_existing(session, data, digest)
    if existing is not None:
        return existing, False
    accounts = load_accounts(session, data)
    entry = JournalEntry(
        external_reference=data.external_reference,
        entry_date=data.entry_date,
        currency=data.currency,
        description=data.description,
        loan_reference=data.loan_reference,
        transaction_type=data.transaction_type,
        total_amount=sum((line.debit for line in data.lines), Decimal("0")),
        payload_hash=digest,
        lines=[
            JournalLine(
                line_number=number,
                account=accounts[line.account_code],
                debit=line.debit,
                credit=line.credit,
            )
            for number, line in enumerate(data.lines, start=1)
        ],
    )
    session.add(entry)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = resolve_existing(session, data, digest)
        if existing is None:
            raise
        return existing, False
    return entry, True


@router.post("", response_model=JournalEntryOut, status_code=status.HTTP_201_CREATED)
def post_journal_entry(
    data: JournalEntryIn, response: Response, session: Annotated[Session, Depends(get_session)]
) -> JournalEntry:
    try:
        entry, created = create_entry(session, data)
    except JournalConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, error.message) from error
    except JournalValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, error.message) from error
    if not created:
        response.status_code = status.HTTP_200_OK
    return entry


@router.get("/{external_reference}", response_model=JournalEntryOut)
def get_journal_entry(
    external_reference: str, session: Annotated[Session, Depends(get_session)]
) -> JournalEntry:
    entry = find_entry(session, external_reference)
    if entry is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"journal entry {external_reference} not found"
        )
    return entry
