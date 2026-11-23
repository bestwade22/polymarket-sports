"""Tests for the daily fetch's listener-window integration."""

from typing import Any, Optional

from src.fetch.daily_fetch import run_daily_fetch

FIRST_TIPOFF = 1794873600
SECOND_TIPOFF = 1794883800


class FakeStore:
    def __init__(self):
        self.windows: dict[str, dict[str, Any]] = {}
        self.batches: list[tuple[str, dict[str, Any]]] = []
        self.games: list[tuple[str, dict[str, Any]]] = []

    def put_game(self, slug: str, meta: dict[str, Any]):
        self.games.append((slug, meta))

    def put_daily_batch(self, day: str, batch: dict[str, Any]) -> None:
        self.batches.append((day, batch))

    def put_schedule_window(self, day: str, window: dict[str, Any]) -> None:
        self.windows[day] = window

    def get_schedule_window(self, day: str):
        return self.windows.get(day)


class FakeNba:
    def fetch_games(self, league: str = "nba"):
        return [
            {
                "game_id": "g1",
                "date": "2026-11-16",
                "start_time": "2026-11-17T00:00:00Z",
                "start_ts": FIRST_TIPOFF,
                "status": "scheduled",
                "home_team": {"name": "Nets", "abbreviation": "BKN"},
                "away_team": {"name": "76ers", "abbreviation": "PHI"},
            },
            {
                "game_id": "g2",
                "date": "2026-11-16",
                "start_time": "2026-11-17T02:30:00Z",
                "start_ts": SECOND_TIPOFF,
                "status": "scheduled",
                "home_team": {"name": "Heat", "abbreviation": "MIA"},
                "away_team": {"name": "Celtics", "abbreviation": "BOS"},
            },
        ]

    def fetch_standings(self, league: str = "nba"):
        return {}


class FakeGamma:
    def list_nba_events(self):
        return [
            {
                "id": "1",
                "slug": "nba-phi-bkn-2026-11-16",
                "title": "76ers vs. Nets",
                "startTime": "2026-11-17T00:00:00Z",
                "teams": [
                    {"name": "76ers", "abbreviation": "phi", "ordering": "away"},
                    {"name": "Nets", "abbreviation": "bkn", "ordering": "home"},
                ],
                "markets": [
                    {
                        "id": "200",
                        "question": "76ers vs. Nets",
                        "sportsMarketType": "moneyline",
                        "outcomes": '["76ers", "Nets"]',
                        "clobTokenIds": '["token-phi", "token-bkn"]',
                        "outcomePrices": '["0.68", "0.32"]',
                    }
                ],
            },
            {
                "id": "2",
                "slug": "nba-bos-mia-2026-11-16",
                "title": "Celtics vs. Heat",
                "startTime": "2026-11-17T02:30:00Z",
                "teams": [
                    {"name": "Celtics", "abbreviation": "bos", "ordering": "away"},
                    {"name": "Heat", "abbreviation": "mia", "ordering": "home"},
                ],
                "markets": [
                    {
                        "id": "300",
                        "question": "Celtics vs. Heat",
                        "sportsMarketType": "moneyline",
                        "outcomes": '["Celtics", "Heat"]',
                        "clobTokenIds": '["token-bos", "token-mia"]',
                        "outcomePrices": '["0.6", "0.4"]',
                    }
                ],
            },
        ]


def _run():
    from datetime import date

    store = FakeStore()
    result = run_daily_fetch(
        target_date=date(2026, 11, 16),
        store=store,
        nba_client=FakeNba(),
        gamma_client=FakeGamma(),
        notify=False,
        now_ts=FIRST_TIPOFF - 3600,
    )
    return store, result


class TestDailyFetchWindow:
    def test_stores_a_window_for_the_day(self) -> None:
        store, result = _run()
        assert result["window"] is not None
        assert "2026-11-16" in store.windows

    def test_window_spans_the_first_to_the_last_game(self) -> None:
        store, _ = _run()
        window = store.windows["2026-11-16"]
        assert window["first_start_ts"] == FIRST_TIPOFF
        assert window["last_start_ts"] == SECOND_TIPOFF
        assert window["game_count"] == 2

    def test_window_is_returned_in_the_result(self) -> None:
        _, result = _run()
        assert result["window"]["start_ts"] < FIRST_TIPOFF
        assert result["window"]["end_ts"] > SECOND_TIPOFF

    def test_the_batch_is_still_written(self) -> None:
        store, _ = _run()
        assert store.batches
        assert store.batches[0][0] == "2026-11-16"
