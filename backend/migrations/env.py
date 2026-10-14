from alembic import context
from service.db import Base, make_engine
from service import models  # noqa: F401
from service.settings import Settings

target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(url=Settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(Settings().database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
