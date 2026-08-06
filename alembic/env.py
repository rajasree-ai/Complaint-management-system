"""Alembic env.py for this Flask/SQLAlchemy project.

This file looks for DATABASE_URL or NEW_DATABASE_URL in the environment to set the SQLAlchemy URL.
It uses the project's `database.db` SQLAlchemy instance as target_metadata so auto-generated
revisions reflect the models in models.py.
"""
from __future__ import with_statement
import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# If DATABASE_URL or NEW_DATABASE_URL is set, prefer it over the ini setting
db_url = os.environ.get('DATABASE_URL') or os.environ.get('NEW_DATABASE_URL')
if db_url:
    config.set_main_option('sqlalchemy.url', db_url)

# Import the project's metadata
# Ensure this import path matches your project layout
from database import db
target_metadata = db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=False,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
