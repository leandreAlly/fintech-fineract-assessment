from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Protocol

from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import OperationalError

LOAN_LOCK_NAMESPACE = 1


class LoanBusyError(Exception):
    pass


class LoanLocks(Protocol):
    def hold(self, loan_id: int) -> AbstractContextManager[None]: ...


class NoLoanLocks:
    def hold(self, loan_id: int) -> AbstractContextManager[None]:
        return nullcontext()


class PostgresLoanLocks:
    def __init__(self, engine: Engine, timeout_seconds: float) -> None:
        self.engine = engine
        self.timeout_ms = int(timeout_seconds * 1000)

    @contextmanager
    def hold(self, loan_id: int) -> Iterator[None]:
        with self.engine.connect() as connection:
            connection.execute(text(f"SET lock_timeout = {self.timeout_ms}"))
            try:
                connection.execute(select(func.pg_advisory_lock(LOAN_LOCK_NAMESPACE, loan_id)))
            except OperationalError as error:
                raise LoanBusyError(f"loan {loan_id} is busy, retry later") from error
            try:
                yield
            finally:
                connection.execute(select(func.pg_advisory_unlock(LOAN_LOCK_NAMESPACE, loan_id)))
