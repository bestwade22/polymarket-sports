"""Tests for the dashboard view-model builder."""

from typing import Any, Optional

from src.analysis.exporter import build_dashboard, game_view, trade_view


class FakeStore:
    def __init__(self, items: list[dict[str, Any]]):
        self.items = items

    def get_games_for_date(self, day: str) -> list[dict[str, Any]]:
        return list(self.items)


def _game_item(**overrides: Any) -> dict[str, Any]:
    item = {
        "sk": "META",
        "slug": "nba-phi-bkn-2026-11-16",
        "status": "traded",
        "away_team": "76ers",
        "home_team": "Nets",
        "away_abbr": "PHI",
        "home_abbr": "BKN",
        "start_time": "2026-11-17T00:30:00Z",
        "monitored_team": "76ers",
        "monitored_pre_game_mid": 0.68,
        "buy_price": 0.34,
        "sell_price": 0.68,
        "filled_size": 5,
    }
    item.update(overrides)
    return item


def _trade_item(**overrides: Any) -> dict[str, Any]:
    item = {
        "sk": "TRADE#1794875400#t1",
        "slug": "nba-phi-bkn-2026-11-16",
        "ts": 1794875400,
        "team": "76ers",
        "status": "filled",
        "buy_price": 0.34,
        "sell_price": 0.68,
        "filled_size": 5,
    }
    item.update(overrides)
    return item


def test_game_view_shape():
    view = game_view(_game_item())
    assert view["matchup"] == "76ers @ Nets"
    assert view["pre_game_mid"] == 0.68
    assert view["buy_price"] == 0.34


def test_trade_view_computes_edge_and_pnl():
    view = trade_view(_trade_item())
    assert view["implied_edge"] == 0.34
    assert view["pnl"] == 1.70


def test_trade_view_without_prices_has_no_pnl():
    view = trade_view(_trade_item(buy_price=None, sell_price=None))
    assert view["implied_edge"] is None
    assert view["pnl"] is None


def test_build_dashboard_splits_games_trades_and_logs():
    items = [
        _game_item(),
        _trade_item(),
        {
            "sk": "LOG#1794875400#0001",
            "slug": "nba-phi-bkn-2026-11-16",
            "ts": 1794875400,
            "message": "armed",
            "fields": {"monitored_team": "76ers"},
        },
    ]
    payload = build_dashboard("2026-11-16", store=FakeStore(items))
    assert payload["date"] == "2026-11-16"
    assert len(payload["games"]) == 1
    assert len(payload["trades"]) == 1
    assert len(payload["logs"]) == 1
    assert payload["games"][0]["trade_count"] == 1


def test_build_dashboard_summary_counts():
    items = [
        _game_item(),
        _game_item(slug="nba-lal-bos-2026-11-16", status="armed", away_team="Lakers"),
        _trade_item(),
    ]
    payload = build_dashboard("2026-11-16", store=FakeStore(items))
    summary = payload["summary"]
    assert summary["game_count"] == 2
    assert summary["trade_count"] == 1
    assert summary["armed_count"] == 1
    assert summary["traded_count"] == 1
    assert summary["realised_pnl"] == 1.70


def test_build_dashboard_marks_to_market():
    items = [_game_item(), _trade_item()]
    payload = build_dashboard(
        "2026-11-16",
        store=FakeStore(items),
        live_prices={"nba-phi-bkn-2026-11-16": {"best_bid": 0.5, "best_ask": 0.52}},
    )
    assert payload["games"][0]["mark_to_market"] == 0.80
    assert payload["summary"]["mark_to_market"] == 0.80
    assert payload["summary"]["total_pnl"] == 2.50


def test_build_dashboard_without_live_prices_omits_the_mark():
    items = [_game_item(), _trade_item()]
    payload = build_dashboard("2026-11-16", store=FakeStore(items))
    assert "mark_to_market" not in payload["games"][0]
    assert payload["summary"]["mark_to_market"] == 0


def test_build_dashboard_ignores_unfilled_trades_in_realised_pnl():
    items = [_game_item(), _trade_item(status="buy_unfilled")]
    payload = build_dashboard("2026-11-16", store=FakeStore(items))
    assert payload["summary"]["realised_pnl"] == 0
