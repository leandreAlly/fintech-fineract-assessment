from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

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
