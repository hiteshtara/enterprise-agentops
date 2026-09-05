"""The hourly cleanup pass, as a process a scheduler can invoke.

    0 * * * *  cd /path/to/enterprise-agentops && uv run python -m app.pricing_cleanup_job

Once an hour is the cadence the design settled on. It is deliberately coarse:
`cleanup_at` is a date-scale deadline, not a deadline measured in minutes, and
a pass that finds nothing due costs one provider read and nothing else.

**Why a process and not a thread.** Running a scheduler inside the API would
mean cleanup only happens while a web process is up, would fire once per
worker, and would put a background writer inside the request process. A
separate invocation is observable, killable, and cannot be silently skipped by
a deployment that runs the API differently.

**Why it imports `app.main`.** So there is exactly one cleanup runner in the
system, wired from the same store, the same reader and the same write client
as the operator route. A second construction here would be a second
implementation that could drift on which switches it checks -- which is the
one thing this file must not be.

This adds no approval step, and that is the design: cleanup is restorative,
bounded by rows a human already authorised, and requiring a fresh decision per
removal would mean a missed decision leaves a permanent pin. It remains behind
both kill switches, proves ownership before sending anything, and never
retries an ambiguous DELETE.
"""

import json
import sys


def main(argv: list[str] | None = None) -> int:
    """Run one pass. Returns 0 when the pass completed, 1 when it could not.

    A non-zero exit means the pass did not run -- not that a cleanup failed.
    An individual record resolving to NEEDS_REVIEW or UNKNOWN_CLEANUP_STATE is
    a normal, successful pass that found something a person must look at, and
    it is reported in the summary rather than as a crash. Cron treating it as
    failure would produce noise that trains someone to ignore the real thing.
    """
    from app.connectors.pricelabs.errors import PriceLabsUnavailable
    from app.main import pricelabs_cleanup_runner
    from app.pricing_cleanup_runner import summarise

    if pricelabs_cleanup_runner is None:
        print(
            "PriceLabs is not configured; no cleanup runner exists.",
            file=sys.stderr,
        )

        return 1

    try:
        outcomes = pricelabs_cleanup_runner.run_once()

    except PriceLabsUnavailable as exc:
        # Nothing was sent. The rows stay ACTIVE and the next hour retries the
        # read -- only a DELETE is never retried.
        print(f"PriceLabs could not be reached: {exc}", file=sys.stderr)

        return 1

    print(json.dumps(summarise(outcomes), indent=2))

    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
