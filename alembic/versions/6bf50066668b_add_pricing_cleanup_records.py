"""add pricing cleanup records

Revision ID: 6bf50066668b
Revises: 463b2ce3fc64
Create Date: 2026-09-05 12:44:51.950927

Autogenerate also proposed dropping the server defaults on
`hospitality_knowledge.audience`, `.safety_status` and `.safety_reasons_json`.
Those defaults exist in the database but not in the model, so it read the drift
as something to remove. They are unrelated to this change and removing them
would alter how an existing feature inserts rows, so they are deliberately not
included here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6bf50066668b"
down_revision: str | Sequence[str] | None = "463b2ce3fc64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pricing_cleanups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("listing_id", sa.String(length=64), nullable=False),
        sa.Column("pms", sa.String(length=32), nullable=False),
        sa.Column("stay_date", sa.String(length=10), nullable=False),
        sa.Column("old_price", sa.Float(), nullable=True),
        sa.Column("new_price", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("marker", sa.String(length=36), nullable=True),
        sa.Column("reason_sent", sa.Text(), nullable=True),
        sa.Column("adopted", sa.Boolean(), nullable=False),
        sa.Column("approval_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("provider_created_at", sa.String(length=40), nullable=True),
        sa.Column("cleanup_at", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("resolved_at", sa.String(length=40), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("pricing_cleanups", schema=None) as batch_op:
        for column in (
            "approval_id",
            "cleanup_at",
            "listing_id",
            "marker",
            "run_id",
            "state",
            "stay_date",
        ):
            batch_op.create_index(
                batch_op.f(f"ix_pricing_cleanups_{column}"),
                [column],
                unique=False,
            )


def downgrade() -> None:
    with op.batch_alter_table("pricing_cleanups", schema=None) as batch_op:
        for column in (
            "stay_date",
            "state",
            "run_id",
            "marker",
            "listing_id",
            "cleanup_at",
            "approval_id",
        ):
            batch_op.drop_index(batch_op.f(f"ix_pricing_cleanups_{column}"))

    op.drop_table("pricing_cleanups")
