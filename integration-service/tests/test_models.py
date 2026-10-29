import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import GlAccountSync, SyncStatus


def sync_row(external_reference: str = "GL-1200") -> GlAccountSync:
    return GlAccountSync(
        external_reference=external_reference,
        erp_code="1200",
        erp_name="Loan Receivable",
        erp_type="ASSET",
        currency="RWF",
    )


def test_new_row_starts_pending_with_no_attempts(session: Session) -> None:
    row = sync_row()
    session.add(row)
    session.commit()

    assert row.status == SyncStatus.SYNC_PENDING
    assert row.attempts == 0
    assert row.fineract_gl_account_id is None


def test_external_reference_is_unique(session: Session) -> None:
    session.add(sync_row())
    session.commit()
    session.add(sync_row())

    with pytest.raises(IntegrityError):
        session.commit()
