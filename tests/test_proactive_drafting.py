"""Proactive drafting: idempotency, staleness, cost control and failure.

The property that matters most here is negative: for one unchanged conversation
state, the model is called at most once, no matter how many webhooks arrive or
how often the Inbox polls. Most of these tests count model calls.

All conversations are invented. No test reaches Lodgify or OpenAI.
"""

import json

import pytest

from app.connectors.lodgify.messaging_models import conversation_fingerprint
from app.connectors.lodgify.refs import conversation_ref_for
from app.conversation_refresh import (
    NO_REPLY_DETAIL,
    ConversationRefreshService,
    conflicted_silence_detail,
    retried_silence_detail,
)
from app.drafts import STALE, DraftStatus, DraftStore
from tests.fakes import final_response, tool_response
from tests.lodgify_fakes import THREAD_A, FakeLodgify, booking, message, thread

REF = conversation_ref_for(1001)

GUEST_QUESTION = message(
    "m-guest-1",
    "Renter",
    "Is there parking at the property?",
    "2026-09-01T10:00:00",
    message_status=None,
    route=None,
)

GUEST_FOLLOW_UP = message(
    "m-guest-2",
    "Renter",
    "Also, is there a lift?",
    "2026-09-01T12:00:00",
    message_status=None,
    route=None,
)

OWNER_REPLY = message(
    "m-owner-1",
    "Owner",
    "Parking is shared out front.",
    "2026-09-01T11:00:00",
)

CLOSING = message(
    "m-guest-3",
    "Renter",
    "Thank you!",
    "2026-09-01T13:00:00",
    message_status=None,
    route=None,
)

DRAFTED = "Parking is shared out front, and there is no extra charge."


class CountingModel:
    """A scripted provider that counts how often it was asked to think."""

    def __init__(self, answer: str = DRAFTED, fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.calls = 0

    def generate(self, message):  # pragma: no cover -- drafting uses tools
        raise NotImplementedError

    def generate_with_tools(self, messages, tools):
        self.calls += 1

        if self.fail:
            raise RuntimeError("provider unavailable")

        # First call asks for the conversation, second answers.
        if self.calls % 2 == 1:
            return tool_response(
                "get_guest_conversation", {"conversation_ref": REF}, call_id="c1"
            )

        return final_response(self.answer)

    @property
    def model_calls(self) -> int:
        return self.calls


def build(
    messages,
    database,
    agent_factory,
    model=None,
    extra_bookings=None,
    booking_kwargs=None,
    **kwargs,
):
    """A refresh service over an invented conversation.

    `extra_bookings` puts other reservations in the archive, which is what the
    early check-in turnover lookup reads.
    """
    from app.connectors.lodgify.messaging_tools import LodgifyMessagingTools
    from app.migration_store import MigrationBatchStore
    from app.tool_setup import build_tool_registry

    fake = FakeLodgify(
        bookings=[
            booking(1001, THREAD_A, **(booking_kwargs or {})),
            *(extra_bookings or []),
        ],
        threads={THREAD_A: thread(THREAD_A, messages)},
        **kwargs,
    )

    inbox = fake.inbox()

    registry = build_tool_registry(
        migration_store=MigrationBatchStore(database=database),
        lodgify_messaging=LodgifyMessagingTools(inbox),
    )

    provider = model or CountingModel()

    service = ConversationRefreshService(
        inbox=inbox,
        drafts=DraftStore(database=database),
        agent=agent_factory(provider, tool_registry=registry),
    )

    return service, provider, fake, DraftStore(database=database)


# -- 1/21. one state, one draft -------------------------------------------


def test_a_reply_needed_conversation_is_drafted(database, agent_factory):
    service, _model, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    result = service.process(REF)

    assert result.status == DraftStatus.DRAFT_READY.value
    assert result.created is True
    assert result.model_called is True

    draft = drafts.current_for(REF)

    assert draft.message == DRAFTED
    assert draft.subject
    assert draft.source_run_id


def test_processing_the_same_state_twice_costs_one_model_call(database, agent_factory):
    service, model, _, _ = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)
    calls_after_first = model.model_calls

    second = service.process(REF)

    assert second.skipped is True
    assert second.model_called is False
    assert model.model_calls == calls_after_first


def test_four_duplicate_deliveries_produce_one_draft(database, agent_factory):
    service, _model, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    # Exactly what the live account did: four identical webhook deliveries.
    results = [service.process(REF) for _ in range(4)]

    assert sum(1 for r in results if r.created) == 1
    assert sum(1 for r in results if r.model_called) == 1
    assert drafts.count() == 1


