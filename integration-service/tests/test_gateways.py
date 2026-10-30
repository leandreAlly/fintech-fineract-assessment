import json
from collections.abc import Callable

import httpx
import pytest

from app.gateways.erp import ErpClient, ErpUnavailableError
from app.gateways.fineract import (
    FineractClient,
    FineractRejectedError,
    FineractUnavailableError,
)

Handler = Callable[[httpx.Request], httpx.Response]


def fineract(handler: Handler) -> FineractClient:
    return FineractClient(
        httpx.Client(base_url="https://f/api/v1", transport=httpx.MockTransport(handler))
    )


def erp(handler: Handler) -> ErpClient:
    return ErpClient(httpx.Client(base_url="http://erp", transport=httpx.MockTransport(handler)))


def test_sync_gl_account_posts_payload_and_returns_resource_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"resourceId": 6})

    assert fineract(handler).sync_gl_account({"externalId": "GL-1200"}) == 6
    assert seen[0].url.path == "/api/v1/glaccounts/sync"
    assert json.loads(seen[0].content) == {"externalId": "GL-1200"}


def test_conflict_is_rejected_with_fineract_message() -> None:
    body = {"errors": [{"defaultUserMessage": "GL account GL-1200 differs"}]}

    with pytest.raises(FineractRejectedError) as raised:
        fineract(lambda _: httpx.Response(409, json=body)).sync_gl_account({})

    assert raised.value.status_code == 409
    assert raised.value.message == "GL account GL-1200 differs"


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_server_errors_are_unavailable(status_code: int) -> None:
    with pytest.raises(FineractUnavailableError):
        fineract(lambda _: httpx.Response(status_code)).sync_gl_account({})


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_transport_errors_are_unavailable(error: httpx.TransportError) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(FineractUnavailableError):
        fineract(handler).sync_gl_account({})


def test_erp_account_lookup_by_reference() -> None:
    accounts = [
        {
            "code": "1100",
            "name": "Bank",
            "type": "ASSET",
            "currency": "RWF",
            "external_reference": "GL-1100",
            "created_at": "2026-10-29T00:00:00Z",
        },
    ]
    client = erp(lambda _: httpx.Response(200, json=accounts))

    found = client.get_account_by_reference("GL-1100")

    assert found is not None and found.code == "1100"
    assert client.get_account_by_reference("GL-9999") is None


def test_erp_down_raises_unavailable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ErpUnavailableError):
        erp(handler).list_accounts()
