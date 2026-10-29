from typing import Any

import httpx
from pydantic import BaseModel

from app.config import settings


class ErpAccount(BaseModel):
    code: str
    name: str
    type: str
    currency: str
    external_reference: str


class ErpUnavailableError(Exception):
    pass


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

    def _get(self, path: str) -> Any:
        try:
            response = self.http.get(path)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ErpUnavailableError(str(error)) from error
        return response.json()


def build_erp_client() -> ErpClient:
    return ErpClient(
        httpx.Client(base_url=settings.erp_base_url, timeout=settings.http_timeout_seconds)
    )
