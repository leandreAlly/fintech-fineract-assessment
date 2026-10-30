import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from app.config import settings

API_KEY_HEADER = "X-API-Key"

api_key_header = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    description="API key issued to the calling system. Admin endpoints need an admin key.",
)

ApiKey = Annotated[str | None, Depends(api_key_header)]


def parse_keys(value: str) -> list[str]:
    return [key.strip() for key in value.split(",") if key.strip()]


def matches(candidate: str | None, keys: list[str]) -> bool:
    if not candidate:
        return False
    return any(secrets.compare_digest(candidate.encode(), key.encode()) for key in keys)


def require_api_key(api_key: ApiKey) -> None:
    keys = parse_keys(settings.api_keys) + parse_keys(settings.admin_api_keys)
    if not matches(api_key, keys):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            f"missing or invalid {API_KEY_HEADER}",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def require_admin_key(api_key: ApiKey) -> None:
    require_api_key(api_key)
    if not matches(api_key, parse_keys(settings.admin_api_keys)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this endpoint needs an admin API key")
