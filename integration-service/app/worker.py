import asyncio
from typing import Any

import structlog

from app.db import SessionLocal
from app.deps import build_loan_service, build_sync_service
from app.gateways.erp import build_erp_client
from app.gateways.fineract import build_fineract_client
from app.observability import new_correlation_id

log = structlog.get_logger()


def run_retry_pass() -> int:
    erp = build_erp_client()
    fineract = build_fineract_client()
    with SessionLocal() as session, erp.http, fineract.http:
        return build_sync_service(session, erp, fineract).retry_due()


def run_recovery_pass() -> dict[str, Any]:
    structlog.contextvars.bind_contextvars(correlation_id=f"recovery-{new_correlation_id()}")
    erp = build_erp_client()
    fineract = build_fineract_client()
    with SessionLocal() as session, erp.http, fineract.http:
        return build_loan_service(session, fineract, erp).recover()


async def retry_loop(interval_seconds: float) -> None:
    while True:
        try:
            processed = await asyncio.to_thread(run_retry_pass)
            if processed:
                log.info("gl_sync_retry_pass", processed=processed)
        except Exception:
            log.exception("gl_sync_retry_pass_failed")
        try:
            report = await asyncio.to_thread(run_recovery_pass)
            if report["transactions"]:
                log.info("transaction_recovery_pass", **report)
        except Exception:
            log.exception("transaction_recovery_pass_failed")
        await asyncio.sleep(interval_seconds)
