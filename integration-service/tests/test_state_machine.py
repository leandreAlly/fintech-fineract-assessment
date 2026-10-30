import pytest

from app.models import LoanTransaction, TransactionStatus
from app.state_machine import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    can_transition,
    transition,
)

S = TransactionStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.FINERACT_PENDING, S.FINERACT_DONE),
        (S.FINERACT_PENDING, S.FAILED),
        (S.FINERACT_DONE, S.ERP_PENDING),
        (S.ERP_PENDING, S.COMPLETED),
        (S.ERP_PENDING, S.NEEDS_RECONCILIATION),
        (S.NEEDS_RECONCILIATION, S.COMPLETED),
    ],
)
def test_allowed_transitions(current: TransactionStatus, target: TransactionStatus) -> None:
    assert can_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.FINERACT_PENDING, S.COMPLETED),
        (S.FINERACT_PENDING, S.ERP_PENDING),
        (S.FINERACT_DONE, S.FAILED),
        (S.ERP_PENDING, S.FAILED),
        (S.COMPLETED, S.ERP_PENDING),
        (S.FAILED, S.FINERACT_PENDING),
    ],
)
def test_forbidden_transitions(current: TransactionStatus, target: TransactionStatus) -> None:
    assert not can_transition(current, target)


def test_terminal_states_have_no_exit() -> None:
    assert ALLOWED_TRANSITIONS[S.COMPLETED] == frozenset()
    assert ALLOWED_TRANSITIONS[S.FAILED] == frozenset()


def test_every_state_has_rules() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(TransactionStatus)


def test_transition_changes_status() -> None:
    txn = LoanTransaction(external_reference="RPY-1", status=S.ERP_PENDING)

    transition(txn, S.COMPLETED)

    assert txn.status == S.COMPLETED


def test_illegal_transition_raises_and_keeps_status() -> None:
    txn = LoanTransaction(external_reference="RPY-1", status=S.COMPLETED)

    with pytest.raises(IllegalTransitionError, match="COMPLETED -> ERP_PENDING"):
        transition(txn, S.ERP_PENDING)

    assert txn.status == S.COMPLETED
