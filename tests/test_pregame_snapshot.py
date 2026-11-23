"""Tests for the pre-game price snapshot."""

from typing import Any, Optional

import pytest

from config.settings import settings
from src.fetch.pregame_snapshot import (
    choose_monitored_team,
    games_in_pregame_window,
    run_pregame_snapshot,
    snapshot_game_prices,
)


class FakeClob:
    def __init__(self, books: dict[str, dict[str, Any]]):
        self.books = books
        self.requested: list[str] = []

    def get_bid_ask(self, token_id: str) -> dict[str, Any]:
        self.requested.append(token_id)
        return self.books.get(str(token_id), {"best_bid": None, "best_ask": None})


class FakeStore:
    def __init__(self, games: list[dict[str, Any]]):
        self.games = {g["slug"]: dict(g) for g in games}
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str, dict[str, Any]]] = []

    def scan_scheduled_games(self, day: Optional[str] = None):
        return [g for g in self.games.values() if g.get("status") == "scheduled"]

    def get_games_for_date(self, day: str):
        return [g for g in self.games.values() if g.get("gsi1pk") == f"DATE#{day}"]

    def update_game(self, slug: str, updates: dict[str, Any]) -> bool:
        self.updates.append((slug, updates))
        if slug in self.games:
            self.games[slug].update(updates)
        return True

    def put_log(self, slug: str, message: str, fields: Optional[dict[str, Any]] = None) -> str:
        self.logs.append((slug, message, fields or {}))
        return "LOG#x"


def _game(**overrides: Any) -> dict[str, Any]:
    game = {
        "slug": "nba-phi-bkn-2026-11-16",
        "status": "scheduled",
        "start_ts": 1794875400,
        "team_tokens": {
            "away": {"name": "76ers", "abbreviation": "PHI", "token_id": "token-phi"},
            "home": {"name": "Nets", "abbreviation": "BKN", "token_id": "token-bkn"},
        },
    }
    game.update(overrides)
    return game


def test_games_in_pregame_window_selects_games_inside_the_window():
    now = 1794875400 - 20 * 60
    games = [_game(start_ts=now + 20 * 60), _game(start_ts=now + 120 * 60)]
    selected = games_in_pregame_window(games, now_ts=now)
    assert len(selected) == 1


def test_games_in_pregame_window_excludes_started_games():
    now = 1794875400
    games = [_game(start_ts=now - 10 * 60), _game(start_ts=now + 15 * 60)]
    selected = games_in_pregame_window(games, now_ts=now)
    assert len(selected) == 1
    assert selected[0]["start_ts"] == now + 15 * 60


def test_games_in_pregame_window_parses_iso_start_time():
    now = 1794875400 - 20 * 60
    game = _game(start_ts=None, start_time="2026-11-17T00:30:00Z")
    selected = games_in_pregame_window([game], now_ts=now)
    assert len(selected) == 1


def test_choose_monitored_team_picks_the_higher_mid():
    prices = {
        "76ers": {"mid": 0.68, "ask": 0.69},
        "Nets": {"mid": 0.32, "ask": 0.33},
    }
    assert choose_monitored_team(prices) == "76ers"


def test_choose_monitored_team_ignores_missing_mids():
    prices = {"76ers": {"mid": None, "ask": 0.69}, "Nets": {"mid": 0.32, "ask": 0.33}}
    assert choose_monitored_team(prices) == "Nets"


def test_choose_monitored_team_returns_none_without_prices():
    assert choose_monitored_team({}) is None


def test_snapshot_game_prices_reads_both_teams():
    clob = FakeClob(
        {
            "token-phi": {"best_bid": 0.67, "best_ask": 0.69},
            "token-bkn": {"best_bid": 0.31, "best_ask": 0.33},
        }
    )
    prices = snapshot_game_prices(_game(), clob)
    assert set(prices) == {"76ers", "Nets"}
    assert prices["76ers"]["mid"] == pytest.approx(0.68)
    assert prices["76ers"]["token_id"] == "token-phi"
    assert prices["Nets"]["mid"] == pytest.approx(0.32)


def test_run_pregame_snapshot_arms_the_game():
    now = 1794875400 - 20 * 60
    game = _game(start_ts=1794875400)
    store = FakeStore([game])
    clob = FakeClob(
        {
            "token-phi": {"best_bid": 0.67, "best_ask": 0.69},
            "token-bkn": {"best_bid": 0.31, "best_ask": 0.33},
        }
    )
    result = run_pregame_snapshot(store=store, client=clob, now_ts=now)
    assert len(result["armed"]) == 1
    assert result["armed"][0]["monitored_team"] == "76ers"

    updates = dict(store.updates[0][1])
    assert updates["status"] == "armed"
    assert updates["monitored_token_id"] == "token-phi"
    assert updates["monitored_pre_game_mid"] == pytest.approx(0.68)
    assert updates["trigger_threshold"] == pytest.approx(0.68 * settings.price_drop_ratio)
    assert store.games[game["slug"]]["status"] == "armed"


def test_run_pregame_snapshot_skips_games_outside_the_window():
    now = 1794875400
    store = FakeStore([_game(start_ts=now + 600 * 60)])
    clob = FakeClob({})
    result = run_pregame_snapshot(store=store, client=clob, now_ts=now)
    assert result["armed"] == []
    assert result["skipped"][0]["reason"] == "outside_pregame_window"


def test_run_pregame_snapshot_is_idempotent():
    now = 1794875400 - 20 * 60
    store = FakeStore([_game(start_ts=1794875400, pre_game_snapshotted_at=1)])
    clob = FakeClob({})
    result = run_pregame_snapshot(store=store, client=clob, now_ts=now)
    assert result["armed"] == []
    assert result["skipped"][0]["reason"] == "already_snapshotted"


def test_run_pregame_snapshot_skips_games_without_prices():
    now = 1794875400 - 20 * 60
    store = FakeStore([_game(start_ts=1794875400)])
    clob = FakeClob({})
    result = run_pregame_snapshot(store=store, client=clob, now_ts=now)
    assert result["skipped"][0]["reason"] == "no_mid_price"
