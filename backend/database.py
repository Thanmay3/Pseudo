import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

load_dotenv(Path(__file__).with_name(".env"))


@lru_cache
def get_engine():
    value = os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("Set DATABASE_URL in backend/.env. See README.md for PostgreSQL setup.")
    try:
        url = make_url(value)
    except Exception:
        raise RuntimeError("DATABASE_URL is not a valid database URL.") from None
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("DATABASE_URL must point to PostgreSQL; no fallback database is used.")
    url = url.set(drivername="postgresql+psycopg")
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5}, hide_parameters=True)

    @event.listens_for(engine, "connect")
    def use_utc(connection, record):
        # Keep timestamps identical before and after reading them from PostgreSQL,
        # regardless of the server's configured local timezone.
        with connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
        connection.commit()

    return engine


def get_db():
    # Each request gets its own unit of work; close rolls back uncommitted work
    # and returns the connection to the engine's pool, even on an exception.
    with Session(get_engine(), expire_on_commit=False) as db:
        yield db
