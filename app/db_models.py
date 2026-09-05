import json
from datetime import UTC, datetime

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ApprovalRecord(Base):
    """One approval decision, kept after resolution so history is queryable."""

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    run_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )

    requested_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    resolved_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    tool_call_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    tool: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    arguments_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    risk: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    resolved_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    decision: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    @property
    def arguments(self) -> dict:
        return json.loads(self.arguments_json)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    run_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    actor_user_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    details_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )


class MigrationBatchRecord(Base):
    __tablename__ = "migration_batches"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    batch_id: Mapped[int] = mapped_column(
        unique=True,
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    records: Mapped[int] = mapped_column(nullable=False)

    duration_seconds: Mapped[int] = mapped_column(nullable=False)

    error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )


class RunRecord(Base):
    """One agent request, durable across restarts and approval waits."""

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    requested_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    user_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    final_answer: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    conversation_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    updated_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    @property
    def conversation(self) -> list:
        return json.loads(self.conversation_json)


class RunStepRecord(Base):
    """Execution history for replay and resumption.

    Distinct from AuditEventRecord, which is the compliance record. A step is
    what the runtime did; an audit event is what a reviewer needs to see.
    """

    __tablename__ = "run_steps"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    run_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )

    step_number: Mapped[int] = mapped_column(nullable=False)

    step_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    tool_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    arguments_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    result_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    error_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )


class UserRecord(Base):
    """A local identity. Only ever holds a bcrypt hash, never a password."""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        unique=True,
        index=True,
    )

    display_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    password_hash: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )

    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )


class ModelExecutionRecord(Base):
    """One model invocation, with measured duration and reported usage.

    A dedicated table rather than a RunStep JSON blob because these columns are
    summed and averaged per run and per day; aggregating over JSON would mean
    scanning and parsing every row.
    """

    __tablename__ = "model_executions"

    model_execution_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    run_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )

    model: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    provider_request_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )

    started_at: Mapped[str] = mapped_column(String(40), nullable=False)

    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Null means the provider did not report the figure -- never zero.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ToolExecutionRecord(Base):
    """One execution of a deterministic tool callable.

    Timing covers the callable only. A run parked waiting for a human is not
    tool latency, so an approval wait is never included here.
    """

    __tablename__ = "tool_executions"

    tool_execution_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    run_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        index=True,
    )

    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    tool_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )

    started_at: Mapped[str] = mapped_column(String(40), nullable=False)

    completed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # How many executions of this tool in this run already failed.
    retry_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    arguments_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class HistoricalReplyExampleRecord(Base):
    """One sanitized Guest -> Owner exchange, kept as a style precedent.

    Deliberately *not* a copy of a conversation. Only the two message bodies
    survive extraction, with contact details, identifiers and access codes
    stripped -- see app/historical_replies.py. There is no column here that
    could hold a booking id, a thread uid, a guest name or a raw payload,
    which is the point: the schema itself is the guarantee.

    These rows are examples, never facts. Nothing that reads them may treat a
    historical answer as current policy.
    """

    __tablename__ = "historical_reply_examples"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # Content fingerprint. Re-indexing the same exchange finds this row rather
    # than inserting a second copy.
    example_ref: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    property_slug: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        index=True,
    )

    source: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    guest_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    owner_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Deterministically derived topic tags, JSON-encoded. Used for ranking, so
    # a paraphrased question still reaches the right precedent.
    topics_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )

    # When the guest wrote, not when we indexed. Both are kept: the first says
    # how stale the precedent is, the second when we last saw it.
    created_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
        index=True,
    )

    indexed_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )

    @property
    def topics(self) -> list[str]:
        try:
            value = json.loads(self.topics_json)

        except (TypeError, ValueError):
            return []

        return value if isinstance(value, list) else []


class HospitalityKnowledgeRecord(Base):
    """One piece of Priyanka Homes operational knowledge.

    Distillation may *propose* these; only a human may approve one. That
    asymmetry is the whole point of the table: historical frequency is not
    truth, and twenty old replies saying parking is free do not make parking
    free today.

    The content is human-readable text on purpose. An owner has to be able to
    read, edit and reject a rule without a tool, and a rule nobody can read is
    a rule nobody can govern.

    `decided_at` / `decided_by_user_id` cover approval, rejection and
    supersession uniformly -- one decision, one actor, whichever way it went.
    Edits are recorded in the audit log rather than in another column pair.
    """

    __tablename__ = "hospitality_knowledge"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    knowledge_ref: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    # NULL means global -- true of every property. Scope defaults to a single
    # property precisely because widening is the dangerous direction.
    property_slug: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        index=True,
    )

    topic: Mapped[str] = mapped_column(
        String(60),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )

    source_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    # Who a rule may be said to. Guest drafting reads GUEST_FACING only; an
    # internal procedure is useful to keep and wrong to volunteer.
    audience: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="INTERNAL_OPERATION",
        index=True,
    )

    # SAFE / REVIEW_NUMERIC_FACT. A rule carrying a perishable operational
    # number is worth keeping and must not be approved without a human
    # confirming the number is still true.
    safety_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="SAFE",
        index=True,
    )

    safety_reasons_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )

    # Why the model proposed it. Kept for the reviewer, never shown to a guest.
    reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # Evidence is counts and references. The historical examples already exist;
    # copying guest content into a second table would double the exposure for
    # no gain.
    evidence_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    evidence_property_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    evidence_refs_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )

    first_observed_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    last_observed_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )

    updated_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )

    decided_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    decided_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    @property
    def evidence_refs(self) -> list[str]:
        return self.decode(self.evidence_refs_json)

    @property
    def safety_reasons(self) -> list[str]:
        return self.decode(self.safety_reasons_json)

    @staticmethod
    def decode(raw: str) -> list[str]:
        try:
            value = json.loads(raw)

        except (TypeError, ValueError):
            return []

        return value if isinstance(value, list) else []


