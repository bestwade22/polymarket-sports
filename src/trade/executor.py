"""CLOB order executor for team-token markets.

Adapted from ``polymarket-trader/src/trade/executor.py``. The structural
difference for NBA moneyline markets is that the token we trade is the
*team's* token rather than the "Yes" token, so the ordering logic is
identical and only the token selection differs.

Two order shapes are used by this bot:

* the **buy** order is ``GTD``, expiring ``BUY_ORDER_EXPIRY_MINUTES`` after
  placement, so a stale trigger cannot fill hours later;
* the **recovery sell** is ``GTC`` at the original pre-game price, so it
  survives until the market resolves.
"""

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from typing import Any, Optional

from config.settings import CHAIN_ID, CLOB_HOST, settings

logger = logging.getLogger(__name__)

MIN_ORDER_PRICE = Decimal("0.001")
MAX_ORDER_PRICE = Decimal("0.999")

SIGNER_API_KEY_MISMATCH = (
    "Order signer address does not match the CLOB API key address. "
    "SIGNATURE_TYPE=3 (deposit wallet) is not supported by py-clob-client-v2 yet — "
    "the SDK binds API keys to your EOA but signs orders with DEPOSIT_WALLET_ADDRESS. "
    "If you use a Polymarket email/Magic wallet, set SIGNATURE_TYPE=1 and "
    "DEPOSIT_WALLET_ADDRESS to your Polymarket proxy wallet (profile address). "
    "If you trade from MetaMask directly, use SIGNATURE_TYPE=0 and leave "
    "DEPOSIT_WALLET_ADDRESS empty."
)


def _import_clob():
    """Import CLOB client (v2 preferred, v1 fallback)."""
    try:
        from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        try:
            from py_clob_client_v2 import SignatureTypeV2

            sig_map = {
                0: SignatureTypeV2.EOA,
                1: SignatureTypeV2.POLY_PROXY,
                2: SignatureTypeV2.POLY_GNOSIS_SAFE,
                3: SignatureTypeV2.POLY_1271,
            }
        except ImportError:
            sig_map = None

        return "v2", ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, BUY, SELL, sig_map
    except ImportError:
        pass

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY, SELL

        return "v1", ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, BUY, SELL, None
    except ImportError as exc:
        raise ImportError(
            "Install py-clob-client-v2 (Python >=3.9.10) or py-clob-client for live trading"
        ) from exc


def compute_order_expiration(
    expiry_hours: float,
    now_ts: Optional[int] = None,
) -> tuple[int, str]:
    """Return (unix_expiration, order_type). GTC when expiry_hours <= 0, else GTD."""
    if expiry_hours <= 0:
        return 0, "GTC"
    now = now_ts if now_ts is not None else int(time.time())
    return now + int(expiry_hours * 3600), "GTD"


def compute_order_expiration_at(expire_at_utc: datetime) -> tuple[int, str]:
    """Return (unix_expiration, order_type) for an absolute GTD cutoff."""
    return int(expire_at_utc.timestamp()), "GTD"


