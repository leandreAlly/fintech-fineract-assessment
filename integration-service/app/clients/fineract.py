from typing import Any

import httpx

from app.config import settings


class FineractError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class FineractUnavailableError(FineractError):
    pass


class FineractRejectedError(FineractError):
    pass


class FineractClient:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def sync_gl_account(self, payload: dict[str, Any]) -> int:
        body = self._post("/glaccounts/sync", payload)
        return int(body["resourceId"])

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.http.post(path, json=payload)
        except httpx.TransportError as error:
            raise FineractUnavailableError(f"{type(error).__name__}: {error}") from error
        if response.status_code >= 500:
            raise FineractUnavailableError(
                f"Fineract returned {response.status_code}", response.status_code
            )
        if response.status_code >= 400:
            raise FineractRejectedError(error_message(response), response.status_code)
        body: dict[str, Any] = response.json()
        return body


def error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    errors = body.get("errors") or []
    if errors and errors[0].get("defaultUserMessage"):
        return str(errors[0]["defaultUserMessage"])
    return str(body.get("defaultUserMessage") or f"HTTP {response.status_code}")


def build_fineract_client() -> FineractClient:
    return FineractClient(
        httpx.Client(
            base_url=settings.fineract_base_url,
            auth=(settings.fineract_username, settings.fineract_password),
            headers={"Fineract-Platform-TenantId": settings.fineract_tenant},
            verify=settings.fineract_verify_tls,
            timeout=settings.http_timeout_seconds,
        )
    )
