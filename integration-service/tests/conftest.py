from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.main import app
from app.security import require_admin_key, require_api_key


@pytest.fixture
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture(autouse=True)
def authenticated(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("auth"):
        yield
        return
    app.dependency_overrides[require_api_key] = lambda: None
    app.dependency_overrides[require_admin_key] = lambda: None
    yield
    app.dependency_overrides.pop(require_api_key, None)
    app.dependency_overrides.pop(require_admin_key, None)
