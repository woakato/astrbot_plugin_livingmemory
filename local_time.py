"""Local-calendar helpers shared by storage, recall and open-loop logic.

Companion events are bucketed into days, memory items are tagged with a
``YYYY-MM-DD`` label, and open-loop due dates are estimated — all against the
*same* calendar day. Resolving those through different clocks (UTC, the process
timezone, and Asia/Shanghai) mislabelled everything created between 00:00 and
08:00 Beijing time as the previous day, so all of them now route through this
module.

Dependency-free on purpose: importing this must never pull in astrbot or any
other plugin subpackage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: Deployment-local calendar used for every day-level bucket (spec §13).
TZ_SHANGHAI = timezone(timedelta(hours=8))

DAY_FORMAT = "%Y-%m-%d"


def local_day_label(epoch_seconds: float | int | str | None) -> str:
    """Render an epoch-seconds timestamp as a local-calendar ``YYYY-MM-DD``.

    Args:
        epoch_seconds: epoch seconds; ``None``/``0``/unparsable yields ``""``.

    Returns:
        The local-calendar date label, or ``""`` when the input is unusable.
    """
    try:
        ts = float(epoch_seconds or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=TZ_SHANGHAI).strftime(DAY_FORMAT)


def local_day_now() -> str:
    """Today's date in the local calendar as ``YYYY-MM-DD``."""
    return datetime.now(tz=TZ_SHANGHAI).strftime(DAY_FORMAT)


def local_datetime(epoch_seconds: float | int) -> datetime:
    """Timezone-aware local datetime for an epoch-seconds timestamp."""
    return datetime.fromtimestamp(float(epoch_seconds), tz=TZ_SHANGHAI)
