"""Upgrade existing indexes without discarding persisted edges."""

from sqlalchemy import text

from codepilot.storage.db import create_engine_from_url, init_db


def test_init_db_migrates_existing_edge_rows_idempotently():
    engine = create_engine_from_url("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE edges (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO edges (id) VALUES ('preserved')"))
    init_db(engine)
    init_db(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, line FROM edges")).all() == [("preserved", 0)]
        connection.execute(text("INSERT INTO edges (id) VALUES ('new')"))
        assert connection.execute(text("SELECT line FROM edges WHERE id='new'")).scalar_one() == 0
    engine.dispose()
