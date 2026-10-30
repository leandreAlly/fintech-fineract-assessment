import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import IdempotencyRecord, OperationStatus


@dataclass(frozen=True)
class ApiResult:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class Started:
    record: IdempotencyRecord


@dataclass(frozen=True)
class Finished:
    result: ApiResult


Outcome = Started | Finished


def error_body(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def begin(self, operation: str, external_reference: str, payload: dict[str, Any]) -> Outcome:
        digest = request_hash(payload)
        record = IdempotencyRecord(
            operation=operation,
            external_reference=external_reference,
            request_hash=digest,
            status=OperationStatus.IN_PROGRESS,
        )
        self.session.add(record)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.find(operation, external_reference)
            if existing is None:
                return self.begin(operation, external_reference, payload)
            return Finished(self._replay(existing, digest))
        return Started(record)

    def complete(self, record: IdempotencyRecord, result: ApiResult) -> None:
        record.status = OperationStatus.COMPLETED
        record.response_status = result.status_code
        record.response_body = result.body
        self.session.commit()

    def release(self, record: IdempotencyRecord) -> None:
        self.session.rollback()
        self.session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.id == record.id))
        self.session.commit()

    def find(self, operation: str, external_reference: str) -> IdempotencyRecord | None:
        return self.session.scalar(
            select(IdempotencyRecord)
            .where(IdempotencyRecord.operation == operation)
            .where(IdempotencyRecord.external_reference == external_reference)
        )

    def _replay(self, existing: IdempotencyRecord, digest: str) -> ApiResult:
        if existing.request_hash != digest:
            return ApiResult(
                409,
                error_body(
                    "IDEMPOTENCY_CONFLICT",
                    f"external_reference {existing.external_reference} was already used "
                    f"for {existing.operation} with a different payload",
                ),
            )
        if existing.status == OperationStatus.IN_PROGRESS:
            return ApiResult(
                202,
                {
                    "operation": existing.operation,
                    "external_reference": existing.external_reference,
                    "status": OperationStatus.IN_PROGRESS,
                },
            )
        assert existing.response_status is not None and existing.response_body is not None
        return ApiResult(
            200 if existing.response_status == 201 else existing.response_status,
            existing.response_body,
        )
