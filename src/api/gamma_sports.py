"""Gamma API client for Polymarket NBA events and moneyline markets.

Verified against the live API:

* NBA games live in series ``10345`` (``GET /events/keyset?series_id=10345``).
* An event carries 400-480 markets; the moneyline is the single market whose
  ``sportsMarketType`` is exactly ``"moneyline"``.  The first-half moneyline
  is a *different* type (``"first_half_moneyline"``) and must not be matched.
* The moneyline's ``outcomes`` are **team names** (``["Spurs", "Kings"]``),
  not ``["Yes", "No"]``, and ``clobTokenIds`` / ``outcomePrices`` are
  JSON-encoded strings that are index-aligned with ``outcomes``.
* The event slug encodes the ET calendar date:
  ``nba-{away_abbr}-{home_abbr}-{YYYY-MM-DD}``.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests

from config.settings import GAMMA_API_BASE, settings
from src.utils.market_parser import (
    get_order_min_size,
    get_tick_size,
    is_neg_risk,
    parse_float,
    parse_list_field,
)

logger = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")
MONEYLINE_TYPE = "moneyline"


class GammaSportsClient:
    """Read NBA events and their moneyline markets from the Gamma API."""

    def __init__(self, base_url: str = GAMMA_API_BASE, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # --- events ------------------------------------------------------------

    def list_nba_events(
        self,
        *,
        series_id: Optional[str] = None,
        limit: int = 100,
        max_pages: int = 20,
        closed: str = "false",
    ) -> list[dict[str, Any]]:
        """Page through the NBA event list, keeping only moneyline events.

        The ``events/keyset`` endpoint's cursor is broken - it returns the
        same page no matter which cursor is sent - so offset pagination via
        ``GET /events`` is used instead. The server caps a page at 100
        events, so the limit is clamped.
        """
        series = series_id or settings.polymarket_series_id
        page_size = min(int(limit), 100)
        events: list[dict[str, Any]] = []
        seen: set[str] = set()

        for page in range(max_pages):
            params: dict[str, Any] = {
                "series_id": series,
                "limit": page_size,
                "offset": page * page_size,
                "closed": closed,
            }
            try:
                resp = self.session.get(
                    f"{self.base_url}/events", params=params, timeout=self.timeout
                )
                resp.raise_for_status()
                payload = resp.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Gamma events fetch failed: %s", exc)
                break

            batch = payload if isinstance(payload, list) else payload.get("events") or []
            if not batch:
                break

            new_count = 0
            for event in batch:
                slug = str(event.get("slug") or event.get("id") or "")
                if slug and slug in seen:
                    continue
                seen.add(slug)
                new_count += 1
                if self.has_moneyline(event):
                    events.append(event)

            if len(batch) < page_size or new_count == 0:
                break

        logger.info("Gamma returned %d NBA events with a moneyline market", len(events))
        return events

    def list_nba_games(self, **kwargs) -> list[dict[str, Any]]:
        """Alias for :meth:`list_nba_events`."""
        return self.list_nba_events(**kwargs)

    def get_event_by_slug(self, slug: str) -> Optional[dict[str, Any]]:
        """Fetch one event by its slug."""
        try:
            resp = self.session.get(
                f"{self.base_url}/events", params={"slug": slug}, timeout=self.timeout
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Gamma event by slug %s failed: %s", slug, exc)
            return None
        events = payload if isinstance(payload, list) else payload.get("events") or []
        for event in events:
            if self.has_moneyline(event):
                return event
        return events[0] if events else None

    def get_event_by_game_id(self, game_id: str) -> Optional[dict[str, Any]]:
        """Fetch one event by its Gamma ``gameId``."""
        try:
            resp = self.session.get(
                f"{self.base_url}/events", params={"game_id": game_id}, timeout=self.timeout
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Gamma event by game_id %s failed: %s", game_id, exc)
            return None
        events = payload if isinstance(payload, list) else payload.get("events") or []
        for event in events:
            if self.has_moneyline(event):
                return event
        return events[0] if events else None

    # --- moneyline ---------------------------------------------------------

    @staticmethod
    def find_moneyline_market(event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The single market whose ``sportsMarketType`` is ``"moneyline"``."""
        for market in event.get("markets") or []:
            if str(market.get("sportsMarketType") or "") == MONEYLINE_TYPE:
                return market
        return None

    @classmethod
    def has_moneyline(cls, event: dict[str, Any]) -> bool:
        return cls.find_moneyline_market(event) is not None

    @classmethod
    def parse_moneyline_market(cls, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Parse the moneyline market into a normalised structure.

        Returns ``None`` when the event has no moneyline market.

        The result contains:
            market_id, condition_id, question, tick_size, order_min_size,
            neg_risk, closed, active,
            teams: {team_name: {token_id, price, outcome_index}},
            tokens: {team_name: token_id},
            prices: {team_name: price}
        """
        market = cls.find_moneyline_market(event)
        if not market:
            return None

        outcomes = [str(o) for o in parse_list_field(market.get("outcomes"))]
        token_ids = [str(t) for t in parse_list_field(market.get("clobTokenIds"))]
        raw_prices = parse_list_field(market.get("outcomePrices"))

        teams: dict[str, dict[str, Any]] = {}
        tokens: dict[str, str] = {}
        prices: dict[str, float] = {}

        for index, outcome in enumerate(outcomes):
            token_id = token_ids[index] if index < len(token_ids) else None
            price = parse_float(raw_prices[index]) if index < len(raw_prices) else None
            if not token_id:
                continue
            teams[outcome] = {"token_id": token_id, "price": price, "outcome_index": index}
            tokens[outcome] = token_id
            if price is not None:
                prices[outcome] = price

        return {
            "market_id": str(market.get("id", "")),
            "condition_id": market.get("conditionId"),
            "question": market.get("question"),
            "slug": market.get("slug"),
            "tick_size": get_tick_size(market),
            "order_min_size": get_order_min_size(market),
            "neg_risk": is_neg_risk(market),
            "closed": bool(market.get("closed")),
            "active": bool(market.get("active")),
            "accepting_orders": bool(market.get("acceptingOrders")),
            "best_bid": parse_float(market.get("bestBid")),
            "best_ask": parse_float(market.get("bestAsk")),
            "last_trade_price": parse_float(market.get("lastTradePrice")),
            "teams": teams,
            "tokens": tokens,
            "prices": prices,
        }


# --- event helpers (pure functions, unit-testable) -------------------------


def event_teams(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Home/away team records keyed by ``"home"`` and ``"away"``."""
    teams = event.get("teams") or []
    out: dict[str, dict[str, Any]] = {}
    for team in teams:
        if not isinstance(team, dict):
            continue
        ordering = str(team.get("ordering") or "").strip().lower()
        if ordering in ("home", "away"):
            out[ordering] = team
    if "home" not in out or "away" not in out:
        # Fall back to list order: Gamma lists away first for these events.
        if len(teams) >= 2:
            out.setdefault("away", teams[0])
            out.setdefault("home", teams[1])
    return out


def team_abbreviation(team: Optional[dict[str, Any]]) -> str:
    """Lower-case team abbreviation, e.g. ``"phi"``."""
    if not team:
        return ""
    return str(team.get("abbreviation") or "").strip().lower()


def team_name(team: Optional[dict[str, Any]]) -> str:
    """Full team name, e.g. ``"76ers"``."""
    if not team:
        return ""
    return str(team.get("name") or team.get("alias") or "").strip()


def parse_slug(slug: str) -> Optional[dict[str, Any]]:
    """Parse ``nba-{away}-{home}-{YYYY-MM-DD}`` into its components.

    The date itself contains dashes, so the slug is split from the right:
    the last three parts are ``YYYY``, ``MM`` and ``DD``.

    Returns ``{"away_abbr", "home_abbr", "date"}`` or None when the slug does
    not follow the NBA convention.
    """
    if not slug:
        return None
    parts = slug.split("-")
    if len(parts) != 6 or parts[0] != "nba":
        return None
    away, home, year, month, day = parts[1], parts[2], parts[3], parts[4], parts[5]
    try:
        parsed = datetime.strptime(f"{year}-{month}-{day}", "%Y-%m-%d").date()
    except ValueError:
        return None
    return {"away_abbr": away.lower(), "home_abbr": home.lower(), "date": parsed}


def event_et_date(event: dict[str, Any]) -> Optional[date]:
    """The ET calendar date of a game.

    Prefers the date encoded in the slug (which is always the ET date) and
    falls back to converting ``startTime`` into Eastern time.
    """
    parsed = parse_slug(str(event.get("slug") or ""))
    if parsed:
        return parsed["date"]
    start = event.get("startTime")
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET_TZ).date()