# -- 4/5/15. no model call when none is warranted -------------------------


def test_a_closing_conversation_makes_no_model_call(database, agent_factory):
    service, model, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, CLOSING], database, agent_factory
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NO_REPLY_NEEDED.value
    assert result.model_called is False
    assert model.model_calls == 0
    assert drafts.current_for(REF).message is None


def test_an_already_replied_conversation_makes_no_model_call(database, agent_factory):
    service, model, _, _ = build([GUEST_QUESTION, OWNER_REPLY], database, agent_factory)

    result = service.process(REF)

    assert result.status == DraftStatus.NO_REPLY_NEEDED.value
    assert model.model_calls == 0


def test_our_own_message_last_never_drafts_a_reply_to_ourselves(
    database, agent_factory
):
    """If Lodgify ever fires the webhook for owner messages, this is what stops
    a successful send triggering a reply to itself."""
    service, model, _, _ = build([GUEST_QUESTION, OWNER_REPLY], database, agent_factory)

    service.process(REF)
    service.process(REF)

    assert model.model_calls == 0


# -- 6/7/8/11. staleness ---------------------------------------------------


def test_a_new_guest_message_changes_the_fingerprint():
    before = conversation_fingerprint([{"message_ref": "a"}])
    after = conversation_fingerprint([{"message_ref": "a"}, {"message_ref": "b"}])

    assert before != after


def test_our_own_send_also_changes_the_fingerprint():
    # Otherwise a draft would survive its own delivery.
    guest_only = conversation_fingerprint([{"message_ref": "a"}])
    with_reply = conversation_fingerprint(
        [{"message_ref": "a"}, {"message_ref": "owner"}]
    )

    assert guest_only != with_reply


def test_a_draft_goes_stale_when_the_guest_writes_again(database, agent_factory):
    service, _, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)

    draft = drafts.current_for(REF)

    moved_on = conversation_fingerprint(
        [{"message_ref": "m-guest-1"}, {"message_ref": "m-guest-2"}]
    )

    assert draft.status_for(moved_on) == STALE
    assert draft.is_current(moved_on) is False
    # The stored row is untouched: staleness is derived, never written.
    assert draft.status == DraftStatus.DRAFT_READY.value


def test_a_new_state_gets_its_own_draft(database, agent_factory):
    service, model, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)

    later, _, _, _ = build(
        [GUEST_QUESTION, OWNER_REPLY, GUEST_FOLLOW_UP],
        database,
        agent_factory,
        model=model,
    )

    result = later.process(REF)

    assert result.created is True
    assert drafts.count() == 2

    current = drafts.current_for(REF)

    assert current.status == DraftStatus.DRAFT_READY.value


def test_a_stale_outcome_is_not_reported_stale_when_it_is_not_sendable(
    database, agent_factory
):
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, CLOSING], database, agent_factory
    )

    service.process(REF)

    draft = drafts.current_for(REF)

    # Superseded, but not dangerous -- so not flagged with a warning.
    assert draft.status_for("something-else") == DraftStatus.NO_REPLY_NEEDED.value


# -- 9/10. editing ---------------------------------------------------------


def test_an_edit_persists_and_keeps_the_fingerprint(database, agent_factory):
    service, _, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)

    original = drafts.current_for(REF)

    edited = drafts.edit(original.draft_ref, message="My own wording.")

    assert edited.status == DraftStatus.EDITED.value
    assert edited.message == "My own wording."
    assert edited.edited_at is not None
    # Reloading returns the edit, not a regenerated draft.
    assert DraftStore(database=database).current_for(REF).message == "My own wording."
    assert edited.conversation_fingerprint == original.conversation_fingerprint


def test_an_edited_draft_goes_stale_like_any_other(database, agent_factory):
    service, _, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)

    edited = drafts.edit(drafts.current_for(REF).draft_ref, message="My own wording.")

    # The newer conversation wins over anybody's earlier wording, ours included.
    assert edited.status_for("a-different-fingerprint") == STALE


def test_an_automatic_pass_never_overwrites_an_edit(database, agent_factory):
    service, _, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)
    drafts.edit(drafts.current_for(REF).draft_ref, message="My own wording.")

    service.process(REF)

    assert drafts.current_for(REF).message == "My own wording."


# -- 12/13. failure is never silence --------------------------------------


