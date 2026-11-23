"""NBA schedule, standings and live-score client.

Primary source: the API Ninjas basketball API.

* ``GET /v1/basketballgames?league=nba`` - the schedule. Each game carries
  ``start_time`` in UTC, ``home_team`` / ``away_team`` objects with an
  ``abbreviation``, a ``status`` and, once live, ``period`` / ``clock`` and
  the scores.
* ``GET /v1/basketballstandings?league=nba`` - one row per team with
  ``wins``, ``losses``, ``win_pct``, ``streak``, ``last_10``, ``home_record``,
  ``away_record``, ``conference_rank`` and ``point_differential``.

Fallback for live scores: the public ESPN scoreboard API, used when API
Ninjas has no live state for a game.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests

from config.settings import API_NINJAS_BASE, ESPN_SCOREBOARD_URL, settings
from src.utils.market_parser import parse_float

logger = logging.getLogger(__name__)


def _iso_to_utc(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _team_ref(team: Any) -> dict[str, Any]:
    """Normalise an API Ninjas team object."""
    if not isinstance(team, dict):
        return {"name": "", "abbreviation": "", "team_id": ""}
    return {
        "name": str(team.get("name") or "").strip(),
        "abbreviation": str(team.get("abbreviation") or "").strip().lower(),
        "team_id": str(team.get("team_id") or "").strip(),
    }


class NbaClient:
    """Read NBA schedule and standings."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = API_NINJAS_BASE):
        self.api_key = api_key if api_key is not None else settings.api_ninjas_key
        self.base_url = base_url.rstrip("/")
        self.timeout = 30
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ValueError("API_NINJAS_KEY not configured")
        return {"X-Api-Key": self.api_key}

    # --- schedule ----------------------------------------------------------

    def fetch_games(self, league: str = "nba") -> list[dict[str, Any]]:
        """All games for a league, normalised."""
        try:
            resp = self.session.get(
                f"{self.base_url}/basketballgames",
                params={"league": league},
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("API Ninjas games fetch failed: %s", exc)
            return []

        if not isinstance(payload, list):
            logger.warning("API Ninjas games returned %s, expected a list", type(payload))
            return []

        games = [self._normalise_game(g) for g in payload if isinstance(g, dict)]
        games.sort(key=lambda g: g.get("start_time") or "")
        logger.info("API Ninjas returned %d NBA games", len(games))
        return games

    def fetch_todays_games(self, league: str = "nba") -> list[dict[str, Any]]:
        """Alias for :meth:`fetch_games`."""
        return self.fetch_games(league)

    @staticmethod
    def _normalise_game(raw: dict[str, Any]) -> dict[str, Any]:
        home = _team_ref(raw.get("home_team"))
        away = _team_ref(raw.get("away_team"))
        start = _iso_to_utc(raw.get("start_time"))
        venue = raw.get("venue") if isinstance(raw.get("venue"), dict) else {}
        return {
            "game_id": str(raw.get("game_id") or ""),
            "league": raw.get("league"),
            "season": raw.get("season"),
            "season_type": raw.get("season_type"),
            "date": raw.get("date"),
            "start_time": start.isoformat() if start else None,
            "start_ts": int(start.timestamp()) if start else None,
            "status": raw.get("status"),
            "status_detail": raw.get("status_detail"),
            "period": raw.get("period"),
            "clock": raw.get("clock"),
            "home_team": home,
            "away_team": away,
            "home_score": raw.get("home_score"),
            "away_score": raw.get("away_score"),
            "venue_name": venue.get("name"),
            "venue_city": venue.get("city"),
            "neutral_site": bool(raw.get("neutral_site")),
        }

    # --- standings ---------------------------------------------------------

    def fetch_standings(self, league: str = "nba") -> dict[str, dict[str, Any]]:
        """Standings keyed by lower-case team abbreviation."""
        try:
            resp = self.session.get(
                f"{self.base_url}/basketballstandings",
                params={"league": league},
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("API Ninjas standings fetch failed: %s", exc)
            return {}

        if not isinstance(payload, list):
            logger.warning("API Ninjas standings returned %s, expected a list", type(payload))
            return {}

        table: dict[str, dict[str, Any]] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue
            team = _team_ref(row.get("team"))
            abbr = team["abbreviation"]
            if not abbr:
                continue
            table[abbr] = {
                "team_id": team["team_id"],
                "name": team["name"],
                "abbreviation": abbr,
                "wins": row.get("wins"),
                "losses": row.get("losses"),
                "win_pct": parse_float(row.get("win_pct")),
                "games_played": row.get("games_played"),
                "streak": row.get("streak"),
                "last_10": row.get("last_10"),
                "home_record": row.get("home_record"),
                "away_record": row.get("away_record"),
                "conference": row.get("conference"),
                "conference_record": row.get("conference_record"),
                "conference_rank": row.get("conference_rank"),
                "division": row.get("division"),
                "league_rank": row.get("league_rank"),
                "point_differential": row.get("point_differential"),
                "points_for": row.get("points_for"),
                "points_against": row.get("points_against"),
            }
        logger.info("API Ninjas returned standings for %d teams", len(table))
        return table

    # --- live scores -------------------------------------------------------

    def fetch_live_score(self, league: str = "nba") -> dict[str, dict[str, Any]]:
        """Live score for every in-progress game, keyed by game id.

        Uses the public ESPN scoreboard as a fallback when API Ninjas reports
        no live period for a game.
        """
        scores: dict[str, dict[str, Any]] = {}
        for game in self.fetch_games(league):
            if game.get("status") not in ("in_progress", "live", "halftime"):
                continue
            scores[str(game.get("game_id"))] = {
                "home_score": game.get("home_score"),
                "away_score": game.get("away_score"),
                "period": game.get("period"),
                "clock": game.get("clock"),
                "status": game.get("status"),
                "source": "api_ninjas",
            }
        if scores:
            return scores
        return self.fetch_espn_scores(league)

    def fetch_espn_scores(self, league: str = "nba") -> dict[str, dict[str, Any]]:
        """Live scores from the public ESPN scoreboard API."""
        try:
            resp = self.session.get(ESPN_SCOREBOARD_URL, timeout=self.timeout)
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("ESPN scoreboard fetch failed: %s", exc)
            return {}

        out: dict[str, dict[str, Any]] = {}
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            competition = (event.get("competitions") or [{}])[0]
            status = competition.get("status") or {}
            # ``status.period`` is a dict in some ESPN payloads and a plain
            # integer in others, so both shapes are accepted.
            period = status.get("period")
            period_number = (
                period.get("number") if isinstance(period, dict) else period
            )
            # The competitors live on the competition in the older payloads
            # and on the event in the newer ones.
            competitors = competition.get("competitors") or event.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            out[str(event.get("id") or competition.get("id"))] = {
                "home_score": (home or {}).get("score"),
                "away_score": (away or {}).get("score"),
                "period": period_number,
                "clock": (status.get("displayClock")),
                "status": (status.get("type") or {}).get("description"),
                "source": "espn",
            }
        return out

    # --- helpers -----------------------------------------------------------

    def games_for_date(self, target: date, league: str = "nba") -> list[dict[str, Any]]:
        """Games whose schedule date equals ``target``."""
        key = target.isoformat()
        return [g for g in self.fetch_games(league) if g.get("date") == key]

    def standings_for_team(self, abbr: str, league: str = "nba") -> Optional[dict[str, Any]]:
        """Standings row for one team abbreviation."""
        return self.fetch_standings(league).get(str(abbr or "").lower())
