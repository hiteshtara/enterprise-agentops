"""The one enquiry capability the governance layer can execute.

Exactly one tool, and it is not model-callable. `send_enquiry_reply` is
registered with `ToolRisk.DANGEROUS` and `model_callable=False`, so:

  * the model is never told it exists -- `ToolRegistry.definitions()` omits it,
    and a name the model invents is rejected as an unknown tool;
  * the console still sees it in `ToolRegistry.describe()`, because an operator
    reviewing what this deployment can do must see every capability; and
  * `ToolRegistry.execute()` still refuses to run it without a recorded human
    approval, exactly as it would for a model-initiated call.

The division of labour is the point. The model writes words -- that is what
`app/enquiry_replies.py` asks it for, with no tool involved. Whether those
words are ever transmitted is decided by a person, and carried out by Python.
Giving the model the send tool as well would let a drafting run reach a
stranger without anyone having asked it to.

There is deliberately no enquiry *read* tool either. A tool that let a model
name an enquiry would let it name any enquiry; the application chooses the
thread and supplies it.
"""

from typing import Any

from app.connectors.lodgify.enquiries import LodgifyEnquirySender
from app.connectors.lodgify.errors import LodgifyConfigurationError
from app.connectors.lodgify.inbox import MAX_MESSAGE_LENGTH, MAX_SUBJECT_LENGTH
from app.connectors.lodgify.models import unknown


class LodgifyEnquiryTools:
    """Adapter between the tool registry and the enquiry sender."""

    def __init__(self, sender: LodgifyEnquirySender) -> None:
        self._sender = sender

    def send_enquiry_reply(
        self,
        enquiry_ref: str,
        subject: str,
        message: str,
    ) -> dict[str, Any]:
        """Send one enquiry reply. Reached only through an approved call.

        A missing credential is returned rather than raised, for the same
        reason the booked-guest send does it: the three send outcomes are the
        contract, and a configuration failure that became a generic tool error
        would be indistinguishable from a send that might have happened.
        """
        try:
            return self._sender.send_reply(
                enquiry_ref=enquiry_ref,
                subject=subject,
                message=message,
            )

        except LodgifyConfigurationError:
            return unknown(
                "not_configured",
                "The Lodgify connector is not configured, so nothing was sent.",
            )


# -- tool schema -----------------------------------------------------------

# Exactly three fields, `additionalProperties: false`, and no provider
# identifier among them. The model is never shown this schema, but the shape is
# still the contract the route fills in and the approval card renders -- and
# `type` / `send_notification` are pinned in the transport, so no caller of any
# kind can decide who a message appears to come from or whether anyone is told
# about it.
SEND_ENQUIRY_REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enquiry_ref": {
            "type": "string",
            "description": (
                "The enquiry to reply to, exactly as returned by the enquiries "
                "listing. Opaque -- never construct or guess one."
            ),
        },
        "subject": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_SUBJECT_LENGTH,
            "description": (
                "Short single-line subject for the message. Plain text only."
            ),
        },
        "message": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_MESSAGE_LENGTH,
            "description": (
                "The exact text the enquirer will receive. Plain text only, no "
                "HTML. This is sent verbatim once approved."
            ),
        },
    },
    "required": ["enquiry_ref", "subject", "message"],
    "additionalProperties": False,
}
