"""Alembic migration environment for the application database."""
from alembic import context
from main import Base, engine

target_metadata = Base.metadata


def run_migrations_offline():
    raise RuntimeError('Offline migration mode is disabled; use an authorized database connection.')


def run_migrations_online():
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
