"""Initial schema: create service_records table and seed the ansible-bootstrap row.

Revision ID: 001
Revises:     (none — this is the root migration)
Create Date: 2026-01-01 00:00:00.000000

OWNERSHIP
---------
Schema is app-team territory. This migration — not the mariadb_instance DBA role
(ansible/roles/mariadb_instance) — creates the table and seeds the initial row.
The DBA role provisions the database container (database + account + access);
the app team fills it (schema + data). See design spec §8.2 and §8.4.

SEED ROW IDEMPOTENCY
--------------------
Alembic's version tracking (the alembic_version table) ensures this migration
runs only once: a second "alembic upgrade head" is a no-op when already at
revision 001. The explicit COUNT == 0 guard is belt-and-suspenders safety that
prevents a duplicate seed row if the migration is somehow replayed (e.g., after
a manual reset of alembic_version). Re-running produces no changes either way.

The ansible-bootstrap seed is the proof-of-life marker: 04_proof_of_life.yml
asserts that this exact message value appears in GET / output, proving the
app-owned migration reached the data layer (design spec §8.5).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Create service_records and insert the ansible-bootstrap seed row.

    Table columns:
      id         — INT AUTO_INCREMENT PRIMARY KEY
      message    — VARCHAR(255) NOT NULL
      created_at — TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    No per-table character-set override: the table inherits utf8mb4 /
    utf8mb4_unicode_ci from the database's default (set in
    images/database/rootfs/etc/my.cnf.d/00-corporate.cnf, §5.2).

    The running corp-app service issues NO DDL. This migration runs before
    the service is started, via "alembic upgrade head" in the app_release
    Ansible role (design spec §8.4).
    """
    # Create the service_records table.
    # Column types map to the MariaDB DDL equivalent:
    #   sa.Integer()     → INT
    #   sa.String(255)   → VARCHAR(255)
    #   sa.TIMESTAMP()   → TIMESTAMP
    op.create_table(
        "service_records",
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
            comment="Auto-increment primary key",
        ),
        sa.Column(
            "message",
            sa.String(255),
            nullable=False,
            comment="Record message text",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="Insertion timestamp, set by the database server",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Idempotent seed: insert the ansible-bootstrap row only if the table is empty.
    #
    # Implementation: op.execute() with a conditional INSERT avoids Python-level
    # queries and deprecated Alembic APIs (op.get_bind() is deprecated in 1.7+).
    #
    # The SQL reads: "INSERT one row with message='ansible-bootstrap' only when
    # no rows exist in service_records."
    #
    # FROM DUAL is required by MariaDB/MySQL: a bare SELECT without FROM is not
    # valid in MySQL/MariaDB (unlike some other databases). DUAL is the standard
    # MySQL/MariaDB dummy one-row table used when no real table is needed.
    # The WHERE NOT EXISTS clause filters that row to zero rows when service_records
    # already contains data, making the INSERT a no-op.
    #
    # Belt-and-suspenders: Alembic's version tracking prevents this migration from
    # running twice, but the WHERE guard ensures no duplicate even on replay.
    op.execute(
        sa.text(
            "INSERT INTO service_records (message) "
            "SELECT 'ansible-bootstrap' FROM DUAL "
            "WHERE NOT EXISTS (SELECT 1 FROM service_records)"
        )
    )


def downgrade() -> None:
    """
    Drop service_records.

    WARNING: This removes all rows including the ansible-bootstrap seed and any
    records created by POST /record or 04_proof_of_life.yml probe runs.
    There is no data recovery path after a downgrade.
    """
    op.drop_table("service_records")
