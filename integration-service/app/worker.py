import asyncio

import structlog

from app.db import SessionLocal
from app.deps import build_sync_service
from app.gateways.erp import build_erp_client
from app.gateways.fineract import build_fineract_client

log = structlog.get_logger()


def run_retry_pass() -> int:
    erp = build_erp_client()
    fineract = build_fineract_client()
    with SessionLocal() as session, erp.http, fineract.http:
        return build_sync_service(session, erp, fineract).retry_due()


async def retry_loop(interval_seconds: float) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            processed = await asyncio.to_thread(run_retry_pass)
        except Exception:
            log.exception("gl_sync_retry_pass_failed")
            continue
        if processed:
            log.info("gl_sync_retry_pass", processed=processed)