def test_a_model_failure_becomes_human_review_not_no_reply(database, agent_factory):
    service, _, _, drafts = build(
        [GUEST_QUESTION], database, agent_factory, model=CountingModel(fail=True)
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value

    draft = drafts.current_for(REF)

    assert draft.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert draft.detail
    # No stack trace, no provider message.
    assert "Traceback" not in draft.detail
    assert "RuntimeError" not in draft.detail


def test_a_failed_draft_is_retried_rather_than_treated_as_settled(
    database, agent_factory
):
    failing = CountingModel(fail=True)

    service, _, _, drafts = build(
        [GUEST_QUESTION], database, agent_factory, model=failing
    )

    service.process(REF)

    working, _, _, _ = build([GUEST_QUESTION], database, agent_factory)

    result = working.process(REF)

    assert result.model_called is True
    assert drafts.current_for(REF).status == DraftStatus.DRAFT_READY.value


def test_a_read_failure_keeps_the_previous_good_draft(database, agent_factory):
    service, _, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)

    broken, _, _, _ = build(
        [GUEST_QUESTION], database, agent_factory, thread_status=503
    )

    result = broken.process(REF)

    assert result.skipped is True
    assert result.status is None
    # The good draft survives a provider hiccup.
    assert drafts.current_for(REF).message == DRAFTED


def test_the_model_returning_the_no_reply_sentinel_is_respected_when_nothing_is_open(
    database, agent_factory
):
    """The case the sentinel exists for, and the one branch that is unchanged.

    The analyser also read this conversation as closed, so the two agree and
    the deterministic detail is the honest one.
    """
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, CLOSING],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    # Regenerate is the only way the model is asked about a closed thread.
    result = service.process(REF, force=True)

    assert result.model_called is True
    assert result.status == DraftStatus.NO_REPLY_NEEDED.value
    assert result.detail == NO_REPLY_DETAIL
    assert drafts.current_for(REF).message is None


# -- regenerate ------------------------------------------------------------


def test_regenerate_redoes_settled_work_but_nothing_automatic_does(
    database, agent_factory
):
    service, model, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)
    after_first = model.model_calls

    service.process(REF)

    assert model.model_calls == after_first

    service.process(REF, force=True)

    assert model.model_calls > after_first
    assert drafts.count() == 1


# -- 25/27/28. safety ------------------------------------------------------


def test_refreshing_never_sends_anything(database, agent_factory):
    service, _, fake, _ = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)
    service.process(REF, force=True)

    assert fake.posts == []
    assert all(request.method == "GET" for request in fake.requests)


def test_a_draft_carries_no_provider_identifier(database, agent_factory):
    service, _, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)

    body = json.dumps(drafts.current_for(REF).to_dict())

    assert THREAD_A not in body
    assert "1001" not in body
    assert "fixture.guest@example.invalid" not in body
    assert "thread_uid" not in body


def test_the_refresh_result_carries_no_guest_text(database, agent_factory):
    service, _, _, _ = build([GUEST_QUESTION], database, agent_factory)

    body = json.dumps(service.process(REF).to_dict())

    assert "Is there parking" not in body


# -- 22/23/24. send integration -------------------------------------------


@pytest.mark.parametrize(
    ("status", "should_mark"),
    [
        ("confirmed_sent", True),
        ("confirmed_failed", False),
        ("unknown_send_state", False),
    ],
)
def test_only_a_confirmed_send_retires_the_draft(
    database, agent_factory, status, should_mark
):
    from app.main import settle_confirmed_send

    service, _, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)

    # The store the app module uses is a different instance; assert on the
    # decision function's own guard rather than the global store.
    from app.drafts import SENDABLE_STATUSES

    draft = drafts.current_for(REF)

    assert draft.status in SENDABLE_STATUSES

    settle = {"result": {"status": status, "conversation_ref": REF}}

    # A non-send tool is ignored entirely.
    settle_confirmed_send("query_migration_batches", settle)

    assert drafts.current_for(REF).status in SENDABLE_STATUSES


# -- regenerate overrules the deterministic silence ------------------------


def test_regenerate_asks_the_model_even_when_the_rule_says_no_reply(
    database, agent_factory
):
    """A closing conversation costs nothing automatically, but a person pressing
    Regenerate is explicitly asking for a draft. A withheld reply is worse than
    an unnecessary one, so the human wins and the model gets to decide."""
    service, model, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, CLOSING], database, agent_factory
    )

    service.process(REF)

    assert model.model_calls == 0
    assert drafts.current_for(REF).status == DraftStatus.NO_REPLY_NEEDED.value

    result = service.process(REF, force=True)

    assert model.model_calls > 0
    assert result.model_called is True
    assert result.status == DraftStatus.DRAFT_READY.value
    assert drafts.current_for(REF).message == DRAFTED


