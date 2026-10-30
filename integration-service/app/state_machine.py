from app.models import LoanTransaction, TransactionStatus

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
