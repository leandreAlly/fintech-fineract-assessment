import os
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import NoReturn

import httpx
import structlog
from pydantic import BaseModel, Field

from app.config import settings

log = structlog.get_logger()


class FaultOperation(StrEnum):
    FINERACT_DISBURSE = "fineract.disburse"
    FINERACT_REPAYMENT = "fineract.repayment"
    ERP_JOURNAL_ENTRY = "erp.journal_entry"


class FaultMode(StrEnum):
    FAIL_BEFORE = "fail_before"
    TIMEOUT_AFTER = "timeout_after"
    CRASH_AFTER = "crash_after"


class Fault(BaseModel):
    operation: FaultOperation
    mode: FaultMode
    times: int = Field(default=1, ge=1, le=100)


def operation_of(target: str, request: httpx.Request) -> FaultOperation | None:
    if request.method != "POST":
        return None
    if target == "fineract":
        command = request.url.params.get("command")
        if command == "disburse":
            return FaultOperation.FINERACT_DISBURSE
        if command == "repayment":
            return FaultOperation.FINERACT_REPAYMENT
    if target == "erp" and request.url.path == "/api/journal-entries":
        return FaultOperation.ERP_JOURNAL_ENTRY
    return None


def exit_now(code: int) -> NoReturn:
    os._exit(code)


class FaultInjector:
    def __init__(self, exit_process: Callable[[int], NoReturn] = exit_now) -> None:
        self.exit_process = exit_process
        self._faults: dict[FaultOperation, Fault] = {}
        self._lock = threading.Lock()

    def arm(self, fault: Fault) -> None:
        with self._lock:
            self._faults[fault.operation] = fault

    def active(self) -> list[Fault]:
        with self._lock:
            return list(self._faults.values())

    def clear(self) -> None:
        with self._lock:
            self._faults.clear()

    def take(self, operation: FaultOperation) -> FaultMode | None:
        with self._lock:
            fault = self._faults.get(operation)
            if fault is None:
                return None
            if fault.times == 1:
                del self._faults[operation]
            else:
                self._faults[operation] = fault.model_copy(update={"times": fault.times - 1})
            return fault.mode


class FaultInjectingTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, injector: FaultInjector, target: str) -> None:
        self.inner = inner
        self.injector = injector
        self.target = target

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        operation = operation_of(self.target, request)
        mode = self.injector.take(operation) if operation else None
        if mode == FaultMode.FAIL_BEFORE:
            raise httpx.ConnectError(f"injected {operation} fail_before", request=request)
        response = self.inner.handle_request(request)
        if mode is None:
            return response
        response.read()
        response.close()
        if mode == FaultMode.TIMEOUT_AFTER:
            raise httpx.ReadTimeout(
                f"injected {operation} timeout_after: request processed, response lost",
                request=request,
            )
        log.warning("injected_crash", operation=operation, status_code=response.status_code)
        self.injector.exit_process(1)

    def close(self) -> None:
        self.inner.close()


injector = FaultInjector()


def with_faults(transport: httpx.BaseTransport, target: str) -> httpx.BaseTransport:
    if not settings.fault_injection_enabled:
        return transport
    return FaultInjectingTransport(transport, injector, target)
