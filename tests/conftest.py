"""Fixtures shared by the test suite."""

from typing import Any

import pytest


@pytest.fixture
def moneyline_event() -> dict[str, Any]:
    """A Polymarket NBA event shaped like the live Gamma payload."""
    return {
        "id": "999001",
        "slug": "nba-phi-bkn-2026-11-16",
        "gameId": "20023912",
        "title": "76ers vs. Nets",
        "startTime": "2026-11-17T00:30:00Z",
        "openInterest": 123456,
        "volume": 999999,
        "liquidity": 50000,
        "teams": [
            {
                "name": "76ers",
                "abbreviation": "phi",
                "ordering": "away",
                "record": "3-2",
            },
            {
                "name": "Nets",
                "abbreviation": "bkn",
                "ordering": "home",
                "record": "1-4",
            },
        ],
        "markets": [
            {
                "id": "100",
                "sportsMarketType": "spreads",
                "outcomes": '["76ers", "Nets"]',
                "clobTokenIds": '["aaa", "bbb"]',
                "outcomePrices": '["0.5", "0.5"]',
            },
            {
                "id": "200",
                "conditionId": "0xabc",
                "question": "76ers vs. Nets",
                "sportsMarketType": "moneyline",
                "outcomes": '["76ers", "Nets"]',
                "clobTokenIds": '["token-phi", "token-bkn"]',
                "outcomePrices": '["0.68", "0.32"]',
                "orderPriceMinTickSize": 0.001,
                "orderMinSize": 5,
                "negRisk": False,
                "closed": False,
                "active": True,
                "bestBid": 0.67,
                "bestAsk": 0.69,
            },
            {
                "id": "300",
                "sportsMarketType": "first_half_moneyline",
                "outcomes": '["76ers", "Nets"]',
                "clobTokenIds": '["ccc", "ddd"]',
                "outcomePrices": '["0.6", "0.4"]',
            },
        ],
    }


@pytest.fixture
def schedule_game() -> dict[str, Any]:
    """An API Ninjas schedule entry for the same matchup."""
    return {
        "game_id": "nba-20261116-phi-bkn",
        "league": "nba",
        "season": 2026,
        "season_type": "regular",
        "date": "2026-11-16",
        "start_time": "2026-11-17T00:30:00Z",
        "start_ts": 1794875400,
        "status": "scheduled",
        "status_detail": None,
        "period": None,
        "clock": None,
        "home_team": {"team_id": "nba-bkn", "name": "Brooklyn Nets", "abbreviation": "BKN"},
        "away_team": {"team_id": "nba-phi", "name": "Philadelphia 76ers", "abbreviation": "PHI"},
        "home_score": None,
        "away_score": None,
        "venue_name": "Barclays Center",
        "venue_city": "Brooklyn",
        "neutral_site": False,
    }
