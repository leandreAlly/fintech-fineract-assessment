from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Protocol, Self

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting import (
    Breakdown,
    JournalEntry,
    JournalLine,
    MappingError,
    PostedTransaction,
    amount_from_fineract,
    breakdown_from_fineract,
    journal_entry_for,
    repayment_lines,
    reversal_of,
)
from app.client_service import fineract_date
from app.gateways.erp import ErpRejectedError
from app.idempotency import ApiResult, error_body
from app.models import Loan, LoanTransaction, TransactionStatus, TransactionType
from app.retry import utc_now
from app.state_machine import transition

FINERACT_TYPES = {1: TransactionType.DISBURSEMENT, 2: TransactionType.REPAYMENT}
REVERSAL_PREFIX = "REV-"
LOOKUP_CHUNK = 100


class ReconciliationStatus(StrEnum):
    MATCHED = "MATCHED"
    MISSING_IN_ERP = "MISSING_IN_ERP"
    MISSING_IN_FINERACT = "MISSING_IN_FINERACT"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    SPLIT_MISMATCH = "SPLIT_MISMATCH"


@dataclass(frozen=True)
class FineractSide:
    external_reference: str
    loan_reference: str
    type: str
    amount: int
    date: date
    reversed: bool
    breakdown: Breakdown | None = None

    def net(self) -> int:
        return 0 if self.reversed else self.amount

    def expected_credits(self) -> dict[str, int] | None:
        if self.type != TransactionType.REPAYMENT or self.breakdown is None:
            return None
        lines = repayment_lines(
            PostedTransaction(
                self.external_reference,
                self.loan_reference,
                TransactionType.REPAYMENT,
                self.date,
                "",
                self.amount,
                self.breakdown,
            )
        )
        return {line.account_code: line.credit for line in lines if line.credit}


@dataclass(frozen=True)
class ErpSide:
    external_reference: str
    loan_reference: str | None
    type: str | None
    amount: int
    date: date | None
    entry_id: str | None
    reversal_entry_id: str | None = None
    reversal_amount: int = 0
    credits: dict[str, int] = field(default_factory=dict)

    def net(self) -> int:
        return self.amount - self.reversal_amount


@dataclass(frozen=True)
class ReconciliationItem:
    external_reference: str
    loan_reference: str | None
    status: ReconciliationStatus
    fineract: FineractSide | None
    erp: ErpSide | None
    difference: int
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_reference": self.external_reference,
            "loan_reference": self.loan_reference,
            "status": self.status,
            "difference": self.difference,
            "note": self.note,
            "fineract": side_dict(self.fineract),
            "erp": side_dict(self.erp),
        }


def side_dict(side: FineractSide | ErpSide | None) -> dict[str, Any] | None:
    if side is None:
        return None
    body = asdict(side) | {"net_amount": side.net()}
    body.pop("external_reference")
    body.pop("loan_reference")
    body.pop("breakdown", None)
    body["date"] = side.date.isoformat() if side.date else None
    return body


def compare(fineract: FineractSide | None, erp: ErpSide | None) -> ReconciliationItem:
    side = fineract or erp
    if side is None:
        raise ValueError("nothing to compare")
    reference = side.external_reference
    loan = side.loan_reference
    fineract_net = fineract.net() if fineract else 0
    erp_net = erp.net() if erp else 0

    def item(status: ReconciliationStatus, note: str | None = None) -> ReconciliationItem:
        return ReconciliationItem(
            reference, loan, status, fineract, erp, fineract_net - erp_net, note
        )

    if fineract is None:
        return item(ReconciliationStatus.MISSING_IN_FINERACT)
    if erp is None:
        if fineract.reversed:
            return item(
                ReconciliationStatus.MATCHED, "reversed in Fineract, never posted to the ERP"
            )
        return item(ReconciliationStatus.MISSING_IN_ERP)
    if erp.type is not None and erp.type != fineract.type:
        return item(ReconciliationStatus.TYPE_MISMATCH, f"Fineract {fineract.type}, ERP {erp.type}")
    if fineract_net != erp_net:
        if fineract.reversed and erp.reversal_amount == 0:
            return item(
                ReconciliationStatus.AMOUNT_MISMATCH,
                "reversed in Fineract, the ERP entry has no reversal",
            )
        return item(ReconciliationStatus.AMOUNT_MISMATCH)
    if fineract.reversed:
        return item(ReconciliationStatus.MATCHED, "reversed in both systems")
    expected = fineract.expected_credits()
    if expected is not None and erp.credits and expected != erp.credits:
        return item(ReconciliationStatus.SPLIT_MISMATCH, split_note(expected, erp.credits))
    return item(ReconciliationStatus.MATCHED)


