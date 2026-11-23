"""Match NBA schedule games to Polymarket NBA events.

Pure functions only - no I/O - so the matching rules can be unit tested
without touching the network.

The join key is the **team abbreviation pair plus the ET calendar date**:

* Polymarket encodes it in the event slug: ``nba-{away}-{home}-{YYYY-MM-DD}``
  where the date is the ET game date.
* API Ninjas reports ``home_team.abbreviation`` / ``away_team.abbreviation``
  and a ``date`` field, which is the *local* game date.

The two sources disagree about which calendar day a late-evening game
belongs to (a 00:30 UTC game is the previous day in ET but the next day in
UTC), so the match tolerates a one-day window and resolves the ambiguity by
comparing the actual tip-off timestamps.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.api.gamma_sports import event_et_date, event_start_utc, event_teams

logger = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")
# How far apart the two sources' dates may be and still be considered a match.
DATE_TOLERANCE_DAYS = 1


def _abbr(value: Any) -> str:
    return str(value or "").strip().lower()


def _utc(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def game_et_date(game: dict[str, Any]) -> Optional[date]:
    """The ET calendar date of a schedule game."""
    raw_date = game.get("date")
    if raw_date:
        try:
            return datetime.strptime(str(raw_date), "%Y-%m-%d").date()
        except ValueError:
            pass
    start = _utc(game.get("start_time"))
    if start:
        return start.astimezone(ET_TZ).date()
    return None


def game_start_utc(game: dict[str, Any]) -> Optional[datetime]:
    """The UTC tip-off time of a schedule game."""
    if game.get("start_ts"):
        return datetime.fromtimestamp(int(game["start_ts"]), tz=timezone.utc)
    return _utc(game.get("start_time"))


def event_abbr_pair(event: dict[str, Any]) -> Optional[tuple[str, str]]:
    """``(away_abbr, home_abbr)`` for a Polymarket event."""
    teams = event_teams(event)
    away = _abbr((teams.get("away") or {}).get("abbreviation"))
    home = _abbr((teams.get("home") or {}).get("abbreviation"))
    if not away or not home:
        return None
    return away, home


def game_abbr_pair(game: dict[str, Any]) -> Optional[tuple[str, str]]:
    """``(away_abbr, home_abbr)`` for a schedule game."""
    away = _abbr((game.get("away_team") or {}).get("abbreviation"))
    home = _abbr((game.get("home_team") or {}).get("abbreviation"))
    if not away or not home:
        return None
    return away, home


def _dates_close(a: date, b: date) -> bool:
    return abs((a - b).days) <= DATE_TOLERANCE_DAYS


def match_game_to_event(
    game: dict[str, Any], events: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """Find the Polymarket event for one schedule game.

    Matches on the abbreviation pair first, then disambiguates by date within
    a one-day tolerance, preferring the event whose tip-off time is closest
    to the schedule game's tip-off time.
    """
    pair = game_abbr_pair(game)
    if not pair:
        return None

    game_day = game_et_date(game)
    game_start = game_start_utc(game)

    candidates: list[dict[str, Any]] = []
    for event in events:
        if event_abbr_pair(event) != pair:
            continue
        event_day = event_et_date(event)
        if game_day and event_day:
            if not _dates_close(game_day, event_day):
                continue
        candidates.append(event)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def distance(event: dict[str, Any]) -> float:
        event_start = event_start_utc(event)
        if not game_start or not event_start:
            return float("inf")
        return abs((event_start - game_start).total_seconds())

    # The tie-break must be deterministic: sort by distance first, then by
    # slug, so the same candidate always wins regardless of input order.
    candidates.sort(key=lambda e: (distance(e), str(e.get("slug") or "")))
    return candidates[0]


def match_schedule(
    games: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match a whole schedule against a list of Polymarket events.

    Returns ``(matched, unmatched)``. Each matched entry carries the schedule
    game plus the matched event under ``"event"``.
    """
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    used_slugs: set[str] = set()

    for game in games:
        event = match_game_to_event(game, events)
        if event is None:
            unmatched.append(game)
            continue
        slug = str(event.get("slug") or "")
        if slug and slug in used_slugs:
            unmatched.append(game)
            continue
        used_slugs.add(slug)
        matched.append({"game": game, "event": event})

    logger.info(
        "Matched %d/%d games to Polymarket events", len(matched), len(games)
    )
    return matched, unmatched


def unmatched_label(game: dict[str, Any]) -> str:
    """A short human-readable label for an unmatched game."""
    away = (game.get("away_team") or {}).get("abbreviation") or "?"
    home = (game.get("home_team") or {}).get("abbreviation") or "?"
    return f"{away}@{home} {game.get('date') or ''}".strip()


def games_in_window(
    games: list[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    min_minutes: int = 0,
    max_minutes: int = 150,
) -> list[dict[str, Any]]:
    """Games whose tip-off falls within ``[now+min, now+max]`` minutes."""
    current = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for game in games:
        start = game_start_utc(game)
        if not start:
            continue
        delta = (start - current).total_seconds() / 60.0
        if min_minutes <= delta <= max_minutes:
            out.append(game)
    return out


def games_started(
    games: list[dict[str, Any]], *, now: Optional[datetime] = None
) -> list[dict[str, Any]]:
    """Games that have already tipped off."""
    current = now or datetime.now(timezone.utc)
    return [g for g in games if (game_start_utc(g) or current) <= current]


def games_within_halftime_window(
    games: list[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    max_minutes: int = 150,
) -> list[dict[str, Any]]:
    """Games that started but are still inside the monitoring window."""
    current = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for game in games:
        start = game_start_utc(game)
        if not start:
            continue
        elapsed = (current - start).total_seconds() / 60.0
        if 0 <= elapsed <= max_minutes:
            out.append(game)
    return out


def days_until(day: date, *, now: Optional[datetime] = None) -> int:
    """Whole days from ``now`` until ``day`` (negative when in the past)."""
    current = (now or datetime.now(timezone.utc)).astimezone(ET_TZ)
    return (day - current.date()).days


def target_dates_for_fetch(
    target: date, *, now: Optional[datetime] = None
) -> list[date]:
    """Which ET dates to fetch for a requested target date.

    A late-evening ET game starts after midnight UTC, so the "next day" in
    UTC terms still belongs to the requested ET date. Fetching the target
    date plus one day either side covers both boundaries.
    """
    return [target - timedelta(days=1), target, target + timedelta(days=1)]