class TradeExecutor:
    """Place CLOB orders for a single team token."""

    def __init__(self, dry_run: Optional[bool] = None):
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        if not settings.private_key:
            raise ValueError("PRIVATE_KEY not configured")

        version, ClobClient, _, _, _, _, _, sig_map = _import_clob()

        if version == "v2":
            signature_type = (
                sig_map.get(settings.signature_type, sig_map[1]) if sig_map else None
            )
            kwargs = {
                "host": CLOB_HOST,
                "chain_id": CHAIN_ID,
                "key": settings.private_key,
            }
            if signature_type is not None:
                kwargs["signature_type"] = signature_type
            if settings.deposit_wallet_address:
                kwargs["funder"] = settings.deposit_wallet_address
            temp = ClobClient(**kwargs)
            creds = temp.create_or_derive_api_key()
            kwargs["creds"] = creds
            self._client = ClobClient(**kwargs)
            self._validate_signer_api_key_alignment()
        else:
            temp = ClobClient(CLOB_HOST, key=settings.private_key, chain_id=CHAIN_ID)
            creds = temp.create_or_derive_api_creds()
            self._client = ClobClient(
                CLOB_HOST,
                key=settings.private_key,
                chain_id=CHAIN_ID,
                creds=creds,
                signature_type=settings.signature_type,
                funder=settings.deposit_wallet_address or None,
            )
        return self._client

    def _validate_signer_api_key_alignment(self) -> None:
        """Fail fast when POLY_1271 would mismatch API key address (SDK issue #70)."""
        client = self._client
        if client is None or client.signer is None:
            return
        order_signer = client.builder._v2_order_signer()
        api_signer = client.signer.address()
        if order_signer.lower() != api_signer.lower():
            raise ValueError(SIGNER_API_KEY_MISMATCH)

    def place_order(
        self,
        *,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type_name: str,
        expiration: int,
        tick_size: str,
        neg_risk: bool,
        game_slug: str = "",
        market_id: str = "",
        team: str = "",
    ) -> dict[str, Any]:
        """Place a single order for a team token."""
        side = side.upper()
        expires_at = (
            datetime.fromtimestamp(expiration, tz=timezone.utc).isoformat()
            if expiration
            else None
        )

        if self.dry_run:
            result = {
                "dry_run": True,
                "status": "simulated",
                "side": side,
                "token_id": token_id,
                "price": price,
                "size": size,
                "market_id": market_id,
                "event_id": game_slug,
                "team": team,
                "order_type": order_type_name,
                "expiration": expiration,
                "expires_at": expires_at,
            }
            logger.info("DRY RUN %s: %s", side, result)
            return result

        version, _, OrderArgs, OrderType, PartialCreateOrderOptions, BUY, SELL, _ = _import_clob()
        client = self._get_client()
        order_type = OrderType.GTD if order_type_name == "GTD" else OrderType.GTC
        order_side = SELL if side == "SELL" else BUY
        safe_price = self._sanitize_order_price(
            order_price=price,
            tick_size=tick_size,
            market_id=market_id or token_id,
            side_name=side,
        )

        response = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=safe_price,
                size=size,
                side=order_side,
                expiration=expiration,
            ),
            options=PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=neg_risk,
            ),
            order_type=order_type,
        )

        logger.info("%s order placed: %s", side, response)
        return {
            "dry_run": False,
            "side": side,
            "order_id": response.get("orderID") or response.get("order_id"),
            "status": response.get("status"),
            "price": safe_price,
            "size": size,
            "market_id": market_id,
            "event_id": game_slug,
            "team": team,
            "order_type": order_type_name,
            "expiration": expiration,
            "expires_at": expires_at,
            "response": response,
        }

    def buy_team(
        self,
        *,
        token_id: str,
        price: float,
        size: float,
        tick_size: str,
        neg_risk: bool,
        expiry_minutes: Optional[int] = None,
        game_slug: str = "",
        market_id: str = "",
        team: str = "",
    ) -> dict[str, Any]:
        """Buy `size` shares of a team token with a GTD order."""
        minutes = settings.buy_order_expiry_minutes if expiry_minutes is None else expiry_minutes
        expiration, order_type_name = compute_order_expiration(minutes / 60.0)
        return self.place_order(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
            order_type_name=order_type_name,
            expiration=expiration,
            tick_size=tick_size,
            neg_risk=neg_risk,
            game_slug=game_slug,
            market_id=market_id,
            team=team,
        )

    def sell_team(
        self,
        *,
        token_id: str,
        price: float,
        size: float,
        tick_size: str,
        neg_risk: bool,
        expiry_hours: Optional[float] = None,
        game_slug: str = "",
        market_id: str = "",
        team: str = "",
    ) -> dict[str, Any]:
        """Sell `size` shares of a team token. GTC when expiry_hours <= 0."""
        hours = settings.sell_order_expiry_hours if expiry_hours is None else expiry_hours
        expiration, order_type_name = compute_order_expiration(hours)
        return self.place_order(
            token_id=token_id,
            price=price,
            size=size,
            side="SELL",
            order_type_name=order_type_name,
            expiration=expiration,
            tick_size=tick_size,
            neg_risk=neg_risk,
            game_slug=game_slug,
            market_id=market_id,
            team=team,
        )

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        """Fetch a single order by id, or None when unavailable."""
        if not order_id:
            return None
        try:
            version, _, _, _, _, _, _, _ = _import_clob()
            client = self._get_client()
            if version == "v2":
                response = client.get_order(order_id)
            else:  # pragma: no cover - v1 fallback
                response = client.get_order(order_id)
            return response if isinstance(response, dict) else None
        except Exception as exc:
            logger.warning("Failed to fetch order %s: %s", order_id, exc)
            return None

    def _sanitize_order_price(
        self,
        *,
        order_price: float,
        tick_size: str,
        market_id: str,
        side_name: str,
    ) -> float:
        """Clamp to the valid range and round down to the market's tick size."""
        raw = Decimal(str(order_price))
        step = self._parse_tick_size(tick_size)
        adjusted = max(MIN_ORDER_PRICE, min(MAX_ORDER_PRICE, raw))
        ticks = (adjusted / step).to_integral_value(rounding=ROUND_DOWN)
        normalized = ticks * step
        normalized = max(MIN_ORDER_PRICE, min(MAX_ORDER_PRICE, normalized))
        normalized_f = float(normalized)
        if abs(normalized_f - order_price) > 1e-12:
            logger.info(
                "Adjusted %s price for market %s from %.6f to %.6f (tick=%s, range=0.001-0.999)",
                side_name,
                market_id,
                order_price,
                normalized_f,
                step,
            )
        return normalized_f

    @staticmethod
    def _parse_tick_size(tick_size: str) -> Decimal:
        try:
            parsed = Decimal(str(tick_size))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0.001")
        if parsed <= 0:
            return Decimal("0.001")
        return parsed
