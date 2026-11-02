"""
Alembic migration environment for corp-app.

DATABASE URL SOURCE (G12)
--------------------------
The database URL is read from /etc/corp-app/config.ini — the same file
the running corp-app service reads. This guarantees that "alembic upgrade head"
and the running service always target the same database instance.

If /etc/corp-app/config.ini is absent (e.g., during local development or CI
dry-run before the Ansible app_deploy role has run), the environment falls
back to:
  1. The DB_URL environment variable, if set.
  2. A local SQLite database (corp-app-dryrun.db) as a last resort.

The fallback is documented as dry-run only — MariaDB-specific SQL in the
migrations may not be compatible with SQLite. For production use, config.ini
must exist before running migrations.

CORPORATE STACK (G15)
---------------------
This env.py uses SQLAlchemy and Alembic — both baked into the application
runtime image. It is executed inside the /opt/corp-app/venv virtualenv
(--system-site-packages), so these packages are available without re-installation.
"""

from __future__ import annotations

import configparser
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# ---------------------------------------------------------------------------
# Config path — must match the application executable (G12)
# ---------------------------------------------------------------------------
_CONFIG_PATH = "/etc/corp-app/config.ini"

# Fallback URL for dry-run / development when config.ini is absent.
# The DB_URL env var allows overriding to a real MariaDB URL without config.ini.
_FALLBACK_DB_URL: str = os.environ.get(
    "DB_URL",
    "sqlite:///./corp-app-dryrun.db",
)

# ---------------------------------------------------------------------------
# Alembic Config object — provides access to values in alembic.ini
# ---------------------------------------------------------------------------
config = context.config

# Enable logging configuration from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No SQLAlchemy ORM MetaData: corp-app uses raw SQL in migrations rather than
# the ORM. Set target_metadata = None (autogenerate is not used).
target_metadata = None


# ---------------------------------------------------------------------------
# Database URL resolution
# ---------------------------------------------------------------------------


def _get_db_url() -> str:
    """
    Build the SQLAlchemy database URL from /etc/corp-app/config.ini.

    Falls back to DB_URL env var or a local SQLite path when config.ini is
    not present. The fallback is only suitable for dry-run / offline SQL
    generation (alembic upgrade --sql) — do not run online migrations against
    SQLite in production.
    """
    cfg = configparser.ConfigParser()
    files_read = cfg.read(_CONFIG_PATH)

    if not files_read:
        print(
            f"WARNING: {_CONFIG_PATH} not found.\n"
            f"  Using fallback URL: {_FALLBACK_DB_URL!r}\n"
            "  This is only suitable for dry-run / offline SQL generation.\n"
            "  For production migrations, ensure config.ini exists first.",
            file=sys.stderr,
        )
        return _FALLBACK_DB_URL

    db_host = cfg.get("database", "host")
    db_port = cfg.getint("database", "port", fallback=3306)
    db_name = cfg.get("database", "name")
    db_user = cfg.get("database", "user")
    db_password = cfg.get("database", "password")

    return (
        f"mysql+pymysql://{db_user}:{db_password}"
        f"@{db_host}:{db_port}/{db_name}?charset=utf8mb4"
    )


# ---------------------------------------------------------------------------
# Migration execution modes
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Emits SQL to stdout without establishing a live database connection.
    Useful for generating and reviewing SQL before applying it (dry-run).
    Invoke with: alembic upgrade --sql head
    """
    url = _get_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    Establishes a live database connection and applies pending migrations.
    Called by "alembic upgrade head" in the app_release Ansible role (§8.4).

    NullPool is used: migration runs are one-shot operations; connection
    pooling provides no benefit and would prevent clean process exit.
    """
    url = _get_db_url()
    connectable = create_engine(
        url,
        poolclass=pool.NullPool,  # no persistent pool for one-shot migration runs
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------------
# Entry point — called by Alembic when env.py is executed
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