def split_note(expected: dict[str, int], actual: dict[str, int]) -> str:
    accounts = sorted(expected.keys() | actual.keys())
    return "; ".join(
        f"{code}: Fineract {expected.get(code, 0)}, ERP {actual.get(code, 0)}"
        for code in accounts
        if expected.get(code, 0) != actual.get(code, 0)
    )


def reconcile(fineract: Iterable[FineractSide], erp: Iterable[ErpSide]) -> list[ReconciliationItem]:
    fineract_by_ref = {side.external_reference: side for side in fineract}
    erp_by_ref = {side.external_reference: side for side in erp}
    references = sorted(fineract_by_ref.keys() | erp_by_ref.keys())
    return [compare(fineract_by_ref.get(ref), erp_by_ref.get(ref)) for ref in references]


def fineract_side(transaction: dict[str, Any], loan_reference: str) -> FineractSide | None:
    type_ = FINERACT_TYPES.get(int((transaction.get("type") or {}).get("id", 0)))
    transaction_date = fineract_date(transaction.get("date"))
    if type_ is None or transaction_date is None:
        return None
    return FineractSide(
        external_reference=transaction.get("externalId") or f"FINERACT-{transaction['id']}",
        loan_reference=loan_reference,
        type=type_,
        amount=amount_from_fineract(transaction.get("amount")),
        date=date.fromisoformat(transaction_date),
        reversed=bool(transaction.get("manuallyReversed")),
        breakdown=breakdown_from_fineract(transaction)
        if type_ == TransactionType.REPAYMENT
        else None,
    )


def erp_sides(entries: Iterable[dict[str, Any]]) -> dict[str, ErpSide]:
    originals: dict[str, dict[str, Any]] = {}
    reversals: dict[str, dict[str, Any]] = {}
    for entry in entries:
        reference = str(entry["external_reference"])
        if str(entry.get("transaction_type") or "").endswith("_REVERSAL"):
            reversals[reference.removeprefix(REVERSAL_PREFIX)] = entry
        else:
            originals[reference] = entry
    sides = {}
    for reference in originals.keys() | reversals.keys():
        original = originals.get(reference)
        reversal = reversals.get(reference)
        base = original or reversal
        assert base is not None
        sides[reference] = ErpSide(
            external_reference=reference,
            loan_reference=base.get("loan_reference"),
            type=original.get("transaction_type") if original else None,
            amount=amount_from_fineract(original["total_amount"]) if original else 0,
            date=date.fromisoformat(original["entry_date"]) if original else None,
            entry_id=original.get("entry_id") if original else None,
            reversal_entry_id=reversal.get("entry_id") if reversal else None,
            reversal_amount=amount_from_fineract(reversal["total_amount"]) if reversal else 0,
            credits=credits_of(original) if original else {},
        )
    return sides


def credits_of(entry: dict[str, Any]) -> dict[str, int]:
    credits: dict[str, int] = {}
    for line in entry.get("lines") or []:
        amount = amount_from_fineract(line.get("credit"))
        if amount:
            credits[line["account_code"]] = credits.get(line["account_code"], 0) + amount
    return credits


def journal_entry_from_erp(entry: dict[str, Any]) -> JournalEntry:
    return JournalEntry(
        external_reference=entry["external_reference"],
        entry_date=date.fromisoformat(entry["entry_date"]),
        currency=entry["currency"],
        description=entry.get("description") or "",
        loan_reference=entry.get("loan_reference") or "",
        transaction_type=entry.get("transaction_type") or "",
        lines=tuple(
            JournalLine(
                line["account_code"],
                debit=amount_from_fineract(line["debit"]),
                credit=amount_from_fineract(line["credit"]),
            )
            for line in entry["lines"]
        ),
    )


