"""add audit provenance

Revision ID: 239943ef47d4
Revises: 9369a39e0b7d
Create Date: 2026-09-07 01:40:00.000000

Three nullable columns on `audit_events`, and nothing else.

They identify a **process and a request, never a person**: no IP address, no
user-agent, no device fingerprint. They exist because on 2026-09-07 five
pricing approvals were created and approved in ninety seconds and the audit
trail could not say where they came from -- every event carried the same
shared demo identity, and the answer had to be recovered from a uvicorn access
log that happened to still exist.

Nullable on purpose. Every event already in the table predates this and
legitimately carries null; so does any event written outside an HTTP request,
such as a scheduled job. Backfilling a value we do not know would be worse
than an honest null.

`--autogenerate` also proposed dropping `server_default`s on
`hospitality_knowledge` (pre-existing drift, unrelated) and on
`pricing_action_outcomes.reconcile_pass_count` (drift introduced by the
previous revision). Both were **removed by hand** -- an unrelated change must
not ride along inside a revision named for something else. The
`reconcile_pass_count` case was fixed at its source instead, by declaring the
same `server_default` on the model, so the two now agree.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "239943ef47d4"
down_revision: str | Sequence[str] | None = "9369a39e0b7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the three provenance columns. Additive, nullable, indexed."""
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("request_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("actor_source", sa.String(length=8), nullable=True)
        )
        batch_op.add_column(
            sa.Column("instance_id", sa.String(length=36), nullable=True)
        )
        # Indexed because the questions asked of them are "what else happened
        # in this request" and "what did that process do" -- both lookups.
        batch_op.create_index(
            batch_op.f("ix_audit_events_request_id"),
            ["request_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_audit_events_actor_source"),
            ["actor_source"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_audit_events_instance_id"),
            ["instance_id"],
            unique=False,
        )


def downgrade() -> None:
    """Drop the three columns. Nothing else was touched, so nothing else returns."""
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_events_instance_id"))
        batch_op.drop_index(batch_op.f("ix_audit_events_actor_source"))
        batch_op.drop_index(batch_op.f("ix_audit_events_request_id"))
        batch_op.drop_column("instance_id")
        batch_op.drop_column("actor_source")
        batch_op.drop_column("request_id")
