"""
Alembic migration environment for corp-app.

DATABASE URL SOURCE (G12)
--------------------------
The database URL is read from /etc/corp-app/config.ini — the same file
the running corp-app service reads. This guarantees that "alembic upgrade head"
and the running service always target the same database instance.

ONLINE MODE (alembic upgrade head)
  config.ini must exist. If it is absent and DB_URL env var is not set,
  migration fails immediately with a clear error. SQLite is never used for
  live migrations — MariaDB-specific DDL and the FROM DUAL seed would not work.

OFFLINE MODE (alembic upgrade --sql)
  Emits SQL without connecting. Falls back to DB_URL env var for the
  dialect hint, or to a SQLite URL as a last resort (for syntax checking
  only — the generated SQL will target MariaDB syntax regardless).

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
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import create_engine, pool

# ---------------------------------------------------------------------------
# Config path — must match the application executable (G12)
# ---------------------------------------------------------------------------
_CONFIG_PATH = "/etc/corp-app/config.ini"

# SQLite fallback URL used ONLY in offline mode (--sql) when config.ini is
# absent and DB_URL is not set. Online migrations never use this fallback.
_OFFLINE_SQLITE_FALLBACK = "sqlite:///./corp-app-dryrun.db"

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


def _get_db_url(*, allow_sqlite_fallback: bool) -> str:
    """
    Build the SQLAlchemy database URL from /etc/corp-app/config.ini.

    Parameters
    ----------
    allow_sqlite_fallback:
        True  → offline mode: fall back to DB_URL env var, then to a local
                SQLite path (for SQL generation / syntax checking only).
        False → online mode: raise RuntimeError if config.ini is absent and
                DB_URL is not set. Never silently use SQLite for live migrations.

    user and password are percent-encoded with quote_plus so that special
    characters (e.g. @, /, #, +) in credentials do not corrupt the URL.
    """
    cfg = configparser.ConfigParser()
    files_read = cfg.read(_CONFIG_PATH)

    if not files_read:
        env_url = os.environ.get("DB_URL")
        if env_url:
            print(
                f"WARNING: {_CONFIG_PATH} not found; using DB_URL env var.",
                file=sys.stderr,
            )
            return env_url

        if allow_sqlite_fallback:
            # Offline mode only: emit dialect-tagged SQL without a live connection.
            # The generated SQL targets MariaDB syntax even with a SQLite URL here.
            print(
                f"WARNING: {_CONFIG_PATH} not found and DB_URL is not set.\n"
                f"  Using SQLite fallback ({_OFFLINE_SQLITE_FALLBACK!r}) for "
                "offline SQL generation only.\n"
                "  Do NOT apply this SQL to a SQLite database — it contains "
                "MariaDB-specific DDL (FROM DUAL, TIMESTAMP, AUTO_INCREMENT).",
                file=sys.stderr,
            )
            return _OFFLINE_SQLITE_FALLBACK

        # Online mode: fail fast — applying migrations without a real MariaDB
        # connection would produce incorrect or destructive results.
        raise RuntimeError(
            f"Cannot run online migrations: {_CONFIG_PATH} not found "
            "and the DB_URL environment variable is not set.\n"
            "  Deploy the application config with the app_deploy Ansible role "
            "before running 'alembic upgrade head'."
        )

    db_host = cfg.get("database", "host")
    db_port = cfg.getint("database", "port", fallback=3306)
    db_name = cfg.get("database", "name")
    db_user = cfg.get("database", "user")
    db_password = cfg.get("database", "password")

    # Percent-encode credentials: special characters in user/password break
    # URL parsing if embedded raw in the connection string.
    return (
        f"mysql+pymysql://{quote_plus(db_user)}:{quote_plus(db_password)}"
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

    Falls back to DB_URL env var or a local SQLite URL when config.ini is
    absent (SQLite fallback is permitted here because no live DB is touched).
    """
    url = _get_db_url(allow_sqlite_fallback=True)
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

    Establishes a live MariaDB connection and applies pending migrations.
    Called by "alembic upgrade head" in the app_release Ansible role (§8.4).

    Fails fast (RuntimeError) if /etc/corp-app/config.ini is absent and
    DB_URL is not set — SQLite is never used for live online migrations.

    NullPool is used: migration runs are one-shot operations; connection
    pooling provides no benefit and would prevent clean process exit.
    """
    url = _get_db_url(allow_sqlite_fallback=False)  # raises if no real DB config
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
