"""add manual cleanup resolution

Revision ID: e3d94969cf61
Revises: 239943ef47d4
Create Date: 2026-09-07 02:20:00.000000

Three nullable columns on `pricing_cleanups`, and nothing else.

They exist so a person closing an obligation automation gave up on does not
overwrite automation's own account of it. `resolution` and `resolved_at`
already hold what the worker concluded -- "the change was sent but could not
be read back", and when it stopped. Reusing those to record a human decision
would destroy exactly the history a reviewer needs, and leave one field
speaking in two voices.

`MANUALLY_RESOLVED` itself needs no migration: `state` is a plain String
column, so the new value is a code change only.

`--autogenerate` again proposed dropping three `server_default`s on
`hospitality_knowledge` -- unrelated pre-existing drift, **removed by hand**
for the third time. It should be reconciled in its own revision by whoever
owns that model; it must not keep riding along inside revisions named for
other features.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3d94969cf61"
down_revision: str | Sequence[str] | None = "239943ef47d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the three manual-resolution columns. Additive, nullable."""
    with op.batch_alter_table("pricing_cleanups", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("manual_resolution", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("resolved_by_user_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("manually_resolved_at", sa.String(length=40), nullable=True)
        )
        # "What has this person closed by hand" is a question worth being able
        # to ask.
        batch_op.create_index(
            batch_op.f("ix_pricing_cleanups_resolved_by_user_id"),
            ["resolved_by_user_id"],
            unique=False,
        )


def downgrade() -> None:
    """Drop the three columns. Nothing else was touched."""
    with op.batch_alter_table("pricing_cleanups", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pricing_cleanups_resolved_by_user_id"))
        batch_op.drop_column("manually_resolved_at")
        batch_op.drop_column("resolved_by_user_id")
        batch_op.drop_column("manual_resolution")
