from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

HTTP_CODES = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
}


def error_body(code: str, message: str) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    if correlation_id:
        error["correlation_id"] = correlation_id
    return {"error": error}


async def validation_error(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    body = error_body("VALIDATION_ERROR", "the request is not valid")
    body["error"]["details"] = [
        {"field": ".".join(str(p) for p in item["loc"]), "message": item["msg"]}
        for item in error.errors()
    ]
    return JSONResponse(status_code=422, content=body)


async def http_error(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, HTTPException)
    code = HTTP_CODES.get(error.status_code, f"HTTP_{error.status_code}")
    return JSONResponse(status_code=error.status_code, content=error_body(code, str(error.detail)))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, http_error)
