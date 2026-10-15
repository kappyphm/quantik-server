from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

SCHEMA_REVISION = "0007"


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    options = {"check_same_thread": False, "timeout": 15} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def sqlite_options(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