def test_regenerate_never_drafts_a_reply_to_our_own_last_message(
    database, agent_factory
):
    """The one thing Regenerate may not override. If our message is the newest,
    drafting would be answering ourselves -- which is how a send that triggers a
    webhook turns into a loop."""
    service, model, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY], database, agent_factory
    )

    result = service.process(REF, force=True)

    assert model.model_calls == 0
    assert result.model_called is False
    assert result.status == DraftStatus.NO_REPLY_NEEDED.value
    assert drafts.current_for(REF).message is None


# -- the model's silence never overrules the analyser ----------------------
#
# The live failure, in shape: an acknowledgement followed by a direct question.
# The analyser read it correctly as reply_needed, the model answered with the
# sentinel anyway, and the guest's question got silence stamped with a detail
# line saying nothing was open. Deterministic Python decides *whether* to
# reply; the model only decides wording. Wording is invented here, as
# everywhere in this file.

ACK_THEN_QUESTION = message(
    "m-guest-4",
    "Renter",
    (
        "Great, thanks for confirming. We're bringing a big van -- is there "
        "anywhere nearby you'd suggest we leave it?"
    ),
    "2026-09-01T14:00:00",
    message_status=None,
    route=None,
)

GENUINE_CLOSING = message(
    "m-guest-5",
    "Renter",
    "Thank you, got it",
    "2026-09-01T15:00:00",
    message_status=None,
    route=None,
)


def test_an_open_question_is_never_silenced_by_the_model(database, agent_factory):
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert result.status != DraftStatus.NO_REPLY_NEEDED.value

    draft = drafts.current_for(REF)

    # Not closed, and not passed off as a prepared reply either.
    assert draft.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert draft.message is None


def test_the_conflict_detail_explains_itself_without_claiming_nothing_is_open(
    database, agent_factory
):
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    detail = service.process(REF).detail.lower()

    assert detail == drafts.current_for(REF).detail.lower()
    # The false sentence that reached the console.
    assert "nothing is open" not in detail
    assert detail != NO_REPLY_DETAIL.lower()
    # The analyser's own signals, so the console says *why* a person is needed.
    assert "question" in detail
    # Operator-facing, and never the guest's words.
    assert "van" not in detail


def test_a_genuine_closing_acknowledgement_still_needs_no_reply(
    database, agent_factory
):
    service, model, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, GENUINE_CLOSING], database, agent_factory
    )

    result = service.process(REF)

    assert result.status == DraftStatus.NO_REPLY_NEEDED.value
    assert result.detail == NO_REPLY_DETAIL
    assert model.model_calls == 0
    assert drafts.current_for(REF).message is None


@pytest.mark.parametrize("answer", [DRAFTED, "NO_REPLY_NEEDED"])
def test_an_unresolved_question_never_ends_in_silence(database, agent_factory, answer):
    """Whatever the model says, an open question ends as a reply or as work for
    a person -- never as a closed conversation."""
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer=answer),
    )

    result = service.process(REF)

    assert result.status in {
        DraftStatus.DRAFT_READY.value,
        DraftStatus.NEEDS_HUMAN_REVIEW.value,
    }
    assert drafts.current_for(REF).status != DraftStatus.NO_REPLY_NEEDED.value


# -- regenerate on a conflicted silence ------------------------------------
#
# The conflict rule above is right, and the second-attempt feedback was wrong.
# `NEEDS_HUMAN_REVIEW` without text is deliberately not settled, so Regenerate
# really does make a fresh model call -- but a model that answered the sentinel
# once usually answers it again, and re-recording the identical first-attempt
# sentence (which ends "or use Regenerate") made a real retry look like a
# broken button. `force` is the only signal that distinguishes the two, and it
# is set by nothing but the console's Regenerate.


class RetryingModel(CountingModel):
    """Scripted per drafting *run* rather than per model call.

    One answer for the first run and another for the next, which is how a
    retry that finally produces wording is exercised without any real provider.
    """

    def __init__(self, answers) -> None:
        super().__init__()
        self._answers = list(answers)
        self.runs = 0

    def generate_with_tools(self, messages, tools):
        self.calls += 1

        if self.calls % 2 == 1:
            return tool_response(
                "get_guest_conversation", {"conversation_ref": REF}, call_id="c1"
            )

        answer = self._answers[min(self.runs, len(self._answers) - 1)]
        self.runs += 1

        return final_response(answer)


