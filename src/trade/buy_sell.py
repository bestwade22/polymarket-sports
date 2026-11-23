"""Buy on trigger, then place the recovery sell.

The trade worker consumes SQS messages produced by the listener:

``trigger``
    The authoritative pre-trade checks live **here**, closest to the trade,
    because a price tick can be stale by the time the worker runs:

    1. any open BUY order in this game? -> skip;
    2. an open SELL order on this token? -> skip;
    3. already holding shares of this token? -> skip;
    4. refresh the live book, buy at the **best ask** for ``SHARE_COUNT``
       shares with a ``GTD`` order expiring in ``BUY_ORDER_EXPIRY_MINUTES``;
    5. poll the order until it fills or times out (~30 s);
    6. on fill, place a ``GTC`` sell for the filled shares at the original
       pre-game price - the recovery order. It has no expiry, so it survives
       until the market resolves;
    7. write the trade and log records to DynamoDB.

``fill``
    The late-fill path: a fill arrived on the user socket after the worker
    had already moved on. Place the recovery sell for the filled shares.
"""

import logging
import time
from typing import Any, Optional

from config.settings import settings
from src.api.clob_client import ClobPriceClient
from src.api.dynamo_store import DynamoStore
from src.api.queue_client import FILL_MESSAGE, TRIGGER_MESSAGE, TradeQueue
from src.trade.executor import TradeExecutor
from src.trade.open_order_checker import LiveOpenOrderChecker
from src.trade.position_checker import LivePositionChecker

logger = logging.getLogger(__name__)

# How long to wait for the buy order to fill.
FILL_POLL_SECONDS = 30
FILL_POLL_INTERVAL = 2.0

