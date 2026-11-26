"""The live WebSocket listener.

One process, three sockets:

* **CLOB market WebSocket** - the price feed. Subscribe to both teams'
  tokens. Handles ``book`` (full snapshot) and ``price_change`` (per-level
  updates, ``size == "0"`` removes a level). This is the only feed that
  can fire a trade.
* **Sports WebSocket** - live score and period. Broadcasts *every* league,
  so frames are filtered on ``leagueAbbreviation == "nba"``. Treated as
  optional: it closes abruptly and only emits for in-progress games, so
  nothing depends on it.
* **CLOB user WebSocket** - order fill notifications, so a fill that
  arrives after the worker has moved on is still noticed.

Operational notes measured against the live feed:

* the market socket sends a full ``book`` snapshot on subscribe, so no REST
  call is needed for the initial price - but the snapshot is only sent on
  *subscribe*, so a reconnect re-syncs from REST ``/book``;
* the sports socket sent **no** ``ping`` at all in 70 s of idle time, so
  the documented "reply to ping within 10 s" rule never fires in practice.
  A ``pong`` is still sent if one ever arrives, and a client-side idle
  watchdog handles a genuinely dead socket;
* the sports socket closes without a close frame, so every socket has a
  reconnect-with-backoff loop.

The listener holds **no trading state**: everything authoritative lives in
DynamoDB. On restart it re-reads DynamoDB, re-subscribes, re-syncs prices
and re-evaluates the trigger, so an interruption costs a transient tick
rather than a trade.
"""

import asyncio
import json
import logging
import random
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

import websockets

from config.settings import (
    CLOB_MARKET_WS_URL,
    CLOB_USER_WS_URL,
    SPORTS_WS_URL,
    settings,
    today_et,
)
from src.api.clob_client import ClobPriceClient
from src.api.dynamo_store import DynamoStore
from src.api.nba_client import NbaClient
from src.api.queue_client import TradeQueue
from src.trade.book_tracker import BookTracker
from src.trade.score_tracker import ScoreTracker
from src.trade.trigger import TriggerDecision, evaluate_trigger

logger = logging.getLogger(__name__)

# --- CLOB market socket -----------------------------------------------------

MARKET_SUBSCRIBE = "market"


def build_market_subscribe_frame(token_ids: list[str]) -> str:
    """The subscribe frame for the CLOB market WebSocket."""
    return json.dumps(
        {
            "assets_ids": token_ids,
            "type": MARKET_SUBSCRIBE,
            "custom_feature_enabled": True,
        }
    )


def build_user_auth_frame(api_key: str, secret: str, passphrase: str) -> str:
    """The auth frame for the CLOB user WebSocket."""
    return json.dumps(
        {
            "auth": {
                "apiKey": api_key,
                "secret": secret,
                "passphrase": passphrase,
            },
            "type": "user",
            "custom_feature_enabled": True,
        }
    )


class MarketSocket:
    """The CLOB market WebSocket, with heartbeat and reconnect."""

    def __init__(
        self,
        token_ids: list[str],
        url: str = CLOB_MARKET_WS_URL,
        ping_interval: Optional[int] = None,
    ):
        self.token_ids = list(token_ids)
        self.url = url
        self.ping_interval = ping_interval or settings.ws_ping_interval
        self._ws = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    def set_token_ids(self, token_ids: list[str]) -> None:
        """Change the subscription set.

        Called after the armed games are re-read from DynamoDB; the new list
        is sent on the next subscribe.
        """
        self.token_ids = list(token_ids)

    async def resubscribe(self, token_ids: list[str]) -> None:
        """Re-subscribe on the live connection.

        The CLOB market socket sends a full ``book`` snapshot for an asset
        only when it is subscribed, so a game armed after the initial
        subscribe would never deliver a price. Re-subscribing with the new
        asset list is what makes it visible.
        """
        self.token_ids = list(token_ids)
        if self._ws is None:
            return
        await self._ws.send(build_market_subscribe_frame(self.token_ids))
        logger.info("Re-subscribed to %d token(s)", len(self.token_ids))

    async def connect(self):
        self._ws = await websockets.connect(self.url)
        await self._ws.send(build_market_subscribe_frame(self.token_ids))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            "Market socket connected, subscribed to %d token(s)", len(self.token_ids)
        )
        return self._ws

    async def _heartbeat_loop(self) -> None:
        """Send the application-level ``PING`` every N seconds.

        The market socket expects a text ``PING`` from the client and answers
        ``PONG``. A bare ``PONG`` from the server is ignored.
        """
        try:
            while True:
                await asyncio.sleep(self.ping_interval)
                if self._ws is None:
                    return
                try:
                    await self._ws.send("PING")
                except Exception as exc:  # noqa: BLE001 - socket is going away
                    logger.debug("Heartbeat send failed: %s", exc)
                    return
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def recv(self) -> str:
        return await self._ws.recv()


