"""Derive the Fargate listener's on-window from the day's tip-off times.

The listener only needs to be running while there is a game it could trade:
from a little before the first tip-off (the pre-game snapshot arms a game at
T-30, and the watch loop picks it up within a few minutes) until a little
after the last game's halftime cutoff (the trigger stops at halftime, and an
in-flight buy must still be able to place its recovery sell).

A fixed cron cannot express this: an 18:00 ET early tip-off, a double header,
a make-up game or a rescheduled tip-off all move the window. This module
computes it from the schedule instead.

Pure functions only - no I/O - so the rules are unit testable.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# How long before the first tip-off the listener must already be up.
#
# The pre-game snapshot arms a game at T-30 (``PRE_GAME_SNAPSHOT_MINUTES``)
# and the listener's watch loop polls every ``SNAPSHOT_INTERVAL_MINUTES``.
# The listener therefore has to be running before the snapshot that arms the
# first game, not merely before tip-off - otherwise the first game of the
# day is missed while the task is still spinning up.
DEFAULT_LEAD_MINUTES = 40

# How long after the last tip-off the listener stays up.
#
# The trigger stops at the halftime cutoff, which is roughly 60 minutes after
# tip-off. The recovery sell is a GTC order, but a fill that arrives after
# the worker has moved on is only placed via the listener's user WebSocket,
# so the listener must outlive the last possible trigger.
DEFAULT_TRAIL_MINUTES = 90

# A window shorter than this is treated as unusable: a game that is already
# over, or a schedule that is still being populated.
MIN_WINDOW_MINUTES = 15

# The hard ceiling on the window length. A mis-parsed schedule must not ask
# the listener to run for a week.
MAX_WINDOW_HOURS = 12


def _as_ts(value: Any) -> Optional[int]:
    """Coerce a start time into an epoch second."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def game_start_ts(game: dict[str, Any]) -> Optional[int]:
    """The tip-off time of one game as an epoch second."""
    for key in ("start_ts", "start_time"):
        ts = _as_ts(game.get(key))
        if ts:
            return ts
    return None


def collect_start_times(games: list[dict[str, Any]]) -> list[int]:
    """Every tip-off time in the list, sorted ascending."""
    starts = [ts for ts in (game_start_ts(g) for g in games) if ts]
    return sorted(starts)


def compute_window(
    games: list[dict[str, Any]],
    *,
    now_ts: Optional[int] = None,
    lead_minutes: int = DEFAULT_LEAD_MINUTES,
    trail_minutes: int = DEFAULT_TRAIL_MINUTES,
) -> Optional[dict[str, Any]]:
    """The listener window for a set of games.

    Returns ``None`` when there is no usable window (no games with a known
    tip-off, or every game has already finished). Otherwise returns a dict
    with ``start_ts`` / ``end_ts`` plus the first and last tip-off for
    logging.
    """
    starts = collect_start_times(games)
    if not starts:
        logger.info("No tip-off times available; no listener window")
        return None

    now = int(now_ts) if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    first_start = starts[0]
    last_start = starts[-1]

    window_start = first_start - int(lead_minutes) * 60
    window_end = last_start + int(trail_minutes) * 60

    # A game whose window has already passed (the day is over) cannot be
    # traded, so the listener has nothing to do.
    if window_end <= now:
        logger.info(
            "Listener window already over (last tip-off %s); nothing to run",
            datetime.fromtimestamp(last_start, tz=timezone.utc).isoformat(),
        )
        return None

    # If the window has already started, start it now rather than in the past.
    effective_start = max(window_start, now)

    length_minutes = (window_end - effective_start) / 60.0
    if length_minutes < MIN_WINDOW_MINUTES:
        logger.info(
            "Listener window is only %.0f min long; treating as unusable",
            length_minutes,
        )
        return None

    if length_minutes > MAX_WINDOW_HOURS * 60:
        logger.warning(
            "Listener window is %.1f h long; capping at %d h",
            length_minutes / 60.0,
            MAX_WINDOW_HOURS,
        )
        window_end = effective_start + MAX_WINDOW_HOURS * 3600

    return {
        "start_ts": effective_start,
        "end_ts": window_end,
        "first_start_ts": first_start,
        "last_start_ts": last_start,
        "game_count": len(starts),
        "lead_minutes": int(lead_minutes),
        "trail_minutes": int(trail_minutes),
    }


def window_is_open(
    window: Optional[dict[str, Any]], *, now_ts: Optional[int] = None
) -> bool:
    """Whether the listener should be running right now."""
    if not window:
        return False
    now = int(now_ts) if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    start = int(window.get("start_ts") or 0)
    end = int(window.get("end_ts") or 0)
    return start <= now < end


def window_seconds_remaining(
    window: Optional[dict[str, Any]], *, now_ts: Optional[int] = None
) -> int:
    """Seconds until the window closes (0 when it is already closed)."""
    if not window:
        return 0
    now = int(now_ts) if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    return max(0, int(window.get("end_ts") or 0) - now)


def describe_window(window: Optional[dict[str, Any]]) -> str:
    """A short human-readable summary for logs."""
    if not window:
        return "no window"
    start = datetime.fromtimestamp(int(window["start_ts"]), tz=timezone.utc)
    end = datetime.fromtimestamp(int(window["end_ts"]), tz=timezone.utc)
    return (
        f"{start.isoformat()} -> {end.isoformat()} "
        f"({window.get('game_count', 0)} games)"
    )


def parse_schedule_expression(window: Optional[dict[str, Any]]) -> Optional[str]:
    """An EventBridge ``at()`` expression for the window's start.

    EventBridge Scheduler's ``at()`` takes a UTC ISO timestamp with no
    timezone suffix. Returns None when there is no usable window.
    """
    if not window:
        return None
    start = datetime.fromtimestamp(int(window["start_ts"]), tz=timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%S")


def parse_schedule_end_expression(window: Optional[dict[str, Any]]) -> Optional[str]:
    """An EventBridge ``at()`` expression for the window's end."""
    if not window:
        return None
    end = datetime.fromtimestamp(int(window["end_ts"]), tz=timezone.utc)
    return end.strftime("%Y-%m-%dT%H:%M:%S")


def next_window_start(
    windows: list[Optional[dict[str, Any]]], *, now_ts: Optional[int] = None
) -> Optional[int]:
    """The earliest future window start across a list of windows."""
    now = int(now_ts) if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    starts = [
        int(w["start_ts"]) for w in windows if w and int(w.get("start_ts") or 0) > now
    ]
    return min(starts) if starts else None
