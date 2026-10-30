from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.models import TransactionType


class LedgerAccount(StrEnum):
    BANK = "1100"
    LOAN_RECEIVABLE = "1200"
    OVERPAYMENT_LIABILITY = "2100"
    INTEREST_INCOME = "4000"
    FEE_INCOME = "4100"
    PENALTY_INCOME = "4200"


class MappingError(Exception):
    pass


@dataclass(frozen=True)
class Breakdown:
    principal: int = 0
    interest: int = 0
    fees: int = 0
    penalties: int = 0
    overpayment: int = 0

    def total(self) -> int:
        return self.principal + self.interest + self.fees + self.penalties + self.overpayment


@dataclass(frozen=True)
class PostedTransaction:
    external_reference: str
    loan_reference: str
    type: TransactionType
    transaction_date: date
    currency: str
    amount: int
    breakdown: Breakdown


@dataclass(frozen=True)
class JournalLine:
    account_code: str
    debit: int = 0
    credit: int = 0


@dataclass(frozen=True)
class JournalEntry:
    external_reference: str
    entry_date: date
    currency: str
    description: str
    loan_reference: str
    transaction_type: str
    lines: tuple[JournalLine, ...]

    def total_debits(self) -> int:
        return sum(line.debit for line in self.lines)

    def total_credits(self) -> int:
        return sum(line.credit for line in self.lines)

    def as_payload(self) -> dict[str, Any]:
        return {
            "external_reference": self.external_reference,
            "entry_date": self.entry_date.isoformat(),
            "currency": self.currency,
            "description": self.description,
            "loan_reference": self.loan_reference,
            "transaction_type": self.transaction_type,
            "lines": [
                {"account_code": line.account_code, "debit": line.debit, "credit": line.credit}
                for line in self.lines
            ],
        }


def amount_from_fineract(value: Any) -> int:
    amount = Decimal(str(value or 0))
    if amount != amount.to_integral_value():
        raise MappingError(f"amount {value} has decimals, RWF has none")
    return int(amount)


def breakdown_from_fineract(transaction: dict[str, Any]) -> Breakdown:
    return Breakdown(
        principal=amount_from_fineract(transaction.get("principalPortion")),
        interest=amount_from_fineract(transaction.get("interestPortion")),
        fees=amount_from_fineract(transaction.get("feeChargesPortion")),
        penalties=amount_from_fineract(transaction.get("penaltyChargesPortion")),
        overpayment=amount_from_fineract(transaction.get("overpaymentPortion")),
    )


def journal_entry_for(txn: PostedTransaction) -> JournalEntry:
    if txn.amount <= 0:
        raise MappingError(f"{txn.external_reference}: amount must be positive, got {txn.amount}")
    if txn.type == TransactionType.DISBURSEMENT:
        lines = disbursement_lines(txn.amount)
    elif txn.type == TransactionType.REPAYMENT:
        lines = repayment_lines(txn)
    else:
        raise MappingError(f"{txn.external_reference}: no mapping for {txn.type}")
    return JournalEntry(
        external_reference=txn.external_reference,
        entry_date=txn.transaction_date,
        currency=txn.currency,
        description=f"{txn.type.title()} {txn.external_reference} on {txn.loan_reference}",
        loan_reference=txn.loan_reference,
        transaction_type=txn.type,
        lines=lines,
    )


def disbursement_lines(amount: int) -> tuple[JournalLine, ...]:
    return (
        JournalLine(LedgerAccount.LOAN_RECEIVABLE, debit=amount),
        JournalLine(LedgerAccount.BANK, credit=amount),
    )


def repayment_lines(txn: PostedTransaction) -> tuple[JournalLine, ...]:
    parts = txn.breakdown
    credits = [
        (LedgerAccount.LOAN_RECEIVABLE, parts.principal),
        (LedgerAccount.INTEREST_INCOME, parts.interest),
        (LedgerAccount.FEE_INCOME, parts.fees),
        (LedgerAccount.PENALTY_INCOME, parts.penalties),
        (LedgerAccount.OVERPAYMENT_LIABILITY, parts.overpayment),
    ]
    if any(value < 0 for _, value in credits):
        raise MappingError(f"{txn.external_reference}: negative portion in {parts}")
    if parts.total() != txn.amount:
        raise MappingError(
            f"{txn.external_reference}: portions add up to {parts.total()}, "
            f"Fineract amount is {txn.amount}"
        )
    return (
        JournalLine(LedgerAccount.BANK, debit=txn.amount),
        *(JournalLine(account, credit=value) for account, value in credits if value > 0),
    )


def reversal_of(entry: JournalEntry, reversal_reference: str, reversed_on: date) -> JournalEntry:
    return JournalEntry(
        external_reference=reversal_reference,
        entry_date=reversed_on,
        currency=entry.currency,
        description=f"Reversal of {entry.external_reference}",
        loan_reference=entry.loan_reference,
        transaction_type=f"{entry.transaction_type}_REVERSAL",
        lines=tuple(
            JournalLine(line.account_code, debit=line.credit, credit=line.debit)
            for line in entry.lines
        ),
    )
