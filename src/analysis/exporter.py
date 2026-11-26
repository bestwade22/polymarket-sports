"""Build the dashboard view model from DynamoDB.

One GSI query per day returns the games, their trades and their logs.
The result is the exact JSON the dashboard API serves and the local
``dashboard-export`` command writes.

P&L has two parts:

* **realised** - the profit of trades whose recovery sell has filled;
* **mark-to-market** - the value of open positions at the live best bid.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from config.settings import EXPORT_DIR, settings, today_et
from src.api.dynamo_store import DynamoStore

logger = logging.getLogger(__name__)

META_SK = "META"


def _num(value: Any) -> Optional[float]:
    """Coerce a stored number to a float.

    DynamoDB returns every number as a ``Decimal``, which is not JSON
    serialisable - the dashboard API would emit ``"0.32"`` instead of
    ``0.32``. Every numeric field that leaves the exporter goes through here.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _num_or(value: Any, fallback: float = 0.0) -> float:
    number = _num(value)
    return fallback if number is None else number


def _prices_view(prices: Any) -> dict[str, Any]:
    """Normalise the per-team pre-game price snapshots to JSON-safe floats."""
    if not isinstance(prices, dict):
        return {}
    out: dict[str, Any] = {}
    for team, snapshot in prices.items():
        if not isinstance(snapshot, dict):
            continue
        out[str(team)] = {
            "role": snapshot.get("role"),
            "abbreviation": snapshot.get("abbreviation"),
            "token_id": snapshot.get("token_id"),
            "bid": _num(snapshot.get("bid")),
            "ask": _num(snapshot.get("ask")),
            "mid": _num(snapshot.get("mid")),
            "ts": _num(snapshot.get("ts")),
        }
    return out


