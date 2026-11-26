"""Tests for the listener's lowest-price tracking and heartbeat.

The lowest bid the monitored team ever traded at is the number that tells
a human how far the price actually fell, so it is tracked in memory and
flushed to DynamoDB on a timer rather than on every tick.
"""

import asyncio
import json
import time
from typing import Any, Optional

import pytest

from src.trade.listener import Listener


class FakeStore:
    def __init__(self, game: Optional[dict[str, Any]] = None):
        self.game = game
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str, dict[str, Any]]] = []
        self.claims = 0
        self.window: Optional[dict[str, Any]] = None

    def get_schedule_window(self, day: str) -> Optional[dict[str, Any]]:
        return self.window

    def get_game(self, slug: str) -> Optional[dict[str, Any]]:
        return self.game if self.game and self.game.get("slug") == slug else None

    def claim_trigger(self, slug: str, claimed_at: Optional[int] = None) -> bool:
        self.claims += 1
        return True

    def update_game(self, slug: str, updates: dict[str, Any]) -> bool:
        self.updates.append((slug, updates))
        if self.game:
            self.game.update(updates)
        return True

    def put_log(self, slug: str, message: str, fields: Optional[dict[str, Any]] = None) -> str:
        self.logs.append((slug, message, fields or {}))
        return "LOG#x"


class FakeQueue:
    def __init__(self):
        self.published: list[dict[str, Any]] = []

    def publish_trigger(self, payload: dict[str, Any]) -> str:
        self.published.append(payload)
        return "msg-1"

    def publish_fill(self, payload: dict[str, Any]) -> str:
        self.published.append(payload)
        return "msg-2"


def _game(**overrides: Any) -> dict[str, Any]:
    game = {
        "slug": "nba-phi-bkn-2026-11-16",
        "status": "armed",
        "start_ts": int(time.time()) - 20 * 60,
        "away_abbr": "PHI",
        "home_abbr": "BKN",
        "monitored_team": "76ers",
        "monitored_token_id": "token-phi",
        "monitored_pre_game_mid": 0.68,
        "market_id": "200",
        "tick_size": "0.001",
        "neg_risk": False,
        "team_tokens": {
            "away": {"name": "76ers", "abbreviation": "PHI", "token_id": "token-phi"},
            "home": {"name": "Nets", "abbreviation": "BKN", "token_id": "token-bkn"},
        },
    }
    game.update(overrides)
    return game


def _listener() -> tuple[Listener, FakeStore, FakeQueue]:
    store = FakeStore(_game())
    queue = FakeQueue()
    listener = Listener(store=store, queue=queue, dry_run=True)
    listener.games = [store.game]
    return listener, store, queue


def _push_bid(listener: Listener, token: str, bid: str) -> None:
    raw = json.dumps(
        {
            "event_type": "price_change",
            "market": "0xmarket",
            "price_changes": [
                {
                    "asset_id": token,
                    "price": bid,
                    "size": "100",
                    "side": "SELL",
                    "best_bid": bid,
                    "best_ask": "0.70",
                }
            ],
        }
    )
    asyncio.run(listener.handle_market_frame(raw))


def test_the_lowest_bid_is_tracked_for_the_monitored_team():
    listener, store, queue = _listener()
    _push_bid(listener, "token-phi", "0.60")
    _push_bid(listener, "token-phi", "0.40")
    _push_bid(listener, "token-phi", "0.55")

    assert listener._lowest_bids["nba-phi-bkn-2026-11-16"] == 0.40


def test_the_opponent_price_does_not_move_the_lowest_bid():
    listener, store, queue = _listener()
    _push_bid(listener, "token-phi", "0.60")
    _push_bid(listener, "token-bkn", "0.05")

    assert listener._lowest_bids.get("nba-phi-bkn-2026-11-16") == 0.60


def test_flushing_writes_the_lowest_bid_to_dynamodb():
    listener, store, queue = _listener()
    _push_bid(listener, "token-phi", "0.50")
    _push_bid(listener, "token-phi", "0.45")

    written = listener.flush_lowest_prices()

    assert written == 1
    slug, updates = store.updates[-1]
    assert slug == "nba-phi-bkn-2026-11-16"
    assert updates["lowest_bid"] == 0.45
    assert updates["lowest_bid_ts"] is not None


def test_flushing_twice_does_not_write_again():
    listener, store, queue = _listener()
    _push_bid(listener, "token-phi", "0.45")
    listener.flush_lowest_prices()
    count = len(store.updates)

    assert listener.flush_lowest_prices() == 0
    assert len(store.updates) == count


def test_a_higher_price_does_not_mark_the_game_dirty():
    listener, store, queue = _listener()
    _push_bid(listener, "token-phi", "0.45")
    listener.flush_lowest_prices()
    _push_bid(listener, "token-phi", "0.60")

    assert listener._lowest_dirty == set()
    assert listener._lowest_bids["nba-phi-bkn-2026-11-16"] == 0.45


def test_the_heartbeat_reports_the_armed_games_and_the_bids(caplog):
    listener, store, queue = _listener()
    _push_bid(listener, "token-phi", "0.50")

    with caplog.at_level("INFO"):
        listener.log_heartbeat()

    assert "Heartbeat" in caplog.text
    assert "1 armed game(s)" in caplog.text
    assert "76ers" in caplog.text
    assert "0.5000" in caplog.text


def test_a_missing_price_is_reported_as_a_dash(caplog: pytest.LogCaptureFixture):
    listener, store, queue = _listener()

    with caplog.at_level("INFO"):
        listener.log_heartbeat()

    assert "bid=-" in caplog.text
