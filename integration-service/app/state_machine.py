import structlog

from app.models import LoanTransaction, TransactionStatus

log = structlog.get_logger()

ALLOWED_TRANSITIONS: dict[TransactionStatus, frozenset[TransactionStatus]] = {
    TransactionStatus.FINERACT_PENDING: frozenset(
        {TransactionStatus.FINERACT_DONE, TransactionStatus.FAILED}
    ),
    TransactionStatus.FINERACT_DONE: frozenset({TransactionStatus.ERP_PENDING}),
    TransactionStatus.ERP_PENDING: frozenset(
        {TransactionStatus.COMPLETED, TransactionStatus.NEEDS_RECONCILIATION}
    ),
    TransactionStatus.NEEDS_RECONCILIATION: frozenset({TransactionStatus.COMPLETED}),
    TransactionStatus.COMPLETED: frozenset(),
    TransactionStatus.FAILED: frozenset(),
}


class IllegalTransitionError(Exception):
    pass


def can_transition(current: TransactionStatus, target: TransactionStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def transition(txn: LoanTransaction, target: TransactionStatus) -> None:
    current = TransactionStatus(txn.status)
    if not can_transition(current, target):
        raise IllegalTransitionError(
            f"{txn.external_reference}: {current} -> {target} is not allowed"
        )
    txn.status = target
    log.info(
        "transaction_status_changed",
        from_status=str(current),
        **transaction_fields(txn),
    )


def transaction_fields(txn: LoanTransaction) -> dict[str, object]:
    loan = txn.loan
    return {
        "external_reference": txn.external_reference,
        "transaction_type": str(txn.type) if txn.type else None,
        "loan_reference": loan.loan_reference if loan else None,
        "client_uid": loan.client.client_uid if loan else None,
        "fineract_transaction_id": txn.fineract_transaction_id,
        "erp_transaction_id": txn.erp_transaction_id,
        "status": str(txn.status),
    }
