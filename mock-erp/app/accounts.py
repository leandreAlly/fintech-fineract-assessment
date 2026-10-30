from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account, JournalLine
from app.schemas import (
    CENT,
    DEBIT_NORMAL_TYPES,
    AccountBalance,
    AccountIn,
    AccountOut,
    AccountType,
)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

SEED_ACCOUNTS = [
    AccountIn(
        code=code, name=name, type=account_type, currency="RWF", external_reference=f"GL-{code}"
    )
    for code, name, account_type in [
        ("1100", "Bank", AccountType.ASSET),
        ("1200", "Loan Receivable", AccountType.ASSET),
        ("1300", "Transfers in Suspense", AccountType.ASSET),
        ("2100", "Overpayment Liability", AccountType.LIABILITY),
        ("4000", "Interest Income", AccountType.INCOME),
        ("4100", "Fee Income", AccountType.INCOME),
        ("4200", "Penalty Income", AccountType.INCOME),
        ("4300", "Recovery Income", AccountType.INCOME),
        ("5100", "Loan Write-off Expense", AccountType.EXPENSE),
        ("5200", "Goodwill Credit Expense", AccountType.EXPENSE),
    ]
]


class AccountConflictError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def same_payload(account: Account, data: AccountIn) -> bool:
    return (
        account.code == data.code
        and account.name == data.name
        and account.type == data.type
        and account.currency == data.currency
    )


def find_by_reference(session: Session, external_reference: str) -> Account | None:
    return session.scalar(select(Account).where(Account.external_reference == external_reference))


def resolve_existing(session: Session, data: AccountIn) -> Account | None:
    existing = find_by_reference(session, data.external_reference)
    if existing is None:
        return None
    if not same_payload(existing, data):
        raise AccountConflictError(
            f"external_reference {data.external_reference} already exists with a different payload"
        )
    return existing


def create_account(session: Session, data: AccountIn) -> tuple[Account, bool]:
    existing = resolve_existing(session, data)
    if existing is not None:
        return existing, False
    account = Account(**data.model_dump())
    session.add(account)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = resolve_existing(session, data)
        if existing is not None:
            return existing, False
        raise AccountConflictError(f"account code {data.code} already exists") from None
    return account, True


def seed_accounts(session: Session) -> None:
    for data in SEED_ACCOUNTS:
        create_account(session, data)


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def post_account(
    data: AccountIn, response: Response, session: Annotated[Session, Depends(get_session)]
) -> Account:
    try:
        account, created = create_account(session, data)
    except AccountConflictError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, error.message) from error
    if not created:
        response.status_code = status.HTTP_200_OK
    return account


@router.get("", response_model=list[AccountOut])
def list_accounts(session: Annotated[Session, Depends(get_session)]) -> list[Account]:
    return list(session.scalars(select(Account).order_by(Account.code)))


def account_or_404(session: Session, code: str) -> Account:
    account = session.scalar(select(Account).where(Account.code == code))
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"account {code} not found")
    return account


def account_balance(session: Session, account: Account) -> AccountBalance:
    debits, credits, count = session.execute(
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
            func.count(JournalLine.id),
        ).where(JournalLine.account_id == account.id)
    ).one()
    total_debits = Decimal(debits).quantize(CENT)
    total_credits = Decimal(credits).quantize(CENT)
    account_type = AccountType(account.type)
    if account_type in DEBIT_NORMAL_TYPES:
        balance = total_debits - total_credits
    else:
        balance = total_credits - total_debits
    return AccountBalance(
        code=account.code,
        name=account.name,
        type=account_type,
        currency=account.currency,
        total_debits=total_debits,
        total_credits=total_credits,
        balance=balance,
        line_count=count,
    )


@router.get("/{code}", response_model=AccountOut)
def get_account(code: str, session: Annotated[Session, Depends(get_session)]) -> Account:
    return account_or_404(session, code)


@router.get("/{code}/balance", response_model=AccountBalance)
def get_account_balance(
    code: str, session: Annotated[Session, Depends(get_session)]
) -> AccountBalance:
    return account_balance(session, account_or_404(session, code))