def event_start_utc(event: dict[str, Any]) -> Optional[datetime]:
    """The UTC start time of a game."""
    start = event.get("startTime")
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def match_outcome_to_team(
    outcome: str, event: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Map a moneyline outcome label onto an event team record.

    The label is normally the team's ``name`` (e.g. "76ers"), but the API has
    also been seen to use the ``alias``. Matching is case-insensitive and
    whitespace-tolerant. **The outcome index is never assumed** to line up
    with the team's ordering in ``event["teams"]``.
    """
    if not outcome:
        return None
    target = " ".join(str(outcome).split()).lower()
    teams = event_teams(event)

    for team in teams.values():
        for field in ("name", "alias"):
            label = " ".join(str(team.get(field) or "").split()).lower()
            if label and label == target:
                return team

    # Fall back to abbreviation matching.
    for team in teams.values():
        abbr = team_abbreviation(team)
        if abbr and abbr == target:
            return team

    return None


def build_team_token_map(
    event: dict[str, Any], moneyline: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Map each side of a game onto its CLOB token and pre-game price.

    Returns ``{"home": {...}, "away": {...}}`` where each entry has
    ``name``, ``abbreviation``, ``token_id``, ``price`` and ``role``.
    """
    teams = event_teams(event)
    tokens = moneyline.get("tokens") or {}
    prices = moneyline.get("prices") or {}

    result: dict[str, dict[str, Any]] = {}
    used_outcomes: set[str] = set()

    for role in ("away", "home"):
        team = teams.get(role)
        if not team:
            continue
        name = team_name(team)
        token_id = tokens.get(name)
        if token_id is None:
            # Try the alias and a case-insensitive scan as a fallback.
            alias = str(team.get("alias") or "").strip()
            token_id = tokens.get(alias)
            if token_id is None:
                for outcome, candidate in tokens.items():
                    if outcome.lower() == name.lower() and outcome not in used_outcomes:
                        token_id = candidate
                        break
        if token_id is None:
            continue
        used_outcomes.add(name)
        result[role] = {
            "role": role,
            "name": name,
            "abbreviation": team_abbreviation(team),
            "token_id": token_id,
            "price": prices.get(name),
            "record": team.get("record"),
        }

    return result


def summarise_event(event: dict[str, Any]) -> dict[str, Any]:
    """A compact, JSON-safe view of an event for storage and logging."""
    teams = event_teams(event)
    moneyline = GammaSportsClient.parse_moneyline_market(event) or {}
    team_tokens = build_team_token_map(event, moneyline)
    start = event_start_utc(event)
    return {
        "slug": event.get("slug"),
        "event_id": str(event.get("id", "")),
        "game_id": str(event.get("gameId", "")),
        "title": event.get("title"),
        "start_time": start.isoformat() if start else None,
        "et_date": event_et_date(event).isoformat() if event_et_date(event) else None,
        "away_team": team_tokens.get("away", {}).get("name"),
        "away_abbr": team_tokens.get("away", {}).get("abbreviation"),
        "away_token_id": team_tokens.get("away", {}).get("token_id"),
        "away_price": team_tokens.get("away", {}).get("price"),
        "away_record": teams.get("away", {}).get("record"),
        "home_team": team_tokens.get("home", {}).get("name"),
        "home_abbr": team_tokens.get("home", {}).get("abbreviation"),
        "home_token_id": team_tokens.get("home", {}).get("token_id"),
        "home_price": team_tokens.get("home", {}).get("price"),
        "home_record": teams.get("home", {}).get("record"),
        "market_id": moneyline.get("market_id"),
        "condition_id": moneyline.get("condition_id"),
        "tick_size": moneyline.get("tick_size"),
        "order_min_size": moneyline.get("order_min_size"),
        "neg_risk": moneyline.get("neg_risk"),
        "open_interest": parse_float(event.get("openInterest")),
        "volume": parse_float(event.get("volume")),
        "liquidity": parse_float(event.get("liquidity")),
    }


def events_by_abbr_date(
    events: Iterable[dict[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Index events by ``(away_abbr, home_abbr, et_date)``.

    Team abbreviations are the only reliable join key between Polymarket and
    the NBA schedule, and the ET date disambiguates the two games a team can
    play in a week.
    """
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        teams = event_teams(event)
        away = team_abbreviation(teams.get("away"))
        home = team_abbreviation(teams.get("home"))
        day = event_et_date(event)
        if not away or not home or not day:
            continue
        index[(away, home, day.isoformat())] = event
    return index