class SportsSocket:
    """The sports WebSocket: optional, broadcast-every-league, flaky."""

    def __init__(self, url: str = SPORTS_WS_URL):
        self.url = url
        self._ws = None

    async def connect(self):
        self._ws = await websockets.connect(self.url)
        logger.info("Sports socket connected (optional feed)")
        return self._ws

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def recv(self) -> str:
        return await self._ws.recv()


class UserSocket:
    """The CLOB user WebSocket: fill notifications."""

    def __init__(self, url: str = CLOB_USER_WS_URL):
        self.url = url
        self._ws = None

    async def connect(self, api_key: str, secret: str, passphrase: str):
        self._ws = await websockets.connect(self.url)
        await self._ws.send(build_user_auth_frame(api_key, secret, passphrase))
        logger.info("User socket connected")
        return self._ws

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def recv(self) -> str:
        return await self._ws.recv()


class Listener:
    """Coordinates the sockets, the books and the trigger evaluation."""

    def __init__(
        self,
        *,
        store: Optional[DynamoStore] = None,
        queue: Optional[TradeQueue] = None,
        dry_run: Optional[bool] = None,
        clob: Optional[ClobPriceClient] = None,
        nba: Optional[NbaClient] = None,
    ):
        self.store = store or DynamoStore()
        self.queue = queue or TradeQueue()
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.books = BookTracker()
        self.scores = ScoreTracker()
        self.clob = clob or ClobPriceClient()
        self.nba = nba or NbaClient()
        self.games: list[dict[str, Any]] = []
        self.target_date: Optional[date] = None
        self._claimed: set[str] = set()
        self._running = True
        # The running minimum bid for the monitored team of each game, and
        # the slug of the games whose minimum has changed since the last
        # flush to DynamoDB.
        self._lowest_bids: dict[str, float] = {}
        self._lowest_bid_ts: dict[str, int] = {}
        self._lowest_dirty: set[str] = set()

    # --- setup -------------------------------------------------------------

    def load_games(self, target_date: Optional[date] = None) -> list[dict[str, Any]]:
        """Load the armed games from DynamoDB."""
        day = target_date.isoformat() if target_date is not None else None
        if day:
            items = self.store.get_games_for_date(day)
        else:
            items = self.store.scan_armed_games()
        games = [
            i
            for i in items
            if i.get("sk") == "META"
            and i.get("status") == "armed"
            and i.get("monitored_team")
        ]
        self.games = games
        self.scores.track_games(games)
        logger.info("Loaded %d armed game(s)", len(games))
        return games

    def token_ids(self) -> list[str]:
        """Every token we need to subscribe to."""
        tokens: list[str] = []
        for game in self.games:
            for entry in (game.get("team_tokens") or {}).values():
                token_id = entry.get("token_id")
                if token_id and token_id not in tokens:
                    tokens.append(str(token_id))
        return tokens

    # --- auto-heal ---------------------------------------------------------

    def refresh_scores(self) -> int:
        """Refresh the live scores over REST.

        The sports WebSocket is the least reliable feed in the stack: it
        closes without a close frame and only emits for in-progress games.
        So the pre-game and finished states come from REST (API Ninjas, then
        ESPN). No trade decision depends on the score - it only drives the
        display and the halftime cutoff.
        """
        try:
            scores = self.nba.fetch_live_score(settings.sports_league)
        except Exception as exc:  # noqa: BLE001
            logger.warning("REST score refresh failed: %s", exc)
            return 0
        if not scores:
            return 0
        self.scores.track_games(self.games)
        return self.scores.apply_rest_scores(scores)

    async def resync(self, target_date: Optional[date] = None) -> list[str]:
        """Re-read the state and re-sync the prices after a reconnect.

        The listener holds no trading state, so a disconnect is recoverable:
        the armed games are re-read from DynamoDB, and every token's top of
        book is refreshed from the CLOB REST ``/book`` endpoint - the
        WebSocket only sends a full snapshot on subscribe, so any price move
        during the outage would otherwise be missed.

        The REST calls are blocking, so they run in a thread - the market
        read loop must never be stalled by them.

        Returns the token ids that are now subscribed.
        """
        self.load_games(target_date)
        await asyncio.to_thread(self.refresh_scores)
        tokens = self.token_ids()
        for token_id in tokens:
            try:
                live = await asyncio.to_thread(self.clob.get_bid_ask, token_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Price re-sync failed for %s: %s", token_id, exc)
                continue
            self.books.set_top_of_book(
                token_id, live.get("best_bid"), live.get("best_ask")
            )
        logger.info("Re-synced %d token(s) from the CLOB REST book", len(tokens))
        for token_id in tokens:
            await self.evaluate_token(token_id)
        return tokens

    # --- price handling ----------------------------------------------------

    def _game_for_token(self, token_id: str) -> Optional[dict[str, Any]]:
        for game in self.games:
            for entry in (game.get("team_tokens") or {}).values():
                if str(entry.get("token_id")) == str(token_id):
                    return game
        return None

    def _team_for_token(self, game: dict[str, Any], token_id: str) -> Optional[str]:
        for entry in (game.get("team_tokens") or {}).values():
            if str(entry.get("token_id")) == str(token_id):
                return str(entry.get("name"))
        return None

    def record_lowest_price(self, game: dict[str, Any], best_bid: Optional[float]) -> None:
        """Track the lowest bid the monitored team ever traded at.

        The trigger rule fires on a 50% drop, but the *interesting* number
        for a human is how far the price actually fell. The listener sees
        every tick, so it keeps the running minimum for the monitored team
        and writes it back to DynamoDB so the dashboard can show it.

        The minimum is kept in memory and flushed on a timer (and on the
        trigger) rather than on every tick - writing on every tick would
        put the listener in a hot loop against DynamoDB.
        """
        if best_bid is None:
            return
        slug = str(game.get("slug"))
        token_id = str(game.get("monitored_token_id") or "")
        if not slug or not token_id:
            return

        price = float(best_bid)
        current = self._lowest_bids.get(slug)
        if current is not None and price >= current:
            return

        self._lowest_bids[slug] = price
        self._lowest_bid_ts[slug] = int(time.time())
        self._lowest_dirty.add(slug)

        logger.info(
            "Lowest bid for %s (%s): %.4f",
            slug,
            game.get("monitored_team"),
            price,
        )

    def flush_lowest_prices(self) -> int:
        """Write the tracked lowest bids to DynamoDB.

        Returns the number of games written. Failures are logged and
        swallowed: losing the low-water mark must never stop the listener
        from trading.
        """
        if not self._lowest_dirty:
            return 0
        written = 0
        for slug in list(self._lowest_dirty):
            price = self._lowest_bids.get(slug)
            if price is None:
                self._lowest_dirty.discard(slug)
                continue
            try:
                self.store.update_game(
                    slug,
                    {
                        "lowest_bid": price,
                        "lowest_bid_ts": self._lowest_bid_ts.get(slug),
                    },
                )
                written += 1
                self._lowest_dirty.discard(slug)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not write the lowest bid for %s: %s", slug, exc)
        if written:
            logger.info("Wrote the lowest bid for %d game(s)", written)
        return written

    async def handle_market_frame(self, raw: str) -> None:
        """Process one frame from the market socket."""
        if raw.strip() in ("PONG", "PING"):
            # The market socket does not ping us, but reply just in case.
            return

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.debug("Unparseable market frame: %s", raw[:120])
            return

        # The initial subscribe snapshot arrives as a JSON **array** of book
        # objects (one per subscribed asset); every later frame is a single
        # object.
        frames = payload if isinstance(payload, list) else [payload]
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            await self._handle_market_payload(frame)

    async def _handle_market_payload(self, payload: dict[str, Any]) -> None:
        """Process one market payload (already decoded to a dict)."""
        event_type = str(payload.get("event_type") or payload.get("type") or "")
        if event_type == "book":
            book = self.books.apply_book_frame(payload)
            if book is not None:
                await self.evaluate_token(book.token_id)
        elif event_type in ("price_change", "priceChange"):
            for book in self.books.apply_price_change_frame(payload):
                await self.evaluate_token(book.token_id)
        elif event_type == "last_trade_price":
            # Trade prints do not change the book; nothing to evaluate.
            return
        elif event_type == "tick_size_change":
            logger.info("Tick size changed: %s", payload.get("new_tick_size"))
        else:
            logger.debug("Unhandled market event type: %s", event_type)

    async def evaluate_token(self, token_id: str) -> None:
        """Evaluate the trigger rule for the team owning this token."""
        game = self._game_for_token(token_id)
        if game is None:
            return
        team = self._team_for_token(game, token_id)
        if not team:
            return

        slug = str(game.get("slug"))
        if slug in self._claimed:
            return

        best_bid = self.books.best_bid(token_id)
        best_ask = self.books.best_ask(token_id)

        # Track the low-water mark of the monitored team's bid. This runs
        # for the monitored team only - the opponent's price is not what
        # the dashboard reports.
        if team == str(game.get("monitored_team") or ""):
            self.record_lowest_price(game, best_bid)

        decision = self.build_decision(game, team, best_bid, best_ask)
        if not decision.fired:
            logger.debug(
                "Trigger not fired for %s (%s): %s",
                slug,
                team,
                decision.reason,
            )
            return

        await self.fire(game, decision, best_ask)

    def build_decision(
        self,
        game: dict[str, Any],
        team: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
    ) -> TriggerDecision:
        """Build the trigger decision for a game/tick."""
        now = int(time.time())
        score = self.scores.get(
            str(game.get("away_abbr") or ""), str(game.get("home_abbr") or "")
        )
        opponent = game.get("opponent_team")
        opponent_bid = None
        opponent_pre_game_mid = None
        if opponent:
            opponent_token = None
            for entry in (game.get("team_tokens") or {}).values():
                if str(entry.get("name")) == str(opponent):
                    opponent_token = entry.get("token_id")
            if opponent_token:
                opponent_bid = self.books.best_bid(str(opponent_token))
            opponent_prices = (game.get("pre_game_prices") or {}).get(str(opponent))
            if opponent_prices:
                opponent_pre_game_mid = opponent_prices.get("mid")

        return evaluate_trigger(
            status=game.get("status"),
            triggered_at=game.get("triggered_at"),
            now_ts=now,
            start_ts=game.get("start_ts"),
            monitored_team=game.get("monitored_team"),
            team=team,
            best_bid=best_bid,
            best_ask=best_ask,
            pre_game_mid=game.get("monitored_pre_game_mid"),
            price_drop_ratio=settings.price_drop_ratio,
            stop_after_period=settings.monitor_stop_after_period,
            max_minutes=settings.monitor_max_minutes,
            require_opponent_rise=settings.require_opponent_rise,
            opponent_best_bid=opponent_bid,
            opponent_pre_game_mid=opponent_pre_game_mid,
            min_price=settings.trigger_min_price,
            max_price=settings.trigger_max_price,
            elapsed_minutes=None,
            period=score.period if score else None,
        )

    async def fire(
        self,
        game: dict[str, Any],
        decision: TriggerDecision,
        best_ask: Optional[float],
    ) -> None:
        """Claim the trigger, publish to SQS, and record the state."""
        slug = str(game.get("slug"))
        self._claimed.add(slug)

        try:
            claimed = self.store.claim_trigger(slug)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to claim trigger for %s: %s", slug, exc)
            self._claimed.discard(slug)
            return

        if not claimed:
            logger.info("Trigger for %s was already claimed by another worker", slug)
            return

        message = {
            "slug": slug,
            "type": "trigger",
            "team": decision.monitored_team,
            "token_id": game.get("monitored_token_id"),
            "market_id": game.get("market_id"),
            "tick_size": game.get("tick_size"),
            "neg_risk": game.get("neg_risk"),
            "pre_game_mid": decision.pre_game_mid,
            "trigger_price": decision.trigger_price,
            "trigger_threshold": decision.threshold,
            "best_ask": best_ask,
            "size": settings.share_count,
            "dry_run": self.dry_run,
            "fired_at": int(time.time()),
            "decision": decision.to_dict(),
        }

        try:
            self.queue.publish_trigger(message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to publish trigger for %s: %s", slug, exc)
            self.store.update_game(slug, {"trigger_publish_error": str(exc)})
            self._claimed.discard(slug)
            return

        self.store.update_game(
            slug,
            {
                "status": "triggered",
                "trigger_price": decision.trigger_price,
                "triggered_at": int(time.time()),
                "trigger_threshold": decision.threshold,
            },
        )
        # Persist the low-water mark now, so the dashboard shows it even if
        # the listener is stopped before the next flush.
        lowest = self._lowest_bids.get(slug)
        if lowest is not None:
            self.store.update_game(
                slug,
                {"lowest_bid": lowest, "lowest_bid_ts": self._lowest_bid_ts.get(slug)},
            )
        self.store.put_log(
            slug,
            "trigger fired",
            {
                "team": decision.monitored_team,
                "trigger_price": decision.trigger_price,
                "threshold": decision.threshold,
                "pre_game_mid": decision.pre_game_mid,
            },
        )
        logger.warning(
            "TRIGGER fired for %s: %s at %.4f (pre-game %.4f, threshold %.4f)",
            slug,
            decision.monitored_team,
            decision.trigger_price or 0.0,
            decision.pre_game_mid or 0.0,
            decision.threshold or 0.0,
        )

    # --- socket loops ------------------------------------------------------

    async def run_market_loop(self, socket: MarketSocket) -> None:
        """Consume the market socket, reconnecting on failure.

        Every (re)connect is an auto-heal point: the armed games are re-read
        from DynamoDB, the subscription set is refreshed and the prices are
        re-synced from the CLOB REST ``/book`` endpoint, because the
        WebSocket's full snapshot is only sent on subscribe.

        The socket is also watched for idleness: the market feed does not
        send a server-side heartbeat, so a socket that goes quiet for
        ``WS_IDLE_TIMEOUT_SECONDS`` is treated as dead and recycled.
        """
        backoff = 1.0
        idle_timeout = max(10, settings.ws_idle_timeout_seconds)
        target_date = self.target_date
        while self._running:
            try:
                # Auto-heal first, so the subscription always covers the games
                # that are currently armed.
                tokens = await self.resync(target_date)
                socket.set_token_ids(tokens)
                await socket.connect()
                backoff = 1.0
                while self._running:
                    raw = await asyncio.wait_for(socket.recv(), timeout=idle_timeout)
                    await self.handle_market_frame(raw)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "Market socket idle for %ds; recycling it", idle_timeout
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Market socket error: %s", exc)
            finally:
                await socket.close()
            if not self._running:
                break
            delay = min(backoff, settings.ws_reconnect_max_seconds)
            logger.info("Reconnecting market socket in %.1fs", delay)
            await asyncio.sleep(delay + random.uniform(0, 0.5))
            backoff = min(delay * 2, settings.ws_reconnect_max_seconds)

    async def run_sports_loop(self, socket: SportsSocket) -> None:
        """Consume the sports socket. Optional: failures never stop the bot."""
        backoff = 1.0
        idle_timeout = max(10, settings.ws_idle_timeout_seconds)
        while self._running:
            try:
                await socket.connect()
                backoff = 1.0
                while self._running:
                    # The feed sends no server heartbeat, so a socket that goes
                    # quiet is recycled by the client-side watchdog.
                    raw = await asyncio.wait_for(socket.recv(), timeout=idle_timeout)
                    if raw.strip() == "ping":
                        # The feed does not normally ping us, but reply if it does.
                        await socket._ws.send("pong")  # noqa: SLF001
                        continue
                    if raw.strip() == "pong":
                        continue
                    try:
                        payload = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    self.scores.apply_sports_frame(payload)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning("Sports socket idle for %ds; recycling it", idle_timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sports socket error (optional feed): %s", exc)
            finally:
                await socket.close()
            if not self._running:
                break
            delay = min(backoff, settings.ws_reconnect_max_seconds)
            logger.info("Reconnecting sports socket in %.1fs", delay)
            await asyncio.sleep(delay + random.uniform(0, 0.5))
            backoff = min(delay * 2, settings.ws_reconnect_max_seconds)

    async def run_user_loop(self, socket: UserSocket) -> None:
        """Consume the user socket for fill notifications."""
        if not settings.ws_user_enabled:
            logger.info("User socket disabled by configuration")
            return

        creds = self._user_credentials()
        if not creds:
            logger.warning("No CLOB credentials for the user socket; skipping")
            return

        backoff = 1.0
        idle_timeout = max(10, settings.ws_idle_timeout_seconds)
        while self._running:
            try:
                await socket.connect(*creds)
                backoff = 1.0
                while self._running:
                    raw = await asyncio.wait_for(socket.recv(), timeout=idle_timeout)
                    await self.handle_user_frame(raw)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning("User socket idle for %ds; recycling it", idle_timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("User socket error: %s", exc)
            finally:
                await socket.close()
            if not self._running:
                break
            delay = min(backoff, settings.ws_reconnect_max_seconds)
            logger.info("Reconnecting user socket in %.1fs", delay)
            await asyncio.sleep(delay + random.uniform(0, 0.5))
            backoff = min(delay * 2, settings.ws_reconnect_max_seconds)

    def _user_credentials(self) -> Optional[tuple[str, str, str]]:
        """Derive the CLOB API credentials from the private key."""
        if not settings.private_key:
            return None
        try:
            from src.trade.executor import TradeExecutor

            client = TradeExecutor(dry_run=True)._get_client()  # noqa: SLF001
            creds = getattr(client, "creds", None)
            if creds is None:
                return None
            api_key = getattr(creds, "api_key", None) or getattr(creds, "apiKey", None)
            secret = getattr(creds, "secret", None)
            passphrase = getattr(creds, "passphrase", None)
            if api_key and secret and passphrase:
                return str(api_key), str(secret), str(passphrase)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not derive CLOB user credentials: %s", exc)
        return None

    async def handle_user_frame(self, raw: str) -> None:
        """Handle a user socket frame (order/trade notifications)."""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        event_type = str(payload.get("event_type") or "")
        if event_type not in ("trade", "order"):
            return

        for entry in payload.get("trades") or payload.get("orders") or []:
            asset_id = str(entry.get("asset_id") or entry.get("token_id") or "")
            game = self._game_for_token(asset_id)
            if game is None:
                continue
            slug = str(game.get("slug"))
            status = str(entry.get("status") or "")
            matched = entry.get("size_matched") or entry.get("original_size") or 0
            logger.info(
                "User socket: %s %s on %s matched=%s status=%s",
                event_type,
                slug,
                asset_id,
                matched,
                status,
            )
            self.store.put_log(
                slug,
                f"user_ws_{event_type}",
                {
                    "asset_id": asset_id,
                    "status": status,
                    "size_matched": matched,
                    "order_id": entry.get("order_id"),
                },
            )
            if event_type == "trade" and status in ("MATCHED", "FILLED"):
                try:
                    self.queue.publish_fill(
                        {
                            "slug": slug,
                            "token_id": asset_id,
                            "order_id": entry.get("order_id"),
                            "size_matched": matched,
                            "market_id": game.get("market_id"),
                            "tick_size": game.get("tick_size"),
                            "neg_risk": game.get("neg_risk"),
                            "pre_game_mid": game.get("monitored_pre_game_mid"),
                            "team": game.get("monitored_team"),
                            "dry_run": self.dry_run,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Failed to publish fill for %s: %s", slug, exc)

    # --- run ---------------------------------------------------------------

    async def run(self, target_date: Optional[date] = None) -> None:
        """Run the listener until cancelled.

        The listener starts hours before any game is armed - the scale-up
        schedule fires at 07:00 UTC, but games are only armed roughly 30
        minutes before tip-off. So the listener keeps polling for newly
        armed games instead of loading them once and giving up.
        """
        self.target_date = target_date
        self.load_games(target_date)
        tokens = self.token_ids()
        logger.info(
            "Listener starting: target date %s, %d armed game(s), %d token(s), dry_run=%s",
            self.target_date.isoformat() if self.target_date else "today",
            len(self.games),
            len(tokens),
            self.dry_run,
        )
        if not tokens:
            logger.info(
                "No armed games yet; waiting for the snapshot job to arm them"
            )

        market = MarketSocket(tokens)
        sports = SportsSocket()
        user = UserSocket()

        tasks = [
            asyncio.create_task(self.run_market_loop(market)),
            asyncio.create_task(self.run_sports_loop(sports)),
            asyncio.create_task(self.run_user_loop(user)),
            asyncio.create_task(self.run_watch_loop(market)),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Listener cancelled")
        finally:
            self._running = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_watch_loop(self, socket: MarketSocket) -> None:
        """Poll for newly armed games and keep the subscription current.

        Games are armed by the pre-game snapshot job, which runs every five
        minutes. The market socket only sends a full book snapshot on
        subscribe, so a game armed after the subscription was made would
        never be seen - the socket has to re-subscribe.

        The same loop enforces the day's listener window: once the window
        has closed there is no game left to trade, so the listener stops
        itself rather than running until the scale-down schedule fires.

        This loop also emits the listener's heartbeat. Without it there is
        nothing in the log between start-up and the first trigger, so an
        idle-but-healthy listener is indistinguishable from a dead one.
        """
        interval = max(30, settings.snapshot_interval_minutes * 60)
        while self._running:
            await asyncio.sleep(interval)
            if self.window_closed():
                logger.info("Listener window has closed; stopping the listener")
                self._running = False
                return
            try:
                await self.refresh_subscription(socket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Subscription refresh failed: %s", exc)
            self.log_heartbeat()
            self.flush_lowest_prices()

    def log_heartbeat(self) -> None:
        """Log the listener's current state.

        The heartbeat is the answer to "is the listener working?". It
        reports the number of armed games, the number of subscribed tokens,
        the current best bid for each monitored team and the lowest bid
        seen so far - so the log shows the strategy's inputs moving, not
        just that the process is alive.
        """
        armed = len(self.games)
        tokens = len(self.token_ids())
        parts = []
        for game in self.games:
            slug = str(game.get("slug"))
            token_id = str(game.get("monitored_token_id") or "")
            team = game.get("monitored_team") or "?"
            if not token_id:
                continue
            bid = self.books.best_bid(token_id)
            low = self._lowest_bids.get(slug)
            parts.append(
                "%s bid=%s low=%s" % (
                    team,
                    "%.4f" % bid if bid is not None else "-",
                    "%.4f" % low if low is not None else "-",
                )
            )
        logger.info(
            "Heartbeat: %d armed game(s), %d token(s) subscribed%s",
            armed,
            tokens,
            (" | " + "; ".join(parts)) if parts else "",
        )

    def window_closed(self) -> bool:
        """Whether the day's listener window has passed.

        The window is derived from the tip-off times and stored by the daily
        fetch. When it cannot be read (the item is missing, the fetch has
        not run yet) the listener keeps running - the scale-down schedule is
        the backstop.
        """
        day = self.target_date.isoformat() if self.target_date else today_et().isoformat()
        try:
            window = self.store.get_schedule_window(day)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not read the listener window: %s", exc)
            return False
        if not window or not window.get("end_ts"):
            return False
        return int(window["end_ts"]) <= int(time.time())

    async def refresh_subscription(self, socket: MarketSocket) -> bool:
        """Re-read the armed games and re-subscribe if the set changed.

        Returns True when the subscription was updated.
        """
        previous = set(self.token_ids())
        self.load_games(self.target_date)
        current = set(self.token_ids())
        if current == previous:
            return False

        logger.info(
            "Armed games changed (%d -> %d tokens); re-subscribing",
            len(previous),
            len(current),
        )
        socket.set_token_ids(sorted(current))
        await socket.resubscribe(sorted(current))
        await self.resync(self.target_date)
        return True


async def run_listener(
    *,
    target_date: Optional[date] = None,
    dry_run: Optional[bool] = None,
) -> None:
    """Entry point for the Fargate listener."""
    listener = Listener(dry_run=dry_run)
    await listener.run(target_date)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(run_listener())
