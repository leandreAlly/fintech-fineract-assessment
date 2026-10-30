from typing import Any

import httpx

from app.config import settings

LOAN_LOCKED = "error.msg.loan.locked"


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

    def office_id_by_external_id(self, external_id: str) -> int | None:
        offices: list[dict[str, Any]] = self._get("/offices")
        return next((int(o["id"]) for o in offices if o.get("externalId") == external_id), None)

    def create_client(self, payload: dict[str, Any]) -> int:
        return int(self._post("/clients", payload)["clientId"])

    def client_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        body: dict[str, Any] | None = self._get(
            f"/clients/external-id/{external_id}", allow_404=True
        )
        return body

    def client(self, client_id: int) -> dict[str, Any]:
        body: dict[str, Any] = self._get(f"/clients/{client_id}")
        return body

    def loan_product_id_by_external_id(self, external_id: str) -> int | None:
        products: list[dict[str, Any]] = self._get("/loanproducts")
        return next((int(p["id"]) for p in products if p.get("externalId") == external_id), None)

    def create_loan(self, payload: dict[str, Any]) -> int:
        return int(self._post("/loans", payload)["loanId"])

    def loan_by_external_id(self, external_id: str) -> dict[str, Any] | None:
        body: dict[str, Any] | None = self._get(f"/loans/external-id/{external_id}", allow_404=True)
        return body

    def loan(self, loan_id: int) -> dict[str, Any]:
        body: dict[str, Any] = self._get(f"/loans/{loan_id}")
        return body

    def approve_loan(self, loan_id: int, payload: dict[str, Any]) -> None:
        self._post(f"/loans/{loan_id}?command=approve", payload)

    def disburse_loan(self, loan_id: int, payload: dict[str, Any]) -> None:
        self._post(f"/loans/{loan_id}?command=disburse", payload)

    def repay_loan(self, loan_id: int, payload: dict[str, Any]) -> None:
        self._post(f"/loans/{loan_id}/transactions?command=repayment", payload)

    def loan_transaction_by_external_id(
        self, loan_id: int, external_id: str
    ) -> dict[str, Any] | None:
        body: dict[str, Any] | None = self._get(
            f"/loans/{loan_id}/transactions/external-id/{external_id}", allow_404=True
        )
        return body

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = self._send("POST", path, payload)
        return body

    def _get(self, path: str, allow_404: bool = False) -> Any:
        return self._send("GET", path, None, allow_404)

    def _send(
        self, method: str, path: str, payload: dict[str, Any] | None, allow_404: bool = False
    ) -> Any:
        try:
            response = self.http.request(method, path, json=payload)
        except httpx.TransportError as error:
            raise FineractUnavailableError(f"{type(error).__name__}: {error}") from error
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 500:
            raise FineractUnavailableError(
                f"Fineract returned {response.status_code}", response.status_code
            )
        if response.status_code == 409 and error_code(response) == LOAN_LOCKED:
            raise FineractUnavailableError(error_message(response), response.status_code)
        if response.status_code >= 400:
            raise FineractRejectedError(error_message(response), response.status_code)
        return response.json()


def error_code(response: httpx.Response) -> str | None:
    try:
        code = response.json().get("userMessageGlobalisationCode")
    except ValueError:
        return None
    return str(code) if code else None


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
