from alembic import context
from database import get_engine
from models import Base

if context.is_offline_mode():
    raise RuntimeError("This migration inspects existing tables; run alembic upgrade head online.")

with get_engine().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
