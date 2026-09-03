"""Add observability metrics tables.

Adds model_executions and tool_executions: measured durations, reported token
usage and estimated cost, as queryable columns rather than JSON, because the
console aggregates them per run and per day.

Every token and cost column is nullable on purpose. A figure the provider did
not report, or a model with no configured price, stays NULL -- it must never
become a zero, which would read as a true measurement of nothing.

Creates structure only; no table is altered and no data is seeded.

Original revision id: add observability metrics

Revision ID: 76301e651eea
Revises: baa1819ad1d6
Create Date: 2026-09-02 22:21:02.435229

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "76301e651eea"
down_revision: str | Sequence[str] | None = "baa1819ad1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the two metric tables."""
    op.create_table(
        "model_executions",
        sa.Column("model_execution_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("provider_request_id", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("error_type", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("model_execution_id"),
    )
    with op.batch_alter_table("model_executions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_model_executions_model"), ["model"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_model_executions_provider"), ["provider"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_model_executions_run_id"), ["run_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_model_executions_status"), ["status"], unique=False
        )

    op.create_table(
        "tool_executions",
        sa.Column("tool_execution_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=100), nullable=True),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("retry_number", sa.Integer(), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("tool_execution_id"),
    )
    with op.batch_alter_table("tool_executions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_tool_executions_run_id"), ["run_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_tool_executions_status"), ["status"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_tool_executions_tool_name"), ["tool_name"], unique=False
        )


def downgrade() -> None:
    """Drop the two metric tables."""
    with op.batch_alter_table("tool_executions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tool_executions_tool_name"))
        batch_op.drop_index(batch_op.f("ix_tool_executions_status"))
        batch_op.drop_index(batch_op.f("ix_tool_executions_run_id"))

    op.drop_table("tool_executions")
    with op.batch_alter_table("model_executions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_model_executions_status"))
        batch_op.drop_index(batch_op.f("ix_model_executions_run_id"))
        batch_op.drop_index(batch_op.f("ix_model_executions_provider"))
        batch_op.drop_index(batch_op.f("ix_model_executions_model"))

    op.drop_table("model_executions")
