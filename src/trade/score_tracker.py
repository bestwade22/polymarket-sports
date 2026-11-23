"""Live score tracking for NBA games.

The sports WebSocket is the only push source for in-game state, but it is
the least reliable feed in the stack: it closes without a close frame, it
broadcasts every league, and it only emits frames for games in progress.

So it is treated as **optional**: it feeds the score display and the
halftime cutoff, and a REST fallback (API Ninjas, then ESPN) covers the
pre-game and finished states.

Nothing here gates a trade - the trigger rule never requires a score.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from config.settings import settings
from src.utils.market_parser import parse_float

logger = logging.getLogger(__name__)


@dataclass
class LiveScore:
    """The live state of one game."""

    away_team: str = ""
    home_team: str = ""
    away_score: Optional[int] = None
    home_score: Optional[int] = None
    period: Optional[int] = None
    elapsed: Optional[str] = None
    clock: Optional[str] = None
    live: bool = False
    ended: bool = False
    status: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "away_team": self.away_team,
            "home_team": self.home_team,
            "away_score": self.away_score,
            "home_score": self.home_score,
            "period": self.period,
            "elapsed": self.elapsed,
            "clock": self.clock,
            "live": self.live,
            "ended": self.ended,
            "status": self.status,
            "source": self.source,
        }


def _abbr(value: Any) -> str:
    return str(value or "").strip().lower()


def _int(value: Any) -> Optional[int]:
    parsed = parse_float(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (TypeError, ValueError):
        return None


class ScoreTracker:
    """Live scores keyed by the (away_abbr, home_abbr) pair.

    The sports feed's ``gameId`` is a different id space from Gamma's, so
    games are matched by team abbreviations.
    """

    def __init__(self) -> None:
        self._scores: dict[tuple[str, str], LiveScore] = {}
        # Games the listener is monitoring, keyed by the schedule game id, so
        # a REST score can be matched to the right (away, home) pair.
        self._game_ids: dict[str, tuple[str, str]] = {}

    def track_games(self, games: list[dict[str, Any]]) -> None:
        """Remember the monitored games so REST scores can be matched.

        The REST scoreboard is keyed by the schedule game id
        (``nba-20261117-nyk-tor``), while the tracker is keyed by the
        (away_abbr, home_abbr) pair.
        """
        self._game_ids = {}
        for game in games:
            game_id = str(game.get("game_id") or "").strip()
            away = _abbr(game.get("away_abbr"))
            home = _abbr(game.get("home_abbr"))
            if not game_id or not away or not home:
                continue
            self._game_ids[game_id] = (away, home)

    def key_for(self, away_abbr: str, home_abbr: str) -> tuple[str, str]:
        return (_abbr(away_abbr), _abbr(home_abbr))

    def get(self, away_abbr: str, home_abbr: str) -> Optional[LiveScore]:
        return self._scores.get(self.key_for(away_abbr, home_abbr))

    def apply_sports_frame(self, payload: dict[str, Any]) -> Optional[LiveScore]:
        """Apply one frame from the sports WebSocket.

        **Only NBA frames are accepted.** The feed broadcasts every league
        (cs2, dota2, tennis, ...), so the league filter is mandatory.
        """
        league = _abbr(payload.get("leagueAbbreviation"))
        if league != settings.sports_league:
            return None

        away = _abbr(payload.get("awayTeam"))
        home = _abbr(payload.get("homeTeam"))
        if not away or not home:
            return None

        score = self._scores.setdefault(
            (away, home), LiveScore(away_team=away, home_team=home)
        )
        score.away_score = _int(payload.get("awayScore"))
        score.home_score = _int(payload.get("homeScore"))
        score.period = _int(payload.get("period"))
        score.elapsed = payload.get("elapsed")
        score.clock = payload.get("clock")
        score.live = bool(payload.get("live"))
        score.ended = bool(payload.get("ended"))
        score.status = str(payload.get("status") or "")
        score.source = "sports_ws"

        logger.info(
            "Sports frame: %s @ %s %s-%s period=%s live=%s",
            away,
            home,
            score.away_score,
            score.home_score,
            score.period,
            score.live,
        )
        return score

    def apply_rest_scores(self, scores: dict[str, dict[str, Any]]) -> int:
        """Apply REST scores (API Ninjas / ESPN) for games with no live feed.

        Returns the number of games updated.
        """
        updated = 0
        for game_id, raw in scores.items():
            key = self._lookup_key(game_id)
            if key is None:
                continue
            score = self._scores.setdefault(key, LiveScore())
            score.away_score = _int(raw.get("away_score"))
            score.home_score = _int(raw.get("home_score"))
            score.period = _int(raw.get("period"))
            score.clock = raw.get("clock")
            score.status = str(raw.get("status") or "")
            score.source = str(raw.get("source") or "rest")
            updated += 1
        return updated

    def _lookup_key(self, game_id: str) -> Optional[tuple[str, str]]:
        """Find the tracked key for a schedule game id.

        API Ninjas game ids look like ``nba-20261117-nyk-tor`` - the away and
        home abbreviations are the last two dash-separated parts.
        """
        tracked = self._game_ids.get(str(game_id or ""))
        if tracked is not None:
            return tracked
        parts = str(game_id or "").split("-")
        if len(parts) < 2:
            return None
        away, home = _abbr(parts[-2]), _abbr(parts[-1])
        if not away or not home:
            return None
        key = (away, home)
        return key if key in self._scores else None

    def all_scores(self) -> list[dict[str, Any]]:
        return [score.to_dict() for score in self._scores.values()]
