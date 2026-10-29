from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class AccountIn(BaseModel):
    code: str = Field(min_length=1, max_length=20, pattern=r"^[0-9A-Za-z.-]+$")
    name: str = Field(min_length=1, max_length=100)
    type: AccountType
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    external_reference: str = Field(min_length=1, max_length=100)


class AccountOut(AccountIn):
    model_config = ConfigDict(from_attributes=True)

    created_at: datetime
