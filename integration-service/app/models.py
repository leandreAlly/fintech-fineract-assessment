from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SyncStatus(StrEnum):
    SYNC_PENDING = "SYNC_PENDING"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


class GlAccountSync(Base):
    __tablename__ = "gl_account_sync"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    erp_code: Mapped[str] = mapped_column(String(20))
    erp_name: Mapped[str] = mapped_column(String(100))
    erp_type: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[SyncStatus] = mapped_column(String(20), default=SyncStatus.SYNC_PENDING)
    fineract_gl_account_id: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OperationStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("operation", "external_reference"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(String(40))
    external_reference: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[OperationStatus] = mapped_column(String(20))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_uid: Mapped[str] = mapped_column(String(40), unique=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    fineract_client_id: Mapped[int] = mapped_column(Integer, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_reference: Mapped[str | None] = mapped_column(String(20), unique=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    principal: Mapped[int] = mapped_column(BigInteger)
    fineract_loan_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    approval_reference: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    client: Mapped[Client] = relationship()


class TransactionType(StrEnum):
    DISBURSEMENT = "DISBURSEMENT"
    REPAYMENT = "REPAYMENT"


class TransactionStatus(StrEnum):
    FINERACT_PENDING = "FINERACT_PENDING"
    FINERACT_DONE = "FINERACT_DONE"
    ERP_PENDING = "ERP_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


class LoanTransaction(Base):
    __tablename__ = "loan_transactions"
    __table_args__ = (
        Index(
            "one_active_disbursement_per_loan",
            "loan_id",
            unique=True,
            postgresql_where=text("type = 'DISBURSEMENT' AND status <> 'FAILED'"),
            sqlite_where=text("type = 'DISBURSEMENT' AND status <> 'FAILED'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id"))
    type: Mapped[TransactionType] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(BigInteger)
    transaction_date: Mapped[date] = mapped_column(Date)
    status: Mapped[TransactionStatus] = mapped_column(String(30))
    fineract_transaction_id: Mapped[int | None] = mapped_column(Integer)
    principal_portion: Mapped[int | None] = mapped_column(BigInteger)
    interest_portion: Mapped[int | None] = mapped_column(BigInteger)
    fee_portion: Mapped[int | None] = mapped_column(BigInteger)
    penalty_portion: Mapped[int | None] = mapped_column(BigInteger)
    overpayment_portion: Mapped[int | None] = mapped_column(BigInteger)
    erp_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    loan: Mapped[Loan] = relationship()
