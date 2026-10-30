import logging
import re
import sys
import time
import uuid
from typing import Any

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "X-Correlation-ID"
VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")

SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
]

log = structlog.get_logger()


def add_service(service: str) -> Any:
    def processor(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
        event["service"] = service
        return event

    return processor


def configure_logging(service: str, level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            *SHARED_PROCESSORS,
            add_service(service),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                *SHARED_PROCESSORS,
                add_service(service),
                structlog.stdlib.add_logger_name,
            ],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def correlation_id_from(value: str | None) -> str:
    return value if value and VALID_CORRELATION_ID.match(value) else new_correlation_id()


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation_id = correlation_id_from(Headers(scope=scope).get(CORRELATION_HEADER))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_header(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            log.info(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            structlog.contextvars.clear_contextvars()
