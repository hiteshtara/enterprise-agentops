"""Alembic environment for AgentGuard.

The database URL is never configured here or in alembic.ini -- it is resolved
by the application, so migrations and the running service can never disagree
about which database they mean.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Importing the models registers every table on Base.metadata, which is what
# --autogenerate diffs against. New model modules must be imported here.
from app import db_models  # noqa: F401
from app.database import Base, resolve_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single source of truth for the CLI: the same resolver the application uses.
#
# A caller that has already set a URL on the Config -- a test pointing at its own
# temporary file -- must win. Overriding unconditionally would silently redirect
# programmatic migrations at the development database.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", resolve_database_url())

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Keep Alembic's own bookkeeping table out of autogenerate diffs."""
    return not (type_ == "table" and name == "alembic_version")


def common_options() -> dict:
    return {
        "target_metadata": target_metadata,
        "include_object": include_object,
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead, so future column changes are expressible.
        "render_as_batch": True,
        "compare_type": True,
        "compare_server_default": True,
    }


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **common_options(),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)

    if connectable is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

        with connectable.connect() as connection:
            context.configure(connection=connection, **common_options())

            with context.begin_transaction():
                context.run_migrations()

        return

    # A connection supplied by a caller (tests) is used as-is and not closed.
    context.configure(connection=connectable, **common_options())

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
