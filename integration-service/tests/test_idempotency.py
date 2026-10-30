from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.idempotency import ApiResult, Finished, IdempotencyStore, Started, request_hash
from app.models import IdempotencyRecord

PAYLOAD = {"client_uid": "SME-000001", "name": "Umucyo Traders"}


class MovingClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def stamp(session: Session, record: IdempotencyRecord, at: datetime) -> None:
    session.execute(
        update(IdempotencyRecord).where(IdempotencyRecord.id == record.id).values(updated_at=at)
    )
    session.commit()


def test_new_reference_starts_operation(session: Session) -> None:
    outcome = IdempotencyStore(session).begin("CREATE_CLIENT", "REF-1", PAYLOAD)

    assert isinstance(outcome, Started)


def test_finished_reference_replays_original_body_with_200(session: Session) -> None:
    store = IdempotencyStore(session)
    started = store.begin("CREATE_CLIENT", "REF-1", PAYLOAD)
    assert isinstance(started, Started)
    store.complete(started.record, ApiResult(201, {"client_uid": "SME-000001"}))

    replay = store.begin("CREATE_CLIENT", "REF-1", dict(reversed(PAYLOAD.items())))

    assert replay == Finished(ApiResult(200, {"client_uid": "SME-000001"}))


def test_non_201_results_replay_with_their_own_status(session: Session) -> None:
    store = IdempotencyStore(session)
    started = store.begin("CREATE_CLIENT", "REF-1", PAYLOAD)
    assert isinstance(started, Started)
    store.complete(started.record, ApiResult(422, {"error": {"code": "FINERACT_REJECTED"}}))

    replay = store.begin("CREATE_CLIENT", "REF-1", PAYLOAD)

    assert isinstance(replay, Finished) and replay.result.status_code == 422


def test_in_progress_reference_returns_202(session: Session) -> None:
    store = IdempotencyStore(session)
    store.begin("CREATE_CLIENT", "REF-1", PAYLOAD)

    replay = store.begin("CREATE_CLIENT", "REF-1", PAYLOAD)

    assert isinstance(replay, Finished)
    assert replay.result.status_code == 202
    assert replay.result.body["status"] == "IN_PROGRESS"


def test_same_reference_different_payload_returns_409(session: Session) -> None:
    store = IdempotencyStore(session)
    store.begin("CREATE_CLIENT", "REF-1", PAYLOAD)

    replay = store.begin("CREATE_CLIENT", "REF-1", PAYLOAD | {"name": "Other"})

    assert isinstance(replay, Finished)
    assert replay.result.status_code == 409
    assert replay.result.body["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_reference_is_scoped_by_operation(session: Session) -> None:
    store = IdempotencyStore(session)
    store.begin("CREATE_CLIENT", "REF-1", PAYLOAD)

    assert isinstance(store.begin("CREATE_LOAN", "REF-1", PAYLOAD), Started)


def test_released_reference_can_run_again(session: Session) -> None:
    store = IdempotencyStore(session)
    started = store.begin("CREATE_CLIENT", "REF-1", PAYLOAD)
    assert isinstance(started, Started)

    store.release(started.record)

    assert isinstance(store.begin("CREATE_CLIENT", "REF-1", PAYLOAD), Started)


def test_hash_ignores_key_order() -> None:
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})


def test_in_progress_record_within_its_lease_is_not_taken_over(session: Session) -> None:
    clock = MovingClock()
    store = IdempotencyStore(session, lease=timedelta(seconds=60), clock=clock)
    store.begin("REPAY_LOAN", "RPY-1", {"amount": 1})

    clock.advance(30)

    assert isinstance(store.begin("REPAY_LOAN", "RPY-1", {"amount": 1}), Finished)


def test_in_progress_record_past_its_lease_is_taken_over(session: Session) -> None:
    clock = MovingClock()
    store = IdempotencyStore(session, lease=timedelta(seconds=60), clock=clock)
    first = store.begin("REPAY_LOAN", "RPY-1", {"amount": 1})
    assert isinstance(first, Started)
    stamp(session, first.record, clock())

    clock.advance(61)
    second = store.begin("REPAY_LOAN", "RPY-1", {"amount": 1})
    third = store.begin("REPAY_LOAN", "RPY-1", {"amount": 1})

    assert isinstance(second, Started)
    assert second.record.id == first.record.id
    assert isinstance(third, Finished)
    assert third.result.status_code == 202


def test_expired_lease_with_a_different_payload_is_still_a_conflict(session: Session) -> None:
    clock = MovingClock()
    store = IdempotencyStore(session, lease=timedelta(seconds=60), clock=clock)
    first = store.begin("REPAY_LOAN", "RPY-1", {"amount": 1})
    assert isinstance(first, Started)
    stamp(session, first.record, clock())

    clock.advance(61)
    outcome = store.begin("REPAY_LOAN", "RPY-1", {"amount": 2})

    assert isinstance(outcome, Finished)
    assert outcome.result.status_code == 409


def test_completed_record_is_never_taken_over(session: Session) -> None:
    clock = MovingClock()
    store = IdempotencyStore(session, lease=timedelta(seconds=60), clock=clock)
    first = store.begin("REPAY_LOAN", "RPY-1", {"amount": 1})
    assert isinstance(first, Started)
    store.complete(first.record, ApiResult(201, {"ok": True}))
    stamp(session, first.record, clock())

    clock.advance(3600)
    outcome = store.begin("REPAY_LOAN", "RPY-1", {"amount": 1})

    assert isinstance(outcome, Finished)
    assert outcome.result == ApiResult(200, {"ok": True})
