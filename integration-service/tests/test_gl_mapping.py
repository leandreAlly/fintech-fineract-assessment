from typing import Any

import pytest

from app.gateways.erp import ErpAccount
from app.gl_mapping import (
    ERP_TO_FINERACT_GL_TYPE,
    FineractGlType,
    UnsupportedAccountTypeError,
    fineract_gl_payload,
    fineract_gl_type,
)


@pytest.mark.parametrize(
    ("erp_type", "expected"),
    [
        ("ASSET", 1),
        ("LIABILITY", 2),
        ("EQUITY", 3),
        ("INCOME", 4),
        ("EXPENSE", 5),
    ],
)
def test_each_erp_type_maps_to_fineract_type_id(erp_type: str, expected: int) -> None:
    assert fineract_gl_type(erp_type) == expected


def test_mapping_covers_exactly_the_erp_types() -> None:
    assert set(ERP_TO_FINERACT_GL_TYPE) == {"ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"}
    assert set(ERP_TO_FINERACT_GL_TYPE.values()) == set(FineractGlType)


@pytest.mark.parametrize("erp_type", ["CASH", "asset", "", " ASSET"])
def test_unknown_or_malformed_type_is_rejected(erp_type: str) -> None:
    with pytest.raises(UnsupportedAccountTypeError) as raised:
        fineract_gl_type(erp_type)

    assert raised.value.erp_type == erp_type


def test_mapping_cannot_be_changed_at_runtime() -> None:
    mapping: Any = ERP_TO_FINERACT_GL_TYPE

    with pytest.raises(TypeError):
        mapping["CASH"] = FineractGlType.ASSET


def test_payload_for_loan_receivable() -> None:
    account = ErpAccount(
        code="1200",
        name="Loan Receivable",
        type="ASSET",
        currency="RWF",
        external_reference="GL-1200",
    )

    assert fineract_gl_payload(account) == {
        "externalId": "GL-1200",
        "glCode": "1200",
        "name": "Loan Receivable",
        "type": 1,
        "usage": 1,
        "manualEntriesAllowed": True,
    }


def test_payload_for_income_account_uses_income_type() -> None:
    account = ErpAccount(
        code="4000",
        name="Interest Income",
        type="INCOME",
        currency="RWF",
        external_reference="GL-4000",
    )

    assert fineract_gl_payload(account)["type"] == 4
