from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from rebar_service.config import get_settings


settings = get_settings()
DB_SCHEMA = settings.postgres_schema

config = context.config
config.set_main_option(
    "sqlalchemy.url",
    settings.database_url.render_as_string(hide_password=False).replace("%", "%%"),
)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=DB_SCHEMA,
        include_schemas=True,
    )

    context.execute(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"')
    context.execute(f'SET search_path TO "{DB_SCHEMA}", public')

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Создаём отдельную schema для rebar optimizer.
        connection.execute(
            text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"')
        )

        # Все неуточнённые таблицы миграции создаются внутри rebar.
        connection.execute(
            text(f'SET search_path TO "{DB_SCHEMA}", public')
        )

        # CREATE SCHEMA должен быть зафиксирован до запуска Alembic.
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=None,
            version_table_schema=DB_SCHEMA,
            include_schemas=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()