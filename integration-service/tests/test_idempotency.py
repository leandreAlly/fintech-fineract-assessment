from sqlalchemy.orm import Session

from app.idempotency import ApiResult, Finished, IdempotencyStore, Started, request_hash

PAYLOAD = {"client_uid": "SME-000001", "name": "Umucyo Traders"}


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
