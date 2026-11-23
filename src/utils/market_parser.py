"""Parsing helpers for Polymarket payloads.

The Gamma sports payload encodes ``outcomes``, ``clobTokenIds`` and
``outcomePrices`` as JSON-encoded *strings*, and unlike the weather markets
the outcome names are team names ("76ers", "Nets") rather than "Yes"/"No".
"""

import json
from typing import Any, Optional


def parse_float(value: Any) -> Optional[float]:
    """Parse API/CLOB price fields; treat blank strings as missing."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def parse_json_field(value: Any) -> Any:
    """Decode a JSON-encoded string field, passing through non-strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return []
    return value


def parse_list_field(value: Any) -> list[Any]:
    """Decode a field that should be a list (JSON-encoded string or real list)."""
    parsed = parse_json_field(value)
    return parsed if isinstance(parsed, list) else []


def get_tick_size(market: dict) -> str:
    """Order price tick size as a string, defaulting to 0.001."""
    tick = market.get("orderPriceMinTickSize")
    if tick is not None:
        return str(tick)
    if market.get("orderPriceMinTickSize") is None and market.get("tick_size"):
        return str(market.get("tick_size"))
    return "0.001"


def get_order_min_size(market: dict) -> int:
    """Minimum order size in shares."""
    raw = market.get("orderMinSize")
    try:
        return int(raw) if raw is not None else 5
    except (TypeError, ValueError):
        return 5


def is_neg_risk(market: dict) -> bool:
    return bool(market.get("negRisk", False))


def get_spread(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    """Bid-ask spread, or None when either side is missing / crossed."""
    if best_bid is None or best_ask is None or best_ask < best_bid:
        return None
    return round(best_ask - best_bid, 4)


def mid_from_bid_ask(
    best_bid: Optional[float], best_ask: Optional[float]
) -> Optional[float]:
    """Midpoint, falling back to whichever side is known."""
    if best_bid is not None and best_ask is not None:
        return (best_bid + best_ask) / 2.0
    if best_bid is not None:
        return best_bid
    return best_ask
