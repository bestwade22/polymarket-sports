"""Tests for the listener's market-frame handling.

The frame shapes are the ones measured against the live CLOB market socket:

* the initial subscribe snapshot arrives as a JSON **array** of ``book``
  objects, one per subscribed asset;
* every later frame is a single ``price_change`` object whose
  ``price_changes`` entries each name their own ``asset_id`` and carry the
  feed's ``best_bid`` / ``best_ask``.
"""

import asyncio
import json
import time
from typing import Any, Optional

import pytest

from src.trade.listener import Listener, build_market_subscribe_frame


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
        # The game tipped off 20 minutes ago, so it is inside the monitoring
        # window (tip-off .. tip-off + MONITOR_MAX_MINUTES).
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


def _listener(game: Optional[dict[str, Any]] = None) -> tuple[Listener, FakeStore, FakeQueue]:
    store = FakeStore(game if game is not None else _game())
    queue = FakeQueue()
    listener = Listener(store=store, queue=queue, dry_run=True)
    listener.games = [store.game] if store.game else []
    return listener, store, queue


def test_subscribe_frame_shape():
    frame = json.loads(build_market_subscribe_frame(["a", "b"]))
    assert frame["assets_ids"] == ["a", "b"]
    assert frame["type"] == "market"
    assert frame["custom_feature_enabled"] is True


def test_book_array_frame_populates_the_books():
    listener, store, queue = _listener()
    raw = json.dumps(
        [
            {
                "event_type": "book",
                "asset_id": "token-phi",
                "bids": [{"price": "0.67", "size": "100"}],
                "asks": [{"price": "0.69", "size": "100"}],
            },
            {
                "event_type": "book",
                "asset_id": "token-bkn",
                "bids": [{"price": "0.31", "size": "100"}],
                "asks": [{"price": "0.33", "size": "100"}],
            },
        ]
    )
    asyncio.run(listener.handle_market_frame(raw))
    assert listener.books.best_bid("token-phi") == 0.67
    assert listener.books.best_ask("token-phi") == 0.69
    assert listener.books.best_bid("token-bkn") == 0.31


def test_price_change_frame_uses_the_entry_asset_id_and_hints():
    listener, store, queue = _listener()
    raw = json.dumps(
        {
            "event_type": "price_change",
            "market": "0xmarket",
            "price_changes": [
                {
                    "asset_id": "token-phi",
                    "price": "0.61",
                    "size": "19190",
                    "side": "SELL",
                    "best_bid": "0.57",
                    "best_ask": "0.58",
                }
            ],
        }
    )
    asyncio.run(listener.handle_market_frame(raw))
    assert listener.books.best_bid("token-phi") == 0.57
    assert listener.books.best_ask("token-phi") == 0.58


def test_a_price_drop_fires_the_trigger_and_publishes():
    listener, store, queue = _listener()
    raw = json.dumps(
        {
            "event_type": "price_change",
            "price_changes": [
                {
                    "asset_id": "token-phi",
                    "price": "0.30",
                    "size": "500",
                    "side": "BUY",
                    "best_bid": "0.30",
                    "best_ask": "0.32",
                }
            ],
        }
    )
    asyncio.run(listener.handle_market_frame(raw))

    assert len(queue.published) == 1
    message = queue.published[0]
    assert message["type"] == "trigger"
    assert message["slug"] == "nba-phi-bkn-2026-11-16"
    assert message["token_id"] == "token-phi"
    assert message["trigger_price"] == 0.30
    assert message["pre_game_mid"] == 0.68
    assert store.game["status"] == "triggered"


def test_a_stable_price_does_not_fire():
    listener, store, queue = _listener()
    raw = json.dumps(
        {
            "event_type": "price_change",
            "price_changes": [
                {
                    "asset_id": "token-phi",
                    "price": "0.60",
                    "size": "500",
                    "side": "BUY",
                    "best_bid": "0.60",
                    "best_ask": "0.62",
                }
            ],
        }
    )
    asyncio.run(listener.handle_market_frame(raw))
    assert queue.published == []
    assert store.game["status"] == "armed"


def test_the_opponent_token_never_fires_the_trigger():
    listener, store, queue = _listener()
    raw = json.dumps(
        {
            "event_type": "price_change",
            "price_changes": [
                {
                    "asset_id": "token-bkn",
                    "price": "0.05",
                    "size": "500",
                    "side": "BUY",
                    "best_bid": "0.05",
                    "best_ask": "0.07",
                }
            ],
        }
    )
    asyncio.run(listener.handle_market_frame(raw))
    assert queue.published == []


def test_a_game_fires_only_once():
    listener, store, queue = _listener()
    frame = json.dumps(
        {
            "event_type": "price_change",
            "price_changes": [
                {
                    "asset_id": "token-phi",
                    "price": "0.30",
                    "size": "500",
                    "side": "BUY",
                    "best_bid": "0.30",
                    "best_ask": "0.32",
                }
            ],
        }
    )
    asyncio.run(listener.handle_market_frame(frame))
    asyncio.run(listener.handle_market_frame(frame))
    assert len(queue.published) == 1


def test_unparseable_frames_are_ignored():
    listener, store, queue = _listener()
    asyncio.run(listener.handle_market_frame("not json"))
    asyncio.run(listener.handle_market_frame("PONG"))
    assert queue.published == []


def test_token_ids_are_deduplicated():
    listener, store, queue = _listener()
    listener.games = [_game(), _game(slug="other")]
    tokens = listener.token_ids()
    assert len(tokens) == len(set(tokens))
    assert set(tokens) == {"token-phi", "token-bkn"}


class TestListenerWindow:
    """The listener stops itself once the day's window has closed."""

    def test_closed_window_stops_the_listener(self) -> None:
        listener, store, queue = _listener()
        store.window = {"end_ts": int(time.time()) - 1}
        assert listener.window_closed() is True

    def test_open_window_keeps_the_listener_running(self) -> None:
        listener, store, queue = _listener()
        store.window = {"end_ts": int(time.time()) + 3600}
        assert listener.window_closed() is False

    def test_no_window_keeps_the_listener_running(self) -> None:
        # The window item is missing (the daily fetch has not run yet), so
        # the scale-down schedule is the backstop.
        listener, store, queue = _listener()
        store.window = None
        assert listener.window_closed() is False

    def test_an_unreadable_window_keeps_the_listener_running(self) -> None:
        listener, store, queue = _listener()

        def _boom(day: str):
            raise RuntimeError("DynamoDB is unhappy")

        store.get_schedule_window = _boom
        assert listener.window_closed() is False
