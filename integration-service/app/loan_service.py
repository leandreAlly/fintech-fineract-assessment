from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounting import (
    MappingError,
    PostedTransaction,
    breakdown_from_fineract,
    journal_entry_for,
)
from app.client_service import fineract_date
from app.gateways.erp import ErpRejectedError, ErpUnavailableError
from app.gateways.fineract import FineractRejectedError
from app.idempotency import ApiResult, Finished, IdempotencyStore, error_body
from app.models import Client, Loan, LoanTransaction, TransactionStatus, TransactionType
from app.state_machine import transition

CREATE_LOAN = "CREATE_LOAN"
APPROVE_LOAN = "APPROVE_LOAN"
DISBURSE_LOAN = "DISBURSE_LOAN"
REPAY_LOAN = "REPAY_LOAN"

Reference = Field(min_length=1, max_length=100)


class LoanApplicationRequest(BaseModel):
    external_reference: str = Reference
    client_uid: str = Field(pattern=r"^SME-[0-9]{6}$")
    principal: int = Field(gt=0)
    submitted_on: date | None = None


class ApprovalRequest(BaseModel):
    external_reference: str = Reference
    approved_on: date | None = None


class DisbursementRequest(BaseModel):
    external_reference: str = Reference
    disbursed_on: date | None = None


class RepaymentRequest(BaseModel):
    external_reference: str = Reference
    amount: int = Field(gt=0)
    transaction_date: date | None = None


class FineractLoans(Protocol):
    def loan_product_id_by_external_id(self, external_id: str) -> int | None: ...

    def create_loan(self, payload: dict[str, Any]) -> int: ...

    def loan_by_external_id(self, external_id: str) -> dict[str, Any] | None: ...

    def loan(self, loan_id: int) -> dict[str, Any]: ...

    def approve_loan(self, loan_id: int, payload: dict[str, Any]) -> None: ...

    def disburse_loan(self, loan_id: int, payload: dict[str, Any]) -> None: ...

    def repay_loan(self, loan_id: int, payload: dict[str, Any]) -> None: ...

    def loan_transaction_by_external_id(
        self, loan_id: int, external_id: str
    ) -> dict[str, Any] | None: ...


