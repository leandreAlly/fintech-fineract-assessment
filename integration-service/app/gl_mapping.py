from collections.abc import Mapping
from enum import IntEnum
from types import MappingProxyType
from typing import Any

from app.gateways.erp import ErpAccount


class FineractGlType(IntEnum):
    ASSET = 1
    LIABILITY = 2
    EQUITY = 3
    INCOME = 4
    EXPENSE = 5


class FineractGlUsage(IntEnum):
    DETAIL = 1
    HEADER = 2


ERP_TO_FINERACT_GL_TYPE: Mapping[str, FineractGlType] = MappingProxyType(
    {
        "ASSET": FineractGlType.ASSET,
        "LIABILITY": FineractGlType.LIABILITY,
        "EQUITY": FineractGlType.EQUITY,
        "INCOME": FineractGlType.INCOME,
        "EXPENSE": FineractGlType.EXPENSE,
    }
)


class UnsupportedAccountTypeError(ValueError):
    def __init__(self, erp_type: str) -> None:
        super().__init__(f"ERP account type {erp_type!r} has no Fineract GL type")
        self.erp_type = erp_type


def fineract_gl_type(erp_type: str) -> FineractGlType:
    try:
        return ERP_TO_FINERACT_GL_TYPE[erp_type]
    except KeyError:
        raise UnsupportedAccountTypeError(erp_type) from None


def fineract_gl_payload(account: ErpAccount) -> dict[str, Any]:
    return {
        "externalId": account.external_reference,
        "glCode": account.code,
        "name": account.name,
        "type": int(fineract_gl_type(account.type)),
        "usage": int(FineractGlUsage.DETAIL),
        "manualEntriesAllowed": True,
    }
