from datetime import date
from typing import Any

import pytest

from app.accounting import (
    Breakdown,
    JournalLine,
    MappingError,
    PostedTransaction,
    amount_from_fineract,
    breakdown_from_fineract,
    journal_entry_for,
    reversal_of,
)
from app.models import TransactionType


def posted(
    type_: TransactionType = TransactionType.REPAYMENT,
    amount: int = 458_400,
    **portions: int,
) -> PostedTransaction:
    breakdown = Breakdown(**portions) if portions else Breakdown(principal=383_400, interest=75_000)
    return PostedTransaction(
        external_reference="RPY-1",
        loan_reference="LN-2026-000001",
        type=type_,
        transaction_date=date(2026, 10, 30),
        currency="RWF",
        amount=amount,
        breakdown=breakdown,
    )


def lines(entry_lines: tuple[JournalLine, ...]) -> list[tuple[str, int, int]]:
    return [(line.account_code, line.debit, line.credit) for line in entry_lines]


def test_disbursement_debits_loan_receivable_and_credits_bank() -> None:
    entry = journal_entry_for(posted(TransactionType.DISBURSEMENT, 5_000_000, principal=0))

    assert lines(entry.lines) == [("1200", 5_000_000, 0), ("1100", 0, 5_000_000)]
    assert entry.transaction_type == "DISBURSEMENT"


def test_repayment_splits_principal_and_interest_from_fineract() -> None:
    entry = journal_entry_for(posted())

    assert lines(entry.lines) == [
        ("1100", 458_400, 0),
        ("1200", 0, 383_400),
        ("4000", 0, 75_000),
    ]


def test_principal_only_repayment_has_no_interest_line() -> None:
    entry = journal_entry_for(posted(amount=100_000, principal=100_000))

    assert lines(entry.lines) == [("1100", 100_000, 0), ("1200", 0, 100_000)]


def test_fees_and_penalties_go_to_their_income_accounts() -> None:
    entry = journal_entry_for(
        posted(amount=500_000, principal=400_000, interest=70_000, fees=20_000, penalties=10_000)
    )

    assert lines(entry.lines) == [
        ("1100", 500_000, 0),
        ("1200", 0, 400_000),
        ("4000", 0, 70_000),
        ("4100", 0, 20_000),
        ("4200", 0, 10_000),
    ]


def test_overpayment_is_a_liability_not_income() -> None:
    entry = journal_entry_for(
        posted(amount=3_800_000, principal=3_506_748, interest=235_651, overpayment=57_601)
    )

    assert ("2100", 0, 57_601) in lines(entry.lines)


@pytest.mark.parametrize(
    "txn",
    [
        posted(TransactionType.DISBURSEMENT, 5_000_000, principal=0),
        posted(),
        posted(amount=1_000_000, principal=879_101, interest=120_899),
        posted(amount=500_000, principal=400_000, interest=70_000, fees=20_000, penalties=10_000),
        posted(amount=3_800_000, principal=3_506_748, interest=235_651, overpayment=57_601),
    ],
)
def test_every_entry_is_balanced(txn: PostedTransaction) -> None:
    entry = journal_entry_for(txn)

    assert entry.total_debits() == entry.total_credits() == txn.amount


def test_portions_that_do_not_add_up_are_rejected() -> None:
    with pytest.raises(MappingError, match="add up to 453400"):
        journal_entry_for(posted(amount=458_400, principal=383_400, interest=70_000))


def test_negative_portion_is_rejected() -> None:
    with pytest.raises(MappingError, match="negative"):
        journal_entry_for(posted(amount=100_000, principal=150_000, interest=-50_000))


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amount_is_rejected(amount: int) -> None:
    with pytest.raises(MappingError, match="positive"):
        journal_entry_for(posted(TransactionType.DISBURSEMENT, amount, principal=0))


def test_entry_keeps_the_transaction_reference_for_reconciliation() -> None:
    entry = journal_entry_for(posted())

    assert entry.external_reference == "RPY-1"
    assert entry.loan_reference == "LN-2026-000001"
    assert entry.entry_date == date(2026, 10, 30)


def test_mapping_is_pure() -> None:
    txn = posted()

    assert journal_entry_for(txn) == journal_entry_for(txn)


def test_payload_matches_the_erp_contract() -> None:
    payload = journal_entry_for(posted()).as_payload()

    assert payload["external_reference"] == "RPY-1"
    assert payload["entry_date"] == "2026-10-30"
    assert payload["currency"] == "RWF"
    assert payload["lines"][0] == {"account_code": "1100", "debit": 458_400, "credit": 0}


def test_reversal_swaps_every_line_under_a_new_reference() -> None:
    original = journal_entry_for(posted())

    reversal = reversal_of(original, "REV-RPY-1", date(2026, 11, 2))

    assert reversal.external_reference == "REV-RPY-1"
    assert reversal.transaction_type == "REPAYMENT_REVERSAL"
    assert lines(reversal.lines) == [
        ("1100", 0, 458_400),
        ("1200", 383_400, 0),
        ("4000", 75_000, 0),
    ]


def test_breakdown_from_a_real_fineract_repayment() -> None:
    fineract_transaction: dict[str, Any] = {
        "id": 15,
        "amount": 458400.000000,
        "principalPortion": 383400.000000,
        "interestPortion": 75000.000000,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
    }

    assert breakdown_from_fineract(fineract_transaction) == Breakdown(
        principal=383_400, interest=75_000
    )


def test_missing_portions_count_as_zero() -> None:
    assert breakdown_from_fineract({"principalPortion": None}) == Breakdown()


def test_fractional_amounts_are_rejected_for_rwf() -> None:
    with pytest.raises(MappingError, match="decimals"):
        amount_from_fineract("100.50")
