from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CENT = Decimal("0.01")


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


DEBIT_NORMAL_TYPES = frozenset({AccountType.ASSET, AccountType.EXPENSE})


class AccountIn(BaseModel):
    code: str = Field(min_length=1, max_length=20, pattern=r"^[0-9A-Za-z.-]+$")
    name: str = Field(min_length=1, max_length=100)
    type: AccountType
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    external_reference: str = Field(min_length=1, max_length=100)


class AccountOut(AccountIn):
    model_config = ConfigDict(from_attributes=True)

    created_at: datetime


class AccountBalance(BaseModel):
    code: str
    name: str
    type: AccountType
    currency: str
    total_debits: Decimal
    total_credits: Decimal
    balance: Decimal
    line_count: int


class JournalLineIn(BaseModel):
    account_code: str = Field(min_length=1, max_length=20)
    debit: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=18, decimal_places=2)
    credit: Decimal = Field(default=Decimal("0.00"), ge=0, max_digits=18, decimal_places=2)

    @field_validator("debit", "credit")
    @classmethod
    def to_cents(cls, value: Decimal) -> Decimal:
        return value.quantize(CENT)

    @model_validator(mode="after")
    def one_side_only(self) -> Self:
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("each line must have exactly one of debit or credit greater than zero")
        return self


class JournalEntryIn(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    entry_date: date
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    description: str = Field(default="", max_length=255)
    loan_reference: str | None = Field(default=None, max_length=100)
    transaction_type: str | None = Field(default=None, max_length=40)
    lines: list[JournalLineIn] = Field(min_length=2, max_length=50)

    @model_validator(mode="after")
    def balanced(self) -> Self:
        debits = sum((line.debit for line in self.lines), Decimal("0"))
        credits = sum((line.credit for line in self.lines), Decimal("0"))
        if debits != credits:
            raise ValueError(f"entry is not balanced: debits {debits} != credits {credits}")
        return self


class JournalLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_code: str
    debit: Decimal
    credit: Decimal


class JournalEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    external_reference: str
    entry_date: date
    currency: str
    description: str
    loan_reference: str | None
    transaction_type: str | None
    total_amount: Decimal
    lines: list[JournalLineOut]
    created_at: datetime
