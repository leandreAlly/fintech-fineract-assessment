from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from app.idempotency import error_body

HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
}


class ErrorDetail(BaseModel):
    code: str
    message: str
    correlation_id: str | None = None
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


def documented(description: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": description}


COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: documented("Missing or invalid X-API-Key"),
    409: documented("Conflict: same external_reference with a different payload, or a state rule"),
    422: documented("Validation error, or Fineract rejected the operation"),
    503: documented("Fineract or the ERP is unavailable, or the loan is busy; safe to retry"),
}


def field_errors(error: RequestValidationError) -> list[dict[str, Any]]:
    return [
        {
            "field": ".".join(str(part) for part in item["loc"]),
            "message": item["msg"],
            "type": item["type"],
        }
        for item in error.errors()
    ]


async def validation_error(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    body = error_body("VALIDATION_ERROR", "the request is not valid")
    body["error"]["details"] = field_errors(error)
    return JSONResponse(status_code=422, content=body)


async def http_error(_: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, HTTPException)
    code = HTTP_CODES.get(error.status_code, f"HTTP_{error.status_code}")
    return JSONResponse(
        status_code=error.status_code,
        content=error_body(code, str(error.detail)),
        headers=error.headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, http_error)
