"""The outcome reconciliation pass, as a process a scheduler can invoke.

    0 6 * * *  cd /path/to/enterprise-agentops && uv run python -m app.pricing_reconcile_job

Daily. The cadence matters for exactly one thing: PriceLabs gives no usable
cancellation timestamp (`cancelled_on` is the Unix epoch sentinel on 445 of
450 cancelled reservations observed 2026-09-06), so repeated observation is
the only way a cancellation is ever detected, and the gap between passes is
the entire temporal resolution we have on it. First-booking precision is
unaffected -- `booked_date` carries a full timestamp, so a booking six hours
after an action is recorded as 6.0 hours even under daily polling.

**Why a process and not a thread**, and **why it imports `app.main`**: the
same reasons as `app.pricing_cleanup_job`. One reconciler in the system, wired
from the same store and the same reader, observable and killable, never
silently skipped by a deployment that runs the API differently.

**This job cannot change a price.** `PricingOutcomeReconciler` is constructed
with a reader and has no writer -- not an unused one, an absent one. There is
no code path from here to a pricing endpoint.
"""

import json
import sys


def main(argv: list[str] | None = None) -> int:
    """Run one pass. Returns 0 when the pass completed, 1 when it could not.

    A non-zero exit means the pass did not run. Individual rows left unknown
    because the provider could not be read are a normal, successful pass --
    they are reported, not raised, and the next pass tries again.
    """
    from app.main import pricelabs_outcome_reconciler
    from app.pricing_reconciler import summarise

    if pricelabs_outcome_reconciler is None:
        print(
            "PriceLabs is not configured; no outcome reconciler exists.",
            file=sys.stderr,
        )

        return 1

    outcomes = pricelabs_outcome_reconciler.run_once()

    print(json.dumps(summarise(outcomes), indent=2))

    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
