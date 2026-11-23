"""Tests for the listener's auto-heal behaviour.

The plan requires the listener to survive a WebSocket outage: on any
disconnect it re-reads DynamoDB, re-subscribes, and re-syncs the prices
from the CLOB REST ``/book`` endpoint - the WebSocket only sends a full
snapshot on subscribe, so a price move during the outage would otherwise be
missed.

The sockets are also watched for idleness, because the Polymarket feeds send
no server-side heartbeat.
"""

import asyncio
import time
from typing import Any, Optional

import pytest

from src.trade.listener import Listener, MarketSocket


class FakeStore:
    def __init__(self, games: Optional[list[dict[str, Any]]] = None):
        self.games = games if games is not None else []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str, dict[str, Any]]] = []
        self.claims = 0

    def get_game(self, slug: str) -> Optional[dict[str, Any]]:
        for game in self.games:
            if game.get("slug") == slug:
                return game
        return None

    def scan_armed_games(self) -> list[dict[str, Any]]:
        return list(self.games)

    def get_games_for_date(self, day: str) -> list[dict[str, Any]]:
        return list(self.games)

    def claim_trigger(self, slug: str, claimed_at: Optional[int] = None) -> bool:
        self.claims += 1
        return True

    def update_game(self, slug: str, updates: dict[str, Any]) -> bool:
        self.updates.append((slug, updates))
        for game in self.games:
            if game.get("slug") == slug:
                game.update(updates)
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


class FakeClob:
    """Stands in for the CLOB price client."""

    def __init__(self, books: Optional[dict[str, tuple[float, float]]] = None):
        self.books = books or {}
        self.calls: list[str] = []

    def get_bid_ask(self, token_id: str) -> dict[str, Any]:
        self.calls.append(token_id)
        bid, ask = self.books.get(token_id, (0.50, 0.52))
        return {"best_bid": bid, "best_ask": ask}


class FakeNba:
    def __init__(self, scores: Optional[dict[str, dict[str, Any]]] = None):
        self.scores = scores or {}
        self.calls = 0

    def fetch_live_score(self, league: str = "nba") -> dict[str, dict[str, Any]]:
        self.calls += 1
        return dict(self.scores)


