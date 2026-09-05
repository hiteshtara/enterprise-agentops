"""add cleanup claim lease

Revision ID: 18d8b2b8c18d
Revises: 6bf50066668b
Create Date: 2026-09-05 16:30:50.225043

Adds the durable claim that stops two cleanup processes from both deleting the
same override: `claim_token`, `claimed_at` and `lease_until`, plus an index on
the lease so a lapsed claim can be found cheaply. Purely additive -- every
column is nullable, so existing ACTIVE rows are unclaimed and immediately
claimable, which is the correct starting state.

Autogenerate again proposed dropping the server defaults on
`hospitality_knowledge.audience`, `.safety_status` and `.safety_reasons_json`.
Those defaults exist in the database but not in the model, so it reads the
drift as something to remove. They are unrelated to this change and removing
them would alter how an existing feature inserts rows, so they are deliberately
not included here -- the same omission, for the same reason, as
6bf50066668b.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "18d8b2b8c18d"
down_revision: str | Sequence[str] | None = "6bf50066668b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pricing_cleanups", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("claim_token", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("claimed_at", sa.String(length=40), nullable=True)
        )
        batch_op.add_column(
            sa.Column("lease_until", sa.String(length=40), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_pricing_cleanups_lease_until"),
            ["lease_until"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("pricing_cleanups", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pricing_cleanups_lease_until"))
        batch_op.drop_column("lease_until")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("claim_token")
