"""add pricing action outcomes

Revision ID: 9369a39e0b7d
Revises: 18d8b2b8c18d
Create Date: 2026-09-06 20:20:20.155480

Purely additive: one new table, no change to any existing one.

`--autogenerate` also proposed dropping three `server_default`s on
`hospitality_knowledge` (`audience`, `safety_status`, `safety_reasons_json`).
That is pre-existing drift between those models and the database, entirely
unrelated to outcome tracking, and it has been **removed by hand** -- shipping
it inside this revision would silently change the behaviour of a different
feature under a migration named for this one. If that drift should be
reconciled, it belongs in its own revision.

No data migration. Past pricing actions are not backfilled: their
decision-time evidence was never captured and cannot be recovered, and rows
carrying null evidence would be permanently unanalysable. The two live proofs
are recorded in docs/PRICING_LIVE_PROOFS.md instead.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9369a39e0b7d'
down_revision: str | Sequence[str] | None = '18d8b2b8c18d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `pricing_action_outcomes`. Additive only."""
    op.create_table(
        'pricing_action_outcomes',
        sa.Column('id', sa.String(length=36), nullable=False),
        # -- linkage
        sa.Column('approval_id', sa.String(length=36), nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('cleanup_id', sa.String(length=36), nullable=True),
        sa.Column('listing_id', sa.String(length=64), nullable=False),
        sa.Column('stay_date', sa.String(length=10), nullable=False),
        # -- what we did
        sa.Column('action', sa.String(length=16), nullable=False),
        sa.Column('executed_at', sa.String(length=40), nullable=False),
        sa.Column('write_outcome', sa.String(length=32), nullable=False),
        sa.Column('price_before', sa.Float(), nullable=True),
        sa.Column('price_after', sa.Float(), nullable=True),
        sa.Column('currency', sa.String(length=8), nullable=True),
        sa.Column('days_out', sa.Integer(), nullable=True),
        # -- decision-time evidence. Nullable: an unknown is stored as one.
        sa.Column('history_adr', sa.Float(), nullable=True),
        sa.Column('history_sample_count', sa.Integer(), nullable=True),
        sa.Column('market_p25', sa.Float(), nullable=True),
        sa.Column('market_booked_median', sa.Float(), nullable=True),
        sa.Column('demand', sa.String(length=64), nullable=True),
        sa.Column('listing_occupancy', sa.Float(), nullable=True),
        sa.Column('market_occupancy', sa.Float(), nullable=True),
        sa.Column('market_signal_conflict', sa.Boolean(), nullable=True),
        sa.Column('hard_floor', sa.Float(), nullable=True),
        sa.Column('owner_floor', sa.Float(), nullable=True),
        sa.Column('observed_commission_rate', sa.Float(), nullable=True),
        # -- first post-action booking. Write-once, enforced by the store.
        sa.Column('first_booked_at', sa.String(length=40), nullable=True),
        sa.Column('first_booking_lead_days', sa.Integer(), nullable=True),
        sa.Column('first_hours_from_action', sa.Float(), nullable=True),
        sa.Column('first_realized_stay_adr', sa.Float(), nullable=True),
        sa.Column('first_reservation_id', sa.String(length=64), nullable=True),
        sa.Column('first_booking_channel', sa.String(length=32), nullable=True),
        # -- current truth, overwritten each pass
        sa.Column('current_booking_status', sa.String(length=16), nullable=True),
        sa.Column('current_reservation_id', sa.String(length=64), nullable=True),
        sa.Column('current_realized_stay_adr', sa.Float(), nullable=True),
        sa.Column('current_booking_channel', sa.String(length=32), nullable=True),
        sa.Column('cancelled_after_booking', sa.Boolean(), nullable=True),
        # -- reconciliation bookkeeping
        sa.Column('last_reconciled_at', sa.String(length=40), nullable=True),
        sa.Column(
            'reconcile_pass_count',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'cancellation_first_observed_at',
            sa.String(length=40),
            nullable=True,
        ),
        sa.Column('finalized_at', sa.String(length=40), nullable=True),
        sa.Column('reopened_at', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    with op.batch_alter_table('pricing_action_outcomes', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_pricing_action_outcomes_approval_id'),
            ['approval_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_pricing_action_outcomes_executed_at'),
            ['executed_at'],
            unique=False,
        )
        # The reconciler selects on these two: not-yet-finalized rows, ordered
        # by how stale they are.
        batch_op.create_index(
            batch_op.f('ix_pricing_action_outcomes_finalized_at'),
            ['finalized_at'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_pricing_action_outcomes_last_reconciled_at'),
            ['last_reconciled_at'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_pricing_action_outcomes_listing_id'),
            ['listing_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_pricing_action_outcomes_run_id'),
            ['run_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_pricing_action_outcomes_stay_date'),
            ['stay_date'],
            unique=False,
        )


def downgrade() -> None:
    """Drop the table. Nothing else is touched, so nothing else is restored."""
    with op.batch_alter_table('pricing_action_outcomes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pricing_action_outcomes_stay_date'))
        batch_op.drop_index(batch_op.f('ix_pricing_action_outcomes_run_id'))
        batch_op.drop_index(batch_op.f('ix_pricing_action_outcomes_listing_id'))
        batch_op.drop_index(
            batch_op.f('ix_pricing_action_outcomes_last_reconciled_at')
        )
        batch_op.drop_index(
            batch_op.f('ix_pricing_action_outcomes_finalized_at')
        )
        batch_op.drop_index(batch_op.f('ix_pricing_action_outcomes_executed_at'))
        batch_op.drop_index(batch_op.f('ix_pricing_action_outcomes_approval_id'))

    op.drop_table('pricing_action_outcomes')
