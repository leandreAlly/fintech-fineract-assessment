from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def backoff_delay(attempts: int, base_seconds: float, max_seconds: float) -> timedelta:
    return timedelta(seconds=min(max_seconds, base_seconds * 2 ** (attempts - 1)))


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 10
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 60.0
    abandon_after_seconds: float = 900.0

    def next_attempt(self, now: datetime, attempts: int) -> datetime:
        return now + backoff_delay(attempts, self.backoff_base_seconds, self.backoff_max_seconds)