class ErpJournal(Protocol):
    def post_journal_entry(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class LoanProductNotConfiguredError(Exception):
    pass


def money(value: Any) -> int:
    return int(Decimal(str(value or 0)))


def loan_status(fineract_loan: dict[str, Any]) -> str:
    code = str(fineract_loan.get("status", {}).get("code", ""))
    return code.removeprefix("loanStatusType.").replace(".", "_").upper()


APPROVED_STATES = {"APPROVED", "ACTIVE", "CLOSED_OBLIGATIONS_MET", "OVERPAID"}


def fineract_rejected(error: FineractRejectedError) -> ApiResult:
    return ApiResult(422, error_body("FINERACT_REJECTED", error.message))


def loan_not_found(loan_reference: str) -> ApiResult:
    return ApiResult(404, error_body("LOAN_NOT_FOUND", f"no loan {loan_reference}"))


def transaction_body(txn: LoanTransaction) -> dict[str, Any]:
    return {
        "external_reference": txn.external_reference,
        "loan_reference": txn.loan.loan_reference,
        "type": txn.type,
        "status": txn.status,
        "amount": txn.amount,
        "transaction_date": txn.transaction_date.isoformat(),
        "principal_portion": txn.principal_portion,
        "interest_portion": txn.interest_portion,
        "fee_portion": txn.fee_portion,
        "penalty_portion": txn.penalty_portion,
        "overpayment_portion": txn.overpayment_portion,
        "last_error": txn.last_error,
    }


def transaction_result(txn: LoanTransaction) -> ApiResult:
    return ApiResult(
        201 if txn.status == TransactionStatus.COMPLETED else 202, transaction_body(txn)
    )


class LoanService:
    def __init__(
        self,
        session: Session,
        fineract: FineractLoans,
        erp: ErpJournal,
        *,
        product_external_id: str,
        payment_type_id: int,
        currency: str = "RWF",
        today: Callable[[], date] = date.today,
    ) -> None:
        self.session = session
        self.fineract = fineract
        self.erp = erp
        self.currency = currency
        self.product_external_id = product_external_id
        self.payment_type_id = payment_type_id
        self.today = today
        self.idempotency = IdempotencyStore(session)

    def apply(self, request: LoanApplicationRequest) -> ApiResult:
        return self._idempotent(
            CREATE_LOAN, request.external_reference, request, lambda: self._apply(request)
        )

    def approve(self, loan_reference: str, request: ApprovalRequest) -> ApiResult:
        return self._idempotent(
            APPROVE_LOAN,
            request.external_reference,
            request,
            lambda: self._approve(loan_reference, request),
            loan_reference,
        )

    def disburse(self, loan_reference: str, request: DisbursementRequest) -> ApiResult:
        return self._idempotent(
            DISBURSE_LOAN,
            request.external_reference,
            request,
            lambda: self._disburse(loan_reference, request),
            loan_reference,
        )

    def repay(self, loan_reference: str, request: RepaymentRequest) -> ApiResult:
        return self._idempotent(
            REPAY_LOAN,
            request.external_reference,
            request,
            lambda: self._repay(loan_reference, request),
            loan_reference,
        )

    def get(self, loan_reference: str) -> ApiResult:
        loan = self.find(loan_reference)
        if loan is None or loan.fineract_loan_id is None:
            return loan_not_found(loan_reference)
        return ApiResult(200, self._loan_body(loan, self.fineract.loan(loan.fineract_loan_id)))

    def get_transaction(self, external_reference: str) -> ApiResult:
        txn = self.session.scalar(
            select(LoanTransaction).where(LoanTransaction.external_reference == external_reference)
        )
        if txn is None:
            return ApiResult(
                404, error_body("TRANSACTION_NOT_FOUND", f"no transaction {external_reference}")
            )
        return ApiResult(200, transaction_body(txn))

    def find(self, loan_reference: str) -> Loan | None:
        return self.session.scalar(select(Loan).where(Loan.loan_reference == loan_reference))

    def _idempotent(
        self,
        operation: str,
        external_reference: str,
        request: BaseModel,
        run: Callable[[], ApiResult],
        loan_reference: str | None = None,
    ) -> ApiResult:
        payload = request.model_dump(mode="json") | {"loan_reference": loan_reference}
        outcome = self.idempotency.begin(operation, external_reference, payload)
        if isinstance(outcome, Finished):
            return outcome.result
        try:
            result = run()
        except Exception:
            self.idempotency.release(outcome.record)
            raise
        if result.status_code == 202:
            self.idempotency.release(outcome.record)
        else:
            self.idempotency.complete(outcome.record, result)
        return result

    def _apply(self, request: LoanApplicationRequest) -> ApiResult:
        client = self.session.scalar(select(Client).where(Client.client_uid == request.client_uid))
        if client is None:
            return ApiResult(422, error_body("UNKNOWN_CLIENT", f"no client {request.client_uid}"))
        loan = self._reserve_loan(request, client)
        if loan.fineract_loan_id is None:
            existing = self.fineract.loan_by_external_id(str(loan.loan_reference))
            if existing is not None:
                loan.fineract_loan_id = int(existing["id"])
            else:
                try:
                    loan.fineract_loan_id = self.fineract.create_loan(
                        self._application_payload(request, loan, client)
                    )
                except FineractRejectedError as error:
                    return fineract_rejected(error)
            self.session.commit()
        return ApiResult(201, self._loan_body(loan, self.fineract.loan(loan.fineract_loan_id)))

    def _reserve_loan(self, request: LoanApplicationRequest, client: Client) -> Loan:
        loan = self.session.scalar(
            select(Loan).where(Loan.external_reference == request.external_reference)
        )
        if loan is not None:
            return loan
        loan = Loan(
            external_reference=request.external_reference,
            client_id=client.id,
            principal=request.principal,
        )
        self.session.add(loan)
        self.session.flush()
        loan.loan_reference = f"LN-{self.today().year}-{loan.id:06d}"
        self.session.commit()
        return loan

    def _approve(self, loan_reference: str, request: ApprovalRequest) -> ApiResult:
        loan = self._locked_loan(loan_reference)
        if loan is None or loan.fineract_loan_id is None:
            self.session.rollback()
            return loan_not_found(loan_reference)
        if loan.approval_reference not in (None, request.external_reference):
            self.session.rollback()
            return ApiResult(
                409, error_body("LOAN_ALREADY_APPROVED", f"{loan_reference} is already approved")
            )
        status = loan_status(self.fineract.loan(loan.fineract_loan_id))
        if status == "SUBMITTED_AND_PENDING_APPROVAL":
            approved_on = (request.approved_on or self.today()).isoformat()
            try:
                self.fineract.approve_loan(
                    loan.fineract_loan_id,
                    {
                        "approvedOnDate": approved_on,
                        "approvedLoanAmount": loan.principal,
                        "expectedDisbursementDate": approved_on,
                        "dateFormat": "yyyy-MM-dd",
                        "locale": "en",
                    },
                )
            except FineractRejectedError as error:
                self.session.rollback()
                return fineract_rejected(error)
        elif status not in APPROVED_STATES:
            self.session.rollback()
            return ApiResult(
                409, error_body("LOAN_STATE_CONFLICT", f"{loan_reference} is {status}")
            )
        loan.approval_reference = request.external_reference
        self.session.commit()
        return ApiResult(200, self._loan_body(loan, self.fineract.loan(loan.fineract_loan_id)))

    def _disburse(self, loan_reference: str, request: DisbursementRequest) -> ApiResult:
        loan = self.find(loan_reference)
        if loan is None or loan.fineract_loan_id is None:
            return loan_not_found(loan_reference)
        fineract_loan_id = loan.fineract_loan_id
        txn = self._record_transaction(
            loan,
            TransactionType.DISBURSEMENT,
            request.external_reference,
            loan.principal,
            request.disbursed_on,
        )
        if txn is None:
            return ApiResult(
                409,
                error_body(
                    "LOAN_ALREADY_DISBURSED", f"{loan_reference} already has a disbursement"
                ),
            )
        return self._post_transaction(
            txn,
            fineract_loan_id,
            lambda: self.fineract.disburse_loan(
                fineract_loan_id,
                {
                    "actualDisbursementDate": txn.transaction_date.isoformat(),
                    "transactionAmount": txn.amount,
                    "externalId": txn.external_reference,
                    "paymentTypeId": self.payment_type_id,
                    "dateFormat": "yyyy-MM-dd",
                    "locale": "en",
                },
            ),
        )

    def _repay(self, loan_reference: str, request: RepaymentRequest) -> ApiResult:
        loan = self.find(loan_reference)
        if loan is None or loan.fineract_loan_id is None:
            return loan_not_found(loan_reference)
        fineract_loan_id = loan.fineract_loan_id
        txn = self._record_transaction(
            loan,
            TransactionType.REPAYMENT,
            request.external_reference,
            request.amount,
            request.transaction_date,
        )
        if txn is None:
            return ApiResult(
                409,
                error_body(
                    "TRANSACTION_REFERENCE_CONFLICT",
                    f"{request.external_reference} is already used by another transaction",
                ),
            )
        return self._post_transaction(
            txn,
            fineract_loan_id,
            lambda: self.fineract.repay_loan(
                fineract_loan_id,
                {
                    "transactionDate": txn.transaction_date.isoformat(),
                    "transactionAmount": txn.amount,
                    "externalId": txn.external_reference,
                    "paymentTypeId": self.payment_type_id,
                    "dateFormat": "yyyy-MM-dd",
                    "locale": "en",
                },
            ),
        )

    def _record_transaction(
        self,
        loan: Loan,
        type_: TransactionType,
        external_reference: str,
        amount: int,
        transaction_date: date | None,
    ) -> LoanTransaction | None:
        existing = self.session.scalar(
            select(LoanTransaction).where(LoanTransaction.external_reference == external_reference)
        )
        if existing is not None:
            if existing.loan_id != loan.id or existing.type != type_:
                return None
            return existing
        txn = LoanTransaction(
            external_reference=external_reference,
            loan_id=loan.id,
            type=type_,
            amount=amount,
            transaction_date=transaction_date or self.today(),
            status=TransactionStatus.FINERACT_PENDING,
        )
        self.session.add(txn)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            return None
        return txn

    def _post_transaction(
        self, txn: LoanTransaction, loan_id: int, send: Callable[[], None]
    ) -> ApiResult:
        if txn.status == TransactionStatus.FINERACT_PENDING:
            self._post_to_fineract(txn, loan_id, send)
        if txn.status == TransactionStatus.FAILED:
            return ApiResult(422, error_body("FINERACT_REJECTED", txn.last_error or "rejected"))
        if txn.status in (TransactionStatus.FINERACT_DONE, TransactionStatus.ERP_PENDING):
            self._post_to_erp(txn)
        return transaction_result(txn)

    def _post_to_fineract(
        self, txn: LoanTransaction, loan_id: int, send: Callable[[], None]
    ) -> None:
        posted = self.fineract.loan_transaction_by_external_id(loan_id, txn.external_reference)
        if posted is None:
            try:
                send()
            except FineractRejectedError as error:
                transition(txn, TransactionStatus.FAILED)
                txn.last_error = error.message
                self.session.commit()
                return
            posted = self.fineract.loan_transaction_by_external_id(loan_id, txn.external_reference)
            if posted is None:
                raise RuntimeError(f"Fineract has no transaction {txn.external_reference}")
        breakdown = breakdown_from_fineract(posted)
        txn.fineract_transaction_id = int(posted["id"])
        txn.amount = money(posted.get("amount"))
        txn.principal_portion = breakdown.principal
        txn.interest_portion = breakdown.interest
        txn.fee_portion = breakdown.fees
        txn.penalty_portion = breakdown.penalties
        txn.overpayment_portion = breakdown.overpayment
        txn.last_error = None
        transition(txn, TransactionStatus.FINERACT_DONE)
        self.session.commit()

    def _post_to_erp(self, txn: LoanTransaction) -> None:
        if txn.status == TransactionStatus.FINERACT_DONE:
            transition(txn, TransactionStatus.ERP_PENDING)
            self.session.commit()
        try:
            entry = journal_entry_for(self._posted_transaction(txn))
        except MappingError as error:
            self._needs_reconciliation(txn, str(error))
            return
        txn.erp_attempts += 1
        try:
            self.erp.post_journal_entry(entry.as_payload())
        except ErpUnavailableError as error:
            txn.last_error = f"ERP unavailable: {error}"
            self.session.commit()
            return
        except ErpRejectedError as error:
            self._needs_reconciliation(txn, error.message)
            return
        txn.last_error = None
        transition(txn, TransactionStatus.COMPLETED)
        self.session.commit()

    def _needs_reconciliation(self, txn: LoanTransaction, reason: str) -> None:
        txn.last_error = reason
        transition(txn, TransactionStatus.NEEDS_RECONCILIATION)
        self.session.commit()

    def _posted_transaction(self, txn: LoanTransaction) -> PostedTransaction:
        return PostedTransaction(
            external_reference=txn.external_reference,
            loan_reference=str(txn.loan.loan_reference),
            type=TransactionType(txn.type),
            transaction_date=txn.transaction_date,
            currency=self.currency,
            amount=txn.amount,
            breakdown=breakdown_from_fineract(
                {
                    "principalPortion": txn.principal_portion,
                    "interestPortion": txn.interest_portion,
                    "feeChargesPortion": txn.fee_portion,
                    "penaltyChargesPortion": txn.penalty_portion,
                    "overpaymentPortion": txn.overpayment_portion,
                }
            ),
        )

    def _locked_loan(self, loan_reference: str) -> Loan | None:
        return self.session.scalar(
            select(Loan)
            .where(Loan.loan_reference == loan_reference)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _application_payload(
        self, request: LoanApplicationRequest, loan: Loan, client: Client
    ) -> dict[str, Any]:
        product_id = self.fineract.loan_product_id_by_external_id(self.product_external_id)
        if product_id is None:
            raise LoanProductNotConfiguredError(
                f"Fineract loan product {self.product_external_id} not found"
            )
        submitted_on = (request.submitted_on or self.today()).isoformat()
        return {
            "clientId": client.fineract_client_id,
            "productId": product_id,
            "externalId": loan.loan_reference,
            "principal": request.principal,
            "loanTermFrequency": 12,
            "loanTermFrequencyType": 2,
            "numberOfRepayments": 12,
            "repaymentEvery": 1,
            "repaymentFrequencyType": 2,
            "interestRatePerPeriod": 18,
            "amortizationType": 1,
            "interestType": 0,
            "interestCalculationPeriodType": 1,
            "transactionProcessingStrategyCode": "mifos-standard-strategy",
            "expectedDisbursementDate": submitted_on,
            "submittedOnDate": submitted_on,
            "loanType": "individual",
            "dateFormat": "yyyy-MM-dd",
            "locale": "en",
        }

    def _loan_body(self, loan: Loan, fineract_loan: dict[str, Any]) -> dict[str, Any]:
        summary = fineract_loan.get("summary") or {}
        transactions = self.session.scalars(
            select(LoanTransaction)
            .where(LoanTransaction.loan_id == loan.id)
            .order_by(LoanTransaction.id)
        )
        return {
            "loan_reference": loan.loan_reference,
            "external_reference": loan.external_reference,
            "client_uid": loan.client.client_uid,
            "principal": loan.principal,
            "status": loan_status(fineract_loan),
            "submitted_on": fineract_date(fineract_loan.get("timeline", {}).get("submittedOnDate")),
            "approved_on": fineract_date(fineract_loan.get("timeline", {}).get("approvedOnDate")),
            "disbursed_on": fineract_date(
                fineract_loan.get("timeline", {}).get("actualDisbursementDate")
            ),
            "closed_on": fineract_date(fineract_loan.get("timeline", {}).get("closedOnDate")),
            "balances": {
                "principal_disbursed": money(summary.get("principalDisbursed")),
                "principal_outstanding": money(summary.get("principalOutstanding")),
                "interest_outstanding": money(summary.get("interestOutstanding")),
                "total_outstanding": money(summary.get("totalOutstanding")),
                "total_repaid": money(summary.get("totalRepayment")),
                "total_expected_repayment": money(summary.get("totalExpectedRepayment")),
            },
            "transactions": [
                {
                    k: v
                    for k, v in transaction_body(t).items()
                    if k not in ("loan_reference", "last_error")
                }
                for t in transactions
            ],
        }
