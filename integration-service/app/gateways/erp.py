from typing import Any

import httpx
from pydantic import BaseModel

from app.config import settings
from app.faults import with_faults


class ErpAccount(BaseModel):
    code: str
    name: str
    type: str
    currency: str
    external_reference: str


class ErpUnavailableError(Exception):
    pass


class ErpRejectedError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ErpClient:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def list_accounts(self) -> list[ErpAccount]:
        return [ErpAccount.model_validate(item) for item in self._get("/api/accounts")]

    def get_account_by_reference(self, external_reference: str) -> ErpAccount | None:
        return next(
            (a for a in self.list_accounts() if a.external_reference == external_reference),
            None,
        )

    def post_journal_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.http.post("/api/journal-entries", json=payload)
        except httpx.HTTPError as error:
            raise ErpUnavailableError(f"{type(error).__name__}: {error}") from error
        if response.status_code >= 500:
            raise ErpUnavailableError(f"ERP returned {response.status_code}")
        if response.status_code >= 400:
            raise ErpRejectedError(
                f"ERP rejected the entry ({response.status_code}): {response.text}",
                response.status_code,
            )
        body: dict[str, Any] = response.json()
        return body

    def _get(self, path: str) -> Any:
        try:
            response = self.http.get(path)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ErpUnavailableError(str(error)) from error
        return response.json()


def build_erp_client() -> ErpClient:
    return ErpClient(
        httpx.Client(
            base_url=settings.erp_base_url,
            timeout=settings.http_timeout_seconds,
            transport=with_faults(httpx.HTTPTransport(), "erp"),
        )
    )