class ConversationDraftRecord(Base):
    """The outcome of processing one state of one conversation.

    One row per (conversation, fingerprint): a prepared reply, a decision that
    no reply is needed, or a recorded failure. That key is the idempotency
    boundary for the whole proactive pipeline -- four duplicate webhook
    deliveries and a poll all resolve to the same row.

    `subject` and `message` are nullable because most outcomes carry no text: a
    NO_REPLY_NEEDED row is a decision, not a draft.

    Deliberately narrow. The conversation reference is already opaque, and there
    is no column here that could hold a booking id, a thread uid, or a guest's
    name.
    """

    __tablename__ = "conversation_drafts"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    draft_ref: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    conversation_ref: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        index=True,
    )

    property_slug: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )

    conversation_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    subject: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # DRAFT_READY / EDITED / NO_REPLY_NEEDED / NEEDS_HUMAN_REVIEW / SENT /
    # DISCARDED. STALE is never stored: it is derived by comparing this row's
    # fingerprint against the live conversation, so a row cannot be stale in the
    # database and fresh on screen.
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        index=True,
    )

    # Why drafting could not produce a reply. Operator-facing prose, never a
    # stack trace and never a provider message.
    detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    # The agent run that produced the draft, so it can be traced to its model
    # call in observability.
    source_run_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )

    updated_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )

    edited_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    sent_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )


class ConversationActivityRecord(Base):
    """The latest known activity of one conversation.

    An index, not an archive. It exists so a conversation the Inbox does not
    enumerate -- a Historic stay, reachable only because a webhook named it --
    can still be listed and ordered by recency.

    Metadata only. There is deliberately no column that could hold a message
    body, an excerpt, a guest's name, email or phone, a booking id or a thread
    uid. Guest text lives in Lodgify, transiently in model context, and in
    sanitized historical reply storage under its own rules. Never here.

    `property_name` is not stored either: it is derived from `property_slug`
    through configuration, so a rename cannot leave stale display text behind.
    `needs_attention` is not stored: it is derived from `status`, so the two
    cannot disagree.
    """

    __tablename__ = "conversation_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # One conversation is one row. The upsert depends on this being unique.
    conversation_ref: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    property_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)

    source: Mapped[str | None] = mapped_column(String(64), nullable=True)

    booking_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # The ordering signal for the whole Inbox.
    last_message_at: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )

    last_message_sender: Mapped[str | None] = mapped_column(String(32), nullable=True)

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    conversation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # A ConversationStatus value.
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    first_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)

    last_refreshed_at: Mapped[str] = mapped_column(String(32), nullable=False)


class PricingCleanupRecord(Base):
    """One temporary fixed-price override AgentGuard owes a cleanup for.

    The row is written **before** the override is sent. A write with no row is
    impossible by construction: the row is what makes the cleanup owed, and an
    override nobody recorded is exactly the stranded pin this exists to
    prevent.

    Ownership is carried in `marker`, written to the front of the provider's
    `reason` field as ``AGENTGUARD:<marker>: <text>``. PriceLabs gives an
    override no id, so the token is the only thing that ties a provider row to
    a specific record here. See docs/PRICING_CLEANUP_V2.md.
    """

    __tablename__ = "pricing_cleanups"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    listing_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    pms: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    stay_date: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        index=True,
    )

    #: What was there before, so a reviewer can see what was displaced. Null
    #: when the date carried no override.
    old_price: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    new_price: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="USD",
    )

    #: The ownership token. Equal to `id` for rows this system created; the
    #: adopted pre-V2 row has none, which is why `adopted` exists.
    marker: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    #: The exact `reason` string sent, so the confirming re-read can be
    #: compared byte-for-byte and truncation caught at write time.
    reason_sent: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    #: True only for the one override written before V2 existed. It carries no
    #: marker, so ownership falls back to price and timestamps. The exemption
    #: is stored here rather than inferred, so it cannot spread.
    adopted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    approval_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    run_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    created_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=lambda: datetime.now(UTC).isoformat(),
    )

    #: `created_at` as the provider reported it on the confirming re-read.
    provider_created_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    #: When the override must be removed. Stored, never recomputed, so a later
    #: change to the default policy cannot silently re-date overrides in flight.
    cleanup_at: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        index=True,
    )

    state: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        index=True,
    )

    resolved_at: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )

    #: Free-text account of how it ended, for a person reading the queue.
    resolution: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
