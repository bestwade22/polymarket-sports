"""Detect open CLOB orders that would block a new trade.

Adapted from ``polymarket-trader/src/trade/open_order_checker.py``. Instead of
scanning every market in a weather event, this bot checks a single game by its
moneyline token ids.
"""

import logging
from typing import Any, Optional

from config.settings import settings
from src.trade.executor import TradeExecutor, _import_clob

logger = logging.getLogger(__name__)


def _order_asset_id(order: dict[str, Any]) -> Optional[str]:
    raw = order.get("asset_id") or order.get("token_id") or order.get("tokenId")
    return str(raw) if raw is not None else None


def _is_buy_order(order: dict[str, Any]) -> bool:
    side = str(order.get("side", "")).upper()
    return side in ("BUY", "B", "0")


def _is_sell_order(order: dict[str, Any]) -> bool:
    side = str(order.get("side", "")).upper()
    return side in ("SELL", "S", "1")


class LiveOpenOrderChecker:
    """Detect open CLOB buy/sell orders for a game's tokens."""

    def __init__(self, executor: TradeExecutor):
        self.executor = executor
        self._orders: Optional[list[dict[str, Any]]] = None

    def get_open_orders(self) -> list[dict[str, Any]]:
        if self._orders is not None:
            return self._orders
        if not settings.private_key:
            self._orders = []
            return self._orders
        try:
            version, _, _, _, _, _, _, _ = _import_clob()
            client = self.executor._get_client()
            if version == "v2":
                orders = client.get_open_orders()
            else:
                orders = client.get_orders()  # type: ignore[attr-defined]
            self._orders = orders if isinstance(orders, list) else []
        except Exception as exc:
            logger.warning("Failed to fetch open orders: %s", exc)
            self._orders = []
        return self._orders

    def token_has_open_order(
        self, token_id: str, side: Optional[str] = None
    ) -> tuple[bool, list[dict[str, Any]]]:
        """True when the token has an open order (optionally filtered by side)."""
        if not token_id:
            return False, []
        matching: list[dict[str, Any]] = []
        for order in self.get_open_orders():
            asset_id = _order_asset_id(order)
            if not asset_id or asset_id != str(token_id):
                continue
            if side is not None:
                want_buy = side.upper() == "BUY"
                if want_buy and not _is_buy_order(order):
                    continue
                if not want_buy and not _is_sell_order(order):
                    continue
            matching.append(order)
        return bool(matching), matching

    def game_has_open_buy_order(
        self, token_ids: list[str]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """True when any open BUY order targets one of the game's tokens."""
        wanted = {str(t) for t in token_ids if t}
        if not wanted:
            return False, []
        matching: list[dict[str, Any]] = []
        for order in self.get_open_orders():
            asset_id = _order_asset_id(order)
            if asset_id and asset_id in wanted and _is_buy_order(order):
                matching.append(order)
        return bool(matching), matching

    def token_has_open_sell_order(self, token_id: str) -> tuple[bool, list[dict[str, Any]]]:
        """True when the token already has an open sell order."""
        return self.token_has_open_order(token_id, side="SELL")