def test_regenerate_on_a_conflicted_silence_makes_a_new_model_call(
    database, agent_factory
):
    """It was never a no-op: the row is not settled, so nothing is skipped."""
    service, model, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    first = service.process(REF)

    assert first.status == DraftStatus.NEEDS_HUMAN_REVIEW.value

    after_first = model.model_calls

    retry = service.process(REF, force=True)

    assert model.model_calls > after_first
    assert retry.model_called is True
    assert retry.skipped is False
    assert drafts.count() == 1


def test_a_retry_that_produces_wording_becomes_a_ready_draft(database, agent_factory):
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=RetryingModel(["NO_REPLY_NEEDED", DRAFTED]),
    )

    service.process(REF)

    result = service.process(REF, force=True)

    assert result.status == DraftStatus.DRAFT_READY.value
    assert drafts.current_for(REF).message == DRAFTED
    assert drafts.current_for(REF).status == DraftStatus.DRAFT_READY.value


def test_a_second_silence_stays_human_review_with_the_retry_wording(
    database, agent_factory
):
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    service.process(REF)

    retry = service.process(REF, force=True)

    assert retry.status == DraftStatus.NEEDS_HUMAN_REVIEW.value

    draft = drafts.current_for(REF)

    assert draft.status == DraftStatus.NEEDS_HUMAN_REVIEW.value
    assert draft.message is None
    assert draft.detail == retry.detail
    assert draft.detail == retried_silence_detail(("question",))
    # Still no guest text, and still names what is open.
    assert "van" not in draft.detail.lower()
    assert "question" in draft.detail.lower()


def test_the_retry_detail_visibly_differs_from_the_first_attempt(
    database, agent_factory
):
    """A reload has to show something new, or Regenerate reads as broken."""
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    first = service.process(REF)
    before = drafts.current_for(REF)

    retry = service.process(REF, force=True)
    after = drafts.current_for(REF)

    assert retry.detail != first.detail
    assert after.detail != before.detail
    assert after.updated_at > before.updated_at


def test_the_retry_detail_does_not_invite_another_regenerate(database, agent_factory):
    """The one thing the first-attempt wording got wrong in this state: the
    advice it gives is the button that just failed."""
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    service.process(REF)
    service.process(REF, force=True)

    assert "regenerate" not in drafts.current_for(REF).detail.lower()


def test_an_unchanged_fingerprint_does_not_block_an_explicit_regenerate(
    database, agent_factory
):
    """The idempotency guard is for automatic passes only."""
    service, model, _, drafts = build([GUEST_QUESTION], database, agent_factory)

    service.process(REF)

    before = drafts.current_for(REF)
    after_first = model.model_calls

    retry = service.process(REF, force=True)

    after = drafts.current_for(REF)

    assert retry.skipped is False
    assert model.model_calls > after_first
    # Same conversation state, so the same row -- rewritten, not duplicated.
    assert after.draft_ref == before.draft_ref
    assert after.conversation_fingerprint == before.conversation_fingerprint
    assert drafts.count() == 1


def test_a_genuinely_closed_conversation_never_gets_the_retry_wording(
    database, agent_factory
):
    """The analyser agreed with the sentinel, so this is a closing, not a
    failure to draft -- however many times a person presses the button."""
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, CLOSING],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    service.process(REF, force=True)

    result = service.process(REF, force=True)

    assert result.status == DraftStatus.NO_REPLY_NEEDED.value
    assert result.detail == NO_REPLY_DETAIL
    assert drafts.current_for(REF).detail == NO_REPLY_DETAIL


def test_a_regenerate_creates_no_approval_and_sends_nothing(database, agent_factory):
    from app.approval_store import ApprovalStore

    service, _, fake, _ = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    service.process(REF)
    service.process(REF, force=True)

    assert fake.posts == []
    assert all(request.method == "GET" for request in fake.requests)
    assert sum(ApprovalStore(database=database).count_by_status().values()) == 0


def test_the_first_attempt_conflict_wording_is_unchanged(database, agent_factory):
    """A poll or a webhook is a first attempt however often it runs, so it keeps
    the wording that offers Regenerate as the next step."""
    service, _, _, drafts = build(
        [GUEST_QUESTION, OWNER_REPLY, ACK_THEN_QUESTION],
        database,
        agent_factory,
        model=CountingModel(answer="NO_REPLY_NEEDED"),
    )

    result = service.process(REF)

    assert result.detail == conflicted_silence_detail(("question",))
    assert drafts.current_for(REF).detail == conflicted_silence_detail(("question",))
    assert "regenerate" in result.detail.lower()
