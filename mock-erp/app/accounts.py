from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Account
from app.schemas import AccountIn, AccountOut, AccountType

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

SEED_ACCOUNTS = [
    AccountIn(
        code="1100",
        name="Bank",
        type=AccountType.ASSET,
        currency="RWF",
        external_reference="GL-1100",
    ),
    AccountIn(
        code="1200",
        name="Loan Receivable",
        type=AccountType.ASSET,
        currency="RWF",
        external_reference="GL-1200",
    ),
    AccountIn(
        code="4000",
        name="Interest Income",
        type=AccountType.INCOME,
        currency="RWF",
        external_reference="GL-4000",
    ),
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


@router.get("/{code}", response_model=AccountOut)
def get_account(code: str, session: Annotated[Session, Depends(get_session)]) -> Account:
    account = session.scalar(select(Account).where(Account.code == code))
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"account {code} not found")
    return account