# Order statuses that mean the order is done.
TERMINAL_STATUSES = {"MATCHED", "FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


def _is_filled(status: Any) -> bool:
    return str(status or "").upper() in ("MATCHED", "FILLED")


def _filled_size(order: dict[str, Any], fallback: float) -> float:
    """The filled size of an order, falling back to the requested size."""
    for key in ("size_matched", "matched_size", "filled_size", "original_size"):
        raw = order.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return fallback


class TradeWorker:
    """Executes trades from SQS messages."""

    def __init__(
        self,
        *,
        store: Optional[DynamoStore] = None,
        queue: Optional[TradeQueue] = None,
        executor: Optional[TradeExecutor] = None,
        clob: Optional[ClobPriceClient] = None,
        dry_run: Optional[bool] = None,
    ):
        self.store = store or DynamoStore()
        self.queue = queue or TradeQueue()
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.executor = executor or TradeExecutor(dry_run=self.dry_run)
        self.clob = clob or ClobPriceClient()
        self._order_checker: Optional[LiveOpenOrderChecker] = None
        self._position_checker: Optional[LivePositionChecker] = None

    # --- checkers ----------------------------------------------------------

    @property
    def order_checker(self) -> LiveOpenOrderChecker:
        if self._order_checker is None:
            self._order_checker = LiveOpenOrderChecker(self.executor)
        return self._order_checker

    @property
    def position_checker(self) -> LivePositionChecker:
        if self._position_checker is None:
            self._position_checker = LivePositionChecker(self.executor)
        return self._position_checker

    # --- pre-trade checks --------------------------------------------------

    def pre_trade_checks(
        self, game: dict[str, Any], token_id: str
    ) -> tuple[bool, str]:
        """Run the "no buy / no sell / no holding" checks.

        Returns ``(passed, reason)``.
        """
        game_tokens = [
            str(entry.get("token_id"))
            for entry in (game.get("team_tokens") or {}).values()
            if entry.get("token_id")
        ]

        has_buy, buy_orders = self.order_checker.game_has_open_buy_order(game_tokens)
        if has_buy:
            return False, "open_buy_order"

        has_sell, _sell_orders = self.order_checker.token_has_open_sell_order(token_id)
        if has_sell:
            return False, "open_sell_order"

        has_position, holdings = self.position_checker.game_has_position(game_tokens)
        if has_position:
            return False, "has_position"

        return True, "ok"

    # --- the trade ---------------------------------------------------------

    def execute_trigger(self, message: dict[str, Any]) -> dict[str, Any]:
        """Run the full buy-then-recover flow for a trigger message."""
        slug = str(message.get("slug") or "")
        team = str(message.get("team") or "")
        token_id = str(message.get("token_id") or "")
        game = self.store.get_game(slug) if slug else None

        base = {
            "slug": slug,
            "team": team,
            "status": "skipped",
            "reason": "",
            "trigger_price": message.get("trigger_price"),
            "pre_game_mid": message.get("pre_game_mid"),
            "buy_price": None,
            "sell_price": None,
            "size": message.get("size") or settings.share_count,
            "buy_order_id": None,
            "sell_order_id": None,
            "dry_run": self.dry_run,
        }

        if not slug or not token_id:
            base["reason"] = "missing_slug_or_token"
            return base

        if game is None:
            base["reason"] = "game_not_found"
            self._log(slug, "trade skipped", base)
            return base

        if game.get("status") not in ("triggered", "armed"):
            base["reason"] = f"status_{game.get('status')}"
            self._log(slug, "trade skipped", base)
            return base

        passed, reason = self.pre_trade_checks(game, token_id)
        if not passed:
            base["reason"] = reason
            self._log(slug, "trade skipped", base)
            logger.info("Trade skipped for %s: %s", slug, reason)
            return base

        # Refresh the live book: the tick that fired the trigger may be stale.
        live = self.clob.get_bid_ask(token_id)
        best_ask = live.get("best_ask")
        best_bid = live.get("best_bid")
        if best_ask is None:
            base["reason"] = "no_best_ask"
            self._log(slug, "trade skipped", base)
            return base

        pre_game_mid = game.get("monitored_pre_game_mid") or message.get("pre_game_mid")
        if pre_game_mid is None:
            base["reason"] = "no_pre_game_mid"
            self._log(slug, "trade skipped", base)
            return base

        size = float(base["size"])
        tick_size = str(game.get("tick_size") or message.get("tick_size") or "0.001")
        neg_risk = bool(game.get("neg_risk", message.get("neg_risk", False)))

        # --- buy ---------------------------------------------------------
        buy = self.executor.buy_team(
            token_id=token_id,
            price=float(best_ask),
            size=size,
            tick_size=tick_size,
            neg_risk=neg_risk,
            game_slug=slug,
            market_id=str(game.get("market_id") or ""),
            team=team,
        )
        base["buy_price"] = buy.get("price")
        base["buy_order_id"] = buy.get("order_id")
        base["best_bid"] = best_bid
        base["buy_status"] = buy.get("status")

        filled_size = self._await_fill(buy, size)
        if filled_size <= 0:
            base["reason"] = "buy_not_filled"
            base["status"] = "buy_unfilled"
            self._record_trade(slug, base, game)
            self._log(slug, "buy did not fill", base)
            logger.warning("Buy order for %s did not fill", slug)
            return base

        # --- recovery sell ------------------------------------------------
        sell = self.executor.sell_team(
            token_id=token_id,
            price=float(pre_game_mid),
            size=filled_size,
            tick_size=tick_size,
            neg_risk=neg_risk,
            game_slug=slug,
            market_id=str(game.get("market_id") or ""),
            team=team,
        )
        base["sell_price"] = sell.get("price")
        base["sell_order_id"] = sell.get("order_id")
        base["sell_status"] = sell.get("status")
        base["filled_size"] = filled_size
        base["status"] = "filled" if not self.dry_run else "simulated"

        self.store.update_game(
            slug,
            {
                "status": "traded",
                "buy_price": base["buy_price"],
                "buy_order_id": base["buy_order_id"],
                "sell_price": base["sell_price"],
                "sell_order_id": base["sell_order_id"],
                "filled_size": filled_size,
                "traded_at": int(time.time()),
            },
        )
        self._record_trade(slug, base, game)
        self._log(slug, "trade executed", base)
        logger.warning(
            "Trade executed for %s: bought %s shares of %s at %s, recovery sell at %s",
            slug,
            filled_size,
            team,
            base["buy_price"],
            base["sell_price"],
        )

        try:
            from src.notify.telegram import notify_trade

            notify_trade(base)
        except Exception:
            logger.exception("Telegram trade notification failed")

        return base

    def _await_fill(self, buy: dict[str, Any], requested_size: float) -> float:
        """Poll the buy order until it fills or the wait times out.

        In dry run the order is simulated, so it counts as filled immediately.
        """
        if buy.get("dry_run"):
            return requested_size

        order_id = buy.get("order_id")
        if not order_id:
            return 0.0

        deadline = time.time() + FILL_POLL_SECONDS
        while time.time() < deadline:
            order = self.executor.get_order(str(order_id))
            if order:
                status = str(order.get("status") or "")
                if _is_filled(status):
                    return _filled_size(order, requested_size)
                if str(status).upper() in TERMINAL_STATUSES:
                    return 0.0
            time.sleep(FILL_POLL_INTERVAL)
        return 0.0

    # --- late fills --------------------------------------------------------

    def execute_fill(self, message: dict[str, Any]) -> dict[str, Any]:
        """Place the recovery sell for a late fill."""
        slug = str(message.get("slug") or "")
        token_id = str(message.get("token_id") or "")
        game = self.store.get_game(slug) if slug else None

        base = {
            "slug": slug,
            "team": message.get("team"),
            "status": "skipped",
            "reason": "",
            "sell_price": None,
            "sell_order_id": None,
            "size": message.get("size_matched"),
            "dry_run": self.dry_run,
        }

        if not slug or not token_id or game is None:
            base["reason"] = "missing_context"
            return base

        pre_game_mid = message.get("pre_game_mid") or game.get("monitored_pre_game_mid")
        size = message.get("size_matched") or game.get("filled_size")
        if pre_game_mid is None or not size:
            base["reason"] = "missing_price_or_size"
            self._log(slug, "late fill skipped", base)
            return base

        sell = self.executor.sell_team(
            token_id=token_id,
            price=float(pre_game_mid),
            size=float(size),
            tick_size=str(game.get("tick_size") or "0.001"),
            neg_risk=bool(game.get("neg_risk", False)),
            game_slug=slug,
            market_id=str(game.get("market_id") or ""),
            team=str(message.get("team") or ""),
        )
        base["sell_price"] = sell.get("price")
        base["sell_order_id"] = sell.get("order_id")
        base["status"] = "filled" if not self.dry_run else "simulated"
        self._log(slug, "late fill recovery sell placed", base)
        return base

    # --- records -----------------------------------------------------------

    def _record_trade(self, slug: str, result: dict[str, Any], game: dict[str, Any]) -> None:
        """Write the TRADE item for a completed trade."""
        trade = {
            "ts": int(time.time()),
            "slug": slug,
            "team": result.get("team"),
            "status": result.get("status"),
            "reason": result.get("reason"),
            "buy_price": result.get("buy_price"),
            "sell_price": result.get("sell_price"),
            "size": result.get("size"),
            "filled_size": result.get("filled_size"),
            "buy_order_id": result.get("buy_order_id"),
            "sell_order_id": result.get("sell_order_id"),
            "trigger_price": result.get("trigger_price"),
            "pre_game_mid": result.get("pre_game_mid"),
            "matchup": f"{game.get('away_team')} @ {game.get('home_team')}",
            "dry_run": self.dry_run,
        }
        self.store.put_trade(slug, trade)

    def _log(self, slug: str, message: str, fields: dict[str, Any]) -> None:
        try:
            self.store.put_log(slug, message, fields)
        except Exception:
            logger.exception("Failed to write log for %s", slug)

    # --- SQS ---------------------------------------------------------------

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one SQS message body."""
        message_type = str(message.get("type") or TRIGGER_MESSAGE)
        if message_type == FILL_MESSAGE:
            return self.execute_fill(message)
        return self.execute_trigger(message)

    def run_once(self, *, wait_seconds: int = 10) -> Optional[dict[str, Any]]:
        """Process at most one SQS message."""
        messages = self.queue.receive(max_messages=1, wait_seconds=wait_seconds)
        if not messages:
            logger.info("No SQS messages to process")
            return None

        entry = messages[0]
        body = entry["body"]
        logger.info("Processing SQS message %s: %s", entry["message_id"], body.get("type"))

        try:
            result = self.handle_message(body)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Trade worker failed on message %s", entry["message_id"])
            # Do not delete the message so SQS redrives it.
            raise

        self.queue.delete(entry["receipt_handle"])
        return result

    def run_loop(self, *, wait_seconds: int = 10) -> None:
        """Poll SQS continuously."""
        logger.info("Trade worker started (loop mode)")
        while True:
            try:
                self.run_once(wait_seconds=wait_seconds)
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Trade worker iteration failed; continuing")


def run_trade_worker(
    *, loop: bool = False, dry_run: Optional[bool] = None, wait_seconds: int = 10
) -> Any:
    """Entry point for the trade worker."""
    worker = TradeWorker(dry_run=dry_run)
    if loop:
        worker.run_loop(wait_seconds=wait_seconds)
        return None
    return worker.run_once(wait_seconds=wait_seconds)
