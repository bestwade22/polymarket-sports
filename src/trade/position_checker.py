"""Detect live CLOB share holdings.

Adapted from ``polymarket-trader/src/trade/position_checker.py``. For NBA
moneyline markets the holding we care about is the balance of the *team's*
conditional token.
"""

import logging
from typing import Any, Optional

from config.settings import settings
from src.trade.executor import TradeExecutor, _import_clob
from src.utils.market_parser import parse_float

logger = logging.getLogger(__name__)

CONDITIONAL_TOKEN_DECIMALS = 6
MIN_POSITION_SHARES = 0.01


def parse_conditional_balance(response: dict) -> float:
    """CLOB conditional token balance in shares."""
    raw = response.get("balance")
    if raw is None:
        return 0.0
    amount = parse_float(raw)
    if amount is None:
        return 0.0
    return amount / (10**CONDITIONAL_TOKEN_DECIMALS)


class LivePositionChecker:
    """Detect live share holdings for a team token."""

    def __init__(self, executor: TradeExecutor):
        self.executor = executor
        self._balance_cache: dict[str, Optional[float]] = {}

    def get_balance(self, token_id: str) -> Optional[float]:
        """Return shares held for a token, or None when live lookup fails."""
        if not token_id or not settings.private_key:
            return None
        if token_id in self._balance_cache:
            return self._balance_cache[token_id]
        try:
            version, _, _, _, _, _, _, _ = _import_clob()
            client = self.executor._get_client()
            if version == "v2":
                from py_clob_client_v2 import AssetType, BalanceAllowanceParams

                params = BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=str(token_id),
                )
            else:
                from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

                params = BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=str(token_id),
                )
            response = client.get_balance_allowance(params)
            if not isinstance(response, dict):
                logger.warning(
                    "Unexpected balance response for token %s: %r", token_id, response
                )
                self._balance_cache[token_id] = None
                return None
            balance = parse_conditional_balance(response)
            self._balance_cache[token_id] = balance
            return balance
        except Exception as exc:
            logger.warning("Live position check failed for token %s: %s", token_id, exc)
            self._balance_cache[token_id] = None
            return None

    def has_position(
        self, token_id: str, min_shares: float = MIN_POSITION_SHARES
    ) -> tuple[bool, Optional[float]]:
        balance = self.get_balance(token_id)
        if balance is None:
            return False, None
        return balance >= min_shares, balance

    def game_has_position(
        self,
        token_ids: list[str],
        min_shares: float = MIN_POSITION_SHARES,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """True when the wallet holds any shares on one of the game's tokens."""
        holdings: list[dict[str, Any]] = []
        for token_id in token_ids:
            if not token_id:
                continue
            balance = self.get_balance(token_id)
            if balance is None or balance < min_shares:
                continue
            holdings.append({"token_id": str(token_id), "balance": balance})
        return bool(holdings), holdings