def _split_items(items: list[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split a day's items into games, trades and logs."""
    games: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    for item in items:
        sk = str(item.get("sk") or "")
        if sk == META_SK:
            games.append(item)
        elif sk.startswith("TRADE#"):
            trades.append(item)
        elif sk.startswith("LOG#"):
            logs.append(item)
    games.sort(key=lambda g: str(g.get("start_time") or ""))
    trades.sort(key=lambda t: int(t.get("ts") or 0))
    logs.sort(key=lambda l: str(l.get("sk") or ""))
    return games, trades, logs


def _polymarket_url(slug: Any) -> str:
    """The Polymarket event page for a game's slug.

    The bot's slugs are the Gamma event slugs (``nba-det-orl-2026-11-24``),
    which map directly onto the public event URL.
    """
    text = str(slug or "").strip()
    if not text:
        return ""
    return f"https://polymarket.com/event/{text}"


def game_view(game: dict[str, Any]) -> dict[str, Any]:
    """A compact view of one game for the dashboard."""
    slug = str(game.get("slug") or "")
    return {
        "slug": game.get("slug"),
        "status": game.get("status"),
        "matchup": f"{game.get('away_team')} @ {game.get('home_team')}",
        "away_team": game.get("away_team"),
        "away_abbr": game.get("away_abbr"),
        "away_record": game.get("away_record"),
        "home_team": game.get("home_team"),
        "home_abbr": game.get("home_abbr"),
        "home_record": game.get("home_record"),
        "start_time": game.get("start_time"),
        "monitored_team": game.get("monitored_team"),
        "opponent_team": game.get("opponent_team"),
        "pre_game_mid": _num(game.get("monitored_pre_game_mid")),
        "pre_game_bid": _num(game.get("monitored_pre_game_bid")),
        "pre_game_ask": _num(game.get("monitored_pre_game_ask")),
        # When the pre-game snapshot was taken, and the lowest bid the
        # listener saw for the monitored team.
        "pre_game_snapshot_ts": _num(
            game.get("pre_game_snapshot_ts") or game.get("pre_game_snapshotted_at")
        ),
        "lowest_bid": _num(game.get("lowest_bid")),
        "lowest_bid_ts": _num(game.get("lowest_bid_ts")),
        "polymarket_url": _polymarket_url(slug),
        "trigger_threshold": _num(game.get("trigger_threshold")),
        "trigger_price": _num(game.get("trigger_price")),
        "triggered_at": _num(game.get("triggered_at")),
        "pre_game_prices": _prices_view(game.get("pre_game_prices")),
        "buy_price": _num(game.get("buy_price")),
        "buy_order_id": game.get("buy_order_id"),
        "sell_price": _num(game.get("sell_price")),
        "sell_order_id": game.get("sell_order_id"),
        "filled_size": _num(game.get("filled_size")),
        "away_standings": game.get("away_standings"),
        "home_standings": game.get("home_standings"),
        "open_interest": game.get("open_interest"),
    }


def trade_view(trade: dict[str, Any]) -> dict[str, Any]:
    """A compact view of one trade."""
    buy_price = _num(trade.get("buy_price"))
    sell_price = _num(trade.get("sell_price"))
    size = _num(trade.get("filled_size") or trade.get("size")) or 0
    edge = None
    if buy_price is not None and sell_price is not None:
        edge = round(sell_price - buy_price, 4)
    pnl = None
    if edge is not None:
        pnl = round(edge * size, 4)
    return {
        "slug": trade.get("slug"),
        "ts": _num(trade.get("ts")),
        "team": trade.get("team"),
        "matchup": trade.get("matchup"),
        "status": trade.get("status"),
        "reason": trade.get("reason"),
        "buy_price": buy_price,
        "sell_price": sell_price,
        "size": _num(trade.get("size")),
        "filled_size": _num(trade.get("filled_size")),
        "buy_order_id": trade.get("buy_order_id"),
        "sell_order_id": trade.get("sell_order_id"),
        "trigger_price": _num(trade.get("trigger_price")),
        "pre_game_mid": _num(trade.get("pre_game_mid")),
        "implied_edge": edge,
        "pnl": pnl,
        "dry_run": trade.get("dry_run"),
    }


def build_dashboard(
    day: Optional[str] = None,
    *,
    store: Optional[DynamoStore] = None,
    live_prices: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build the dashboard payload for one ET date.

    ``live_prices`` maps a slug to ``{"best_bid": ..., "best_ask": ...}`` and
    is used for the mark-to-market column. When it is absent, the mark-to-market
    value is omitted rather than guessed.
    """
    dynamo = store or DynamoStore()
    target = day or today_et().isoformat()

    items = dynamo.get_games_for_date(target)
    games, trades, logs = _split_items(items)

    trades_by_slug: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        trades_by_slug.setdefault(str(trade.get("slug")), []).append(trade)

    game_views = []
    total_realised = 0.0
    total_mark = 0.0

    for game in games:
        slug = str(game.get("slug"))
        view = game_view(game)
        slug_trades = trades_by_slug.get(slug, [])
        view["trade_count"] = len(slug_trades)
        view["trades"] = [trade_view(t) for t in slug_trades]

        realised = 0.0
        for trade in slug_trades:
            pnl = trade_view(trade)["pnl"]
            if pnl is not None and trade.get("status") in ("filled", "simulated"):
                realised += pnl
        view["realised_pnl"] = round(realised, 4)
        total_realised += realised

        # Mark-to-market: value open positions at the live best bid.
        mark = None
        if live_prices and slug in live_prices:
            bid = _num(live_prices[slug].get("best_bid"))
            buy_price = _num(view.get("buy_price"))
            filled = _num(view.get("filled_size"))
            if bid is not None and buy_price is not None and filled:
                mark = round((bid - buy_price) * filled, 4)
        if mark is not None:
            view["mark_to_market"] = mark
            total_mark += mark

        game_views.append(view)

    return {
        "date": target,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "games": game_views,
        "trades": [trade_view(t) for t in trades],
        "logs": [
            {
                "slug": l.get("slug"),
                "ts": _num(l.get("ts")),
                "message": l.get("message"),
                "fields": l.get("fields"),
            }
            for l in logs
        ],
        "summary": {
            "game_count": len(game_views),
            "trade_count": len(trades),
            "armed_count": sum(1 for g in game_views if g.get("status") == "armed"),
            "triggered_count": sum(1 for g in game_views if g.get("status") == "triggered"),
            "traded_count": sum(1 for g in game_views if g.get("status") == "traded"),
            "realised_pnl": round(total_realised, 4),
            "mark_to_market": round(total_mark, 4),
            "total_pnl": round(total_realised + total_mark, 4),
        },
        "config": {
            "price_drop_ratio": settings.price_drop_ratio,
            "share_count": settings.share_count,
            "pre_game_snapshot_minutes": settings.pre_game_snapshot_minutes,
            "dry_run": settings.dry_run,
        },
    }


def export_dashboard(
    target: Optional[date] = None, *, store: Optional[DynamoStore] = None
):
    """Write the dashboard payload for a date to a local JSON file."""
    import json

    day = (target or today_et()).isoformat()
    payload = build_dashboard(day, store=store)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"dashboard_{day}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Dashboard export written to %s", path)
    return path
