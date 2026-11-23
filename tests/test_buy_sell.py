"""Tests for the trade worker (buy on trigger, recovery sell on fill)."""

from typing import Any, Optional

import pytest

from src.trade.buy_sell import TradeWorker, _filled_size, _is_filled


class FakeStore:
    """In-memory stand-in for DynamoStore."""

    def __init__(self, game: Optional[dict[str, Any]] = None):
        self.game = game
        self.trades: list[dict[str, Any]] = []
        self.logs: list[tuple[str, str, dict[str, Any]]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def get_game(self, slug: str) -> Optional[dict[str, Any]]:
        return self.game if self.game and self.game.get("slug") == slug else None

    def put_trade(self, slug: str, trade: dict[str, Any], **_: Any) -> str:
        self.trades.append(trade)
        return f"TRADE#{trade.get('ts')}"

    def put_log(self, slug: str, message: str, fields: Optional[dict[str, Any]] = None) -> str:
        self.logs.append((slug, message, fields or {}))
        return f"LOG#{message}"

    def update_game(self, slug: str, updates: dict[str, Any]) -> bool:
        self.updates.append((slug, updates))
        if self.game:
            self.game.update(updates)
        return True


class FakeExecutor:
    """Records orders instead of placing them."""

    def __init__(self, buy_result: Optional[dict[str, Any]] = None):
        self.buys: list[dict[str, Any]] = []
        self.sells: list[dict[str, Any]] = []
        self.buy_result = buy_result or {}
        self.orders: dict[str, dict[str, Any]] = {}

    def buy_team(self, **kwargs: Any) -> dict[str, Any]:
        self.buys.append(kwargs)
        result = dict(self.buy_result)
        result.setdefault("dry_run", True)
        result.setdefault("status", "simulated")
        result.setdefault("order_id", "order-1")
        result.setdefault("price", kwargs.get("price"))
        return result

    def sell_team(self, **kwargs: Any) -> dict[str, Any]:
        self.sells.append(kwargs)
        return {"dry_run": True, "status": "simulated", "order_id": "sell-1", "price": kwargs.get("price")}

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        return self.orders.get(order_id)


class FakeClob:
    def __init__(self, book: Optional[dict[str, Any]] = None):
        self.book = book if book is not None else {"best_bid": 0.29, "best_ask": 0.31}
        self.requested: list[str] = []

    def get_bid_ask(self, token_id: str) -> dict[str, Any]:
        self.requested.append(token_id)
        return self.book


class FakeChecker:
    def __init__(self, buy=False, sell=False, position=False):
        self.buy = buy
        self.sell = sell
        self.position = position

    def game_has_open_buy_order(self, token_ids: list[str]):
        return self.buy, []

    def token_has_open_sell_order(self, token_id: str):
        return self.sell, []

    def game_has_position(self, token_ids: list[str]):
        return self.position, []


def _game(**overrides: Any) -> dict[str, Any]:
    game = {
        "slug": "nba-phi-bkn-2026-11-16",
        "status": "triggered",
        "team_tokens": {
            "PHI": {"token_id": "token-phi"},
            "BKN": {"token_id": "token-bkn"},
        },
        "monitored_pre_game_mid": 0.68,
        "market_id": "200",
        "tick_size": "0.001",
        "neg_risk": False,
        "away_team": "76ers",
        "home_team": "Nets",
    }
    game.update(overrides)
    return game


_MISSING = object()


def _worker(
    *,
    game: Any = _MISSING,
    executor: Optional[FakeExecutor] = None,
    clob: Optional[FakeClob] = None,
    checker: Optional[FakeChecker] = None,
) -> tuple[TradeWorker, FakeStore, FakeExecutor]:
    resolved_game = _game() if game is _MISSING else game
    store = FakeStore(resolved_game)
    exec_ = executor or FakeExecutor()
    worker = TradeWorker(
        store=store,
        executor=exec_,
        clob=clob or FakeClob(),
        dry_run=True,
    )
    # Always stub the live checkers: they would otherwise hit the CLOB API.
    worker._order_checker = checker or FakeChecker()
    worker._position_checker = checker or FakeChecker()
    return worker, store, exec_


def _trigger_message(**overrides: Any) -> dict[str, Any]:
    message = {
        "type": "trigger",
        "slug": "nba-phi-bkn-2026-11-16",
        "team": "76ers",
        "token_id": "token-phi",
        "trigger_price": 0.34,
        "pre_game_mid": 0.68,
        "size": 5,
    }
    message.update(overrides)
    return message


def test_trigger_buys_at_the_best_ask():
    worker, store, exec_ = _worker()
    result = worker.execute_trigger(_trigger_message())
    assert result["status"] == "simulated"
    assert len(exec_.buys) == 1
    assert exec_.buys[0]["price"] == 0.31
    assert exec_.buys[0]["token_id"] == "token-phi"


def test_trigger_places_the_recovery_sell_at_the_pre_game_mid():
    worker, store, exec_ = _worker()
    worker.execute_trigger(_trigger_message())
    assert len(exec_.sells) == 1
    assert exec_.sells[0]["price"] == 0.68
    assert exec_.sells[0]["size"] == 5
    assert exec_.sells[0]["token_id"] == "token-phi"


def test_trigger_records_a_trade_and_a_log():
    worker, store, exec_ = _worker()
    worker.execute_trigger(_trigger_message())
    assert len(store.trades) == 1
    assert store.trades[0]["buy_price"] == 0.31
    assert store.trades[0]["sell_price"] == 0.68
    assert any("trade executed" in message for _, message, _ in store.logs)


def test_trigger_updates_the_game_status():
    worker, store, exec_ = _worker()
    worker.execute_trigger(_trigger_message())
    assert any(updates.get("status") == "traded" for _, updates in store.updates)


def test_trigger_skips_when_an_open_buy_order_exists():
    worker, store, exec_ = _worker(checker=FakeChecker(buy=True))
    result = worker.execute_trigger(_trigger_message())
    assert result["status"] == "skipped"
    assert result["reason"] == "open_buy_order"
    assert not exec_.buys


def test_trigger_skips_when_an_open_sell_order_exists():
    worker, store, exec_ = _worker(checker=FakeChecker(sell=True))
    result = worker.execute_trigger(_trigger_message())
    assert result["reason"] == "open_sell_order"
    assert not exec_.buys


def test_trigger_skips_when_a_position_is_held():
    worker, store, exec_ = _worker(checker=FakeChecker(position=True))
    result = worker.execute_trigger(_trigger_message())
    assert result["reason"] == "has_position"
    assert not exec_.buys


def test_trigger_skips_when_the_game_is_missing():
    worker, store, exec_ = _worker(game=None)
    result = worker.execute_trigger(_trigger_message())
    assert result["reason"] == "game_not_found"


def test_trigger_skips_when_the_game_status_is_wrong():
    worker, store, exec_ = _worker(game=_game(status="scheduled"))
    result = worker.execute_trigger(_trigger_message())
    assert result["reason"] == "status_scheduled"


def test_trigger_skips_without_a_best_ask():
    worker, store, exec_ = _worker(clob=FakeClob({"best_bid": 0.29, "best_ask": None}))
    result = worker.execute_trigger(_trigger_message())
    assert result["reason"] == "no_best_ask"
    assert not exec_.buys


def test_trigger_skips_without_a_pre_game_mid():
    worker, store, exec_ = _worker(game=_game(monitored_pre_game_mid=None))
    result = worker.execute_trigger(_trigger_message(pre_game_mid=None))
    assert result["reason"] == "no_pre_game_mid"


def test_trigger_skips_without_a_slug_or_token():
    worker, store, exec_ = _worker()
    result = worker.execute_trigger(_trigger_message(token_id=""))
    assert result["reason"] == "missing_slug_or_token"


def test_unfilled_buy_records_no_sell():
    executor = FakeExecutor(buy_result={"dry_run": False, "status": "LIVE", "order_id": "x"})
    worker, store, exec_ = _worker(executor=executor)
    executor.orders["x"] = {"status": "CANCELED", "size_matched": "0"}
    result = worker.execute_trigger(_trigger_message())
    assert result["status"] == "buy_unfilled"
    assert not exec_.sells


def test_late_fill_places_the_recovery_sell():
    worker, store, exec_ = _worker()
    result = worker.execute_fill(
        {
            "type": "fill",
            "slug": "nba-phi-bkn-2026-11-16",
            "token_id": "token-phi",
            "team": "76ers",
            "size_matched": 5,
            "pre_game_mid": 0.68,
        }
    )
    assert result["status"] == "simulated"
    assert len(exec_.sells) == 1
    assert exec_.sells[0]["price"] == 0.68
    assert exec_.sells[0]["size"] == 5


def test_late_fill_without_size_is_skipped():
    worker, store, exec_ = _worker()
    result = worker.execute_fill(
        {
            "type": "fill",
            "slug": "nba-phi-bkn-2026-11-16",
            "token_id": "token-phi",
            "pre_game_mid": 0.68,
        }
    )
    assert result["reason"] == "missing_price_or_size"
    assert not exec_.sells


def test_handle_message_dispatches_by_type():
    worker, store, exec_ = _worker()
    result = worker.handle_message(_trigger_message())
    assert result["status"] == "simulated"


def test_is_filled_and_filled_size_helpers():
    assert _is_filled("MATCHED") is True
    assert _is_filled("filled") is True
    assert _is_filled("LIVE") is False
    assert _filled_size({"size_matched": "3"}, 5) == 3
    assert _filled_size({}, 5) == 5