class ReconciliationRequest(BaseModel):
    from_date: date | None = None
    to_date: date | None = None
    loan_references: list[str] | None = Field(default=None, max_length=500)
    external_references: list[str] | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def ordered_dates(self) -> Self:
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must not be after to_date")
        return self

    def covers(self, reference: str, on: date | None) -> bool:
        if self.external_references is not None and reference not in self.external_references:
            return False
        if on is None:
            return True
        if self.from_date and on < self.from_date:
            return False
        return not (self.to_date and on > self.to_date)


class RepairRequest(BaseModel):
    external_reference: str = Field(min_length=1, max_length=100)
    loan_reference: str = Field(min_length=1, max_length=20)


class FineractLedger(Protocol):
    def loan_transactions(self, loan_id: int) -> list[dict[str, Any]]: ...

    def loan_transaction_by_external_id(
        self, loan_id: int, external_id: str
    ) -> dict[str, Any] | None: ...


class ErpLedger(Protocol):
    def journal_entries(self, params: dict[str, Any]) -> list[dict[str, Any]]: ...

    def post_journal_entry(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class ReconciliationService:
    def __init__(
        self, session: Session, fineract: FineractLedger, erp: ErpLedger, currency: str = "RWF"
    ) -> None:
        self.session = session
        self.fineract = fineract
        self.erp = erp
        self.currency = currency

    def run(self, request: ReconciliationRequest) -> dict[str, Any]:
        loans = self._loans(request.loan_references)
        fineract = self._fineract_sides(request, loans)
        erp = {
            ref: side
            for ref, side in self._erp_sides(request).items()
            if request.loan_references is None or side.loan_reference in loans
        }
        erp |= self._erp_lookup([ref for ref in fineract if ref not in erp])
        erp |= self._reversals_for(fineract, erp)
        for ref in erp.keys() - fineract.keys():
            found = self._fineract_lookup(ref, erp[ref].loan_reference, loans)
            if found is not None:
                fineract[ref] = found
        items = reconcile(fineract.values(), erp.values())
        summary = {status: 0 for status in ReconciliationStatus}
        for item in items:
            summary[item.status] += 1
        return {
            "generated_at": utc_now().isoformat(),
            "scope": request.model_dump(mode="json"),
            "summary": summary,
            "items": [item.as_dict() for item in items],
        }

    def check(self, loan_reference: str, external_reference: str) -> ReconciliationItem | None:
        loans = self._loans([loan_reference])
        if loan_reference not in loans:
            return None
        fineract = self._fineract_lookup(external_reference, loan_reference, loans)
        erp = self._erp_lookup([external_reference])
        if fineract is not None:
            erp |= self._reversals_for({external_reference: fineract}, erp)
        if fineract is None and external_reference not in erp:
            return None
        return compare(fineract, erp.get(external_reference))

    def repair(self, request: RepairRequest) -> ApiResult:
        item = self.check(request.loan_reference, request.external_reference)
        if item is None:
            return ApiResult(
                404,
                error_body(
                    "NOTHING_TO_REPAIR",
                    f"{request.external_reference} is in neither system for "
                    f"{request.loan_reference}",
                ),
            )
        if item.status == ReconciliationStatus.MATCHED:
            return ApiResult(200, {"action": "none", "item": item.as_dict()})
        try:
            action = self._repair_action(item)
        except (MappingError, ErpRejectedError) as error:
            return ApiResult(422, error_body("REPAIR_FAILED", str(error)))
        if action is None:
            return ApiResult(
                409,
                error_body(
                    "REPAIR_NOT_SUPPORTED",
                    f"{item.status} needs manual review: no automatic repair is safe",
                ),
            )
        self._complete_local(request.external_reference)
        repaired = self.check(request.loan_reference, request.external_reference)
        assert repaired is not None
        return ApiResult(200, {"action": action, "item": repaired.as_dict()})

    def _repair_action(self, item: ReconciliationItem) -> str | None:
        fineract, erp = item.fineract, item.erp
        if item.status == ReconciliationStatus.MISSING_IN_ERP and fineract is not None:
            loan_id = self._loans([fineract.loan_reference])[fineract.loan_reference]
            raw = self.fineract.loan_transaction_by_external_id(
                loan_id, fineract.external_reference
            )
            assert raw is not None
            entry = journal_entry_for(
                PostedTransaction(
                    external_reference=fineract.external_reference,
                    loan_reference=fineract.loan_reference,
                    type=TransactionType(fineract.type),
                    transaction_date=fineract.date,
                    currency=self.currency,
                    amount=fineract.amount,
                    breakdown=breakdown_from_fineract(raw),
                )
            )
            self.erp.post_journal_entry(entry.as_payload())
            return "posted_to_erp"
        if (
            item.status == ReconciliationStatus.AMOUNT_MISMATCH
            and fineract is not None
            and erp is not None
            and fineract.reversed
            and erp.reversal_amount == 0
            and erp.amount == fineract.amount
        ):
            original = self.erp.journal_entries({"external_reference": [erp.external_reference]})
            reversal = reversal_of(
                journal_entry_from_erp(original[0]),
                f"{REVERSAL_PREFIX}{erp.external_reference}",
                utc_now().date(),
            )
            self.erp.post_journal_entry(reversal.as_payload())
            return "posted_reversal_to_erp"
        return None

    def _complete_local(self, external_reference: str) -> None:
        txn = self.session.scalar(
            select(LoanTransaction).where(LoanTransaction.external_reference == external_reference)
        )
        if txn is None:
            return
        if txn.status == TransactionStatus.FINERACT_DONE:
            transition(txn, TransactionStatus.ERP_PENDING)
        if txn.status in (TransactionStatus.ERP_PENDING, TransactionStatus.NEEDS_RECONCILIATION):
            transition(txn, TransactionStatus.COMPLETED)
            txn.last_error = None
            txn.next_attempt_at = None
        self.session.commit()

    def _loans(self, loan_references: list[str] | None) -> dict[str, int]:
        query = select(Loan.loan_reference, Loan.fineract_loan_id).where(
            Loan.fineract_loan_id.is_not(None)
        )
        if loan_references is not None:
            query = query.where(Loan.loan_reference.in_(loan_references))
        return {
            str(ref): loan_id for ref, loan_id in self.session.execute(query) if loan_id is not None
        }

    def _fineract_sides(
        self, request: ReconciliationRequest, loans: dict[str, int]
    ) -> dict[str, FineractSide]:
        sides = {}
        for loan_reference, loan_id in loans.items():
            for transaction in self.fineract.loan_transactions(loan_id):
                side = fineract_side(transaction, loan_reference)
                if side is not None and request.covers(side.external_reference, side.date):
                    sides[side.external_reference] = side
        return sides

    def _erp_sides(self, request: ReconciliationRequest) -> dict[str, ErpSide]:
        params: dict[str, Any] = {"limit": 10_000}
        if request.from_date:
            params["from_date"] = request.from_date.isoformat()
        if request.to_date:
            params["to_date"] = request.to_date.isoformat()
        if request.external_references:
            params["external_reference"] = request.external_references
        entries = [
            entry
            for entry in self.erp.journal_entries(params)
            if entry.get("transaction_type") in FINERACT_TYPES.values()
        ]
        return erp_sides(entries)

    def _erp_lookup(self, references: list[str]) -> dict[str, ErpSide]:
        entries: list[dict[str, Any]] = []
        for start in range(0, len(references), LOOKUP_CHUNK):
            chunk = references[start : start + LOOKUP_CHUNK]
            entries += self.erp.journal_entries(
                {"external_reference": chunk + [f"{REVERSAL_PREFIX}{ref}" for ref in chunk]}
            )
        return erp_sides(entries)

    def _reversals_for(
        self, fineract: dict[str, FineractSide], erp: dict[str, ErpSide]
    ) -> dict[str, ErpSide]:
        candidates = [
            ref
            for ref, side in fineract.items()
            if side.reversed and ref in erp and erp[ref].reversal_amount == 0
        ]
        if not candidates:
            return {}
        return self._erp_lookup(candidates)

    def _fineract_lookup(
        self, reference: str, loan_reference: str | None, loans: dict[str, int]
    ) -> FineractSide | None:
        if loan_reference is None:
            return None
        loan_id = loans.get(loan_reference) or self._loans([loan_reference]).get(loan_reference)
        if loan_id is None:
            return None
        transaction = self.fineract.loan_transaction_by_external_id(loan_id, reference)
        return fineract_side(transaction, loan_reference) if transaction else None