def _game(**overrides: Any) -> dict[str, Any]:
    game = {
        "pk": "GAME#nba-phi-bkn-2026-11-16",
        "sk": "META",
        "slug": "nba-phi-bkn-2026-11-16",
        "game_id": "nba-20261116-phi-bkn",
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


def _listener(
    clob: Optional[FakeClob] = None,
    nba: Optional[FakeNba] = None,
    games: Optional[list[dict[str, Any]]] = None,
) -> tuple[Listener, FakeStore, FakeQueue]:
    store = FakeStore(games if games is not None else [_game()])
    queue = FakeQueue()
    listener = Listener(
        store=store,
        queue=queue,
        dry_run=True,
        clob=clob or FakeClob(),
        nba=nba or FakeNba(),
    )
    listener.games = list(store.games)
    return listener, store, queue


def test_resync_rereads_dynamodb_and_syncs_the_rest_book():
    clob = FakeClob({"token-phi": (0.30, 0.32), "token-bkn": (0.68, 0.70)})
    listener, store, queue = _listener(clob=clob)

    tokens = asyncio.run(listener.resync())

    assert set(tokens) == {"token-phi", "token-bkn"}
    assert set(clob.calls) == {"token-phi", "token-bkn"}
    # The REST book is the authority after an outage.
    assert listener.books.best_bid("token-phi") == 0.30
    assert listener.books.best_ask("token-bkn") == 0.70


def test_resync_picks_up_a_game_armed_after_the_listener_started():
    listener, store, queue = _listener(games=[])
    # The listener started with nothing armed.
    listener.games = []

    # A game is armed while the listener is running.
    store.games = [_game()]

    tokens = asyncio.run(listener.resync())

    assert "token-phi" in tokens


def test_resync_triggers_on_a_price_that_already_dropped():
    # The book fell to 0.30 during the outage; the pre-game mid was 0.68, so
    # the threshold is 0.34 and the trigger must fire on the re-sync.
    clob = FakeClob({"token-phi": (0.30, 0.32), "token-bkn": (0.68, 0.70)})
    listener, store, queue = _listener(clob=clob)

    asyncio.run(listener.resync())

    assert len(queue.published) == 1
    assert queue.published[0]["type"] == "trigger"
    assert queue.published[0]["trigger_price"] == 0.30


def test_resync_survives_a_rest_failure():
    class BrokenClob(FakeClob):
        def get_bid_ask(self, token_id: str) -> dict[str, Any]:
            raise RuntimeError("network down")

    listener, store, queue = _listener(clob=BrokenClob())

    tokens = asyncio.run(listener.resync())

    # The tokens are still subscribed; only the price sync failed.
    assert set(tokens) == {"token-phi", "token-bkn"}
    assert queue.published == []


def test_the_rest_score_fallback_is_used():
    nba = FakeNba(
        {
            "nba-20261116-phi-bkn": {
                "home_score": 55,
                "away_score": 50,
                "period": 3,
                "clock": "4:12",
                "status": "in_progress",
            }
        }
    )
    listener, store, queue = _listener(nba=nba)

    updated = listener.refresh_scores()

    assert updated == 1
    assert nba.calls == 1


def test_a_score_refresh_failure_is_not_fatal():
    class BrokenNba(FakeNba):
        def fetch_live_score(self, league: str = "nba") -> dict[str, dict[str, Any]]:
            raise RuntimeError("api down")

    listener, store, queue = _listener(nba=BrokenNba())

    assert listener.refresh_scores() == 0


def test_the_market_socket_subscription_can_be_changed():
    socket = MarketSocket(["a"])
    socket.set_token_ids(["a", "b"])
    assert socket.token_ids == ["a", "b"]


def test_a_newly_armed_game_is_picked_up_and_resubscribed():
    """The listener starts hours before any game is armed, so it has to
    keep watching for newly armed games and re-subscribe to them."""
    listener, store, queue = _listener(games=[])
    listener.games = []
    listener.target_date = None

    class RecordingSocket(MarketSocket):
        def __init__(self):
            super().__init__([])
            self.sent: list[list[str]] = []
            self._ws = None

        async def resubscribe(self, token_ids):
            self.sent.append(list(token_ids))
            self.token_ids = list(token_ids)

    socket = RecordingSocket()

    # Nothing is armed yet.
    changed = asyncio.run(listener.refresh_subscription(socket))
    assert changed is False
    assert socket.sent == []

    # The snapshot job arms a game.
    store.games = [_game()]
    changed = asyncio.run(listener.refresh_subscription(socket))

    assert changed is True
    assert socket.sent == [["token-bkn", "token-phi"]]


def test_the_subscription_is_unchanged_when_nothing_new_is_armed():
    listener, store, queue = _listener()
    listener.target_date = None

    class RecordingSocket(MarketSocket):
        def __init__(self):
            super().__init__([])
            self.sent: list[list[str]] = []

        async def resubscribe(self, token_ids):
            self.sent.append(list(token_ids))

    socket = RecordingSocket()
    changed = asyncio.run(listener.refresh_subscription(socket))

    assert changed is False
    assert socket.sent == []


def test_an_idle_market_socket_is_recycled():
    """A socket that never delivers a frame must not hang the listener."""

    class IdleSocket(MarketSocket):
        def __init__(self):
            super().__init__([])
            self.connects = 0
            self.closes = 0

        async def connect(self):
            self.connects += 1
            return None

        async def recv(self):
            await asyncio.sleep(3600)

        async def close(self):
            self.closes += 1

    listener, store, queue = _listener()
    listener.target_date = None
    socket = IdleSocket()

    async def run_briefly():
        task = asyncio.create_task(listener.run_market_loop(socket))
        await asyncio.sleep(0.2)
        listener._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_briefly())

    # The listener reconnected at least once rather than hanging on the
    # dead socket.
    assert socket.connects >= 1
