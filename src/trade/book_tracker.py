"""Local order book tracking from Polymarket market WebSocket frames.

Pure data structures, no I/O - the listener feeds it frames and reads back
the best bid/ask.

Two frame shapes matter (both verified against the live feed):

``book``
    A full snapshot for one asset: ``{"asset_id", "bids": [...], "asks": [...]}``
    where each level is ``{"price": "0.63", "size": "100"}``.

``price_change``
    Per-level updates: ``{"price_changes": [{"price", "size", "side",
    "best_bid", "best_ask"}]}``. A ``size`` of ``"0"`` means the level was
    **removed**, so the local book has to be maintained to know the next
    level down.

``price_change`` frames often carry ``best_bid``/``best_ask`` directly; those
are used when present, and the locally maintained book is the fallback.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from src.utils.market_parser import parse_float


@dataclass
class BookLevel:
    """One price level: a price and the size resting at it."""

    price: float
    size: float


@dataclass
class OrderBook:
    """A local order book for one token."""

    token_id: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    best_bid_hint: Optional[float] = None
    best_ask_hint: Optional[float] = None

    # --- mutations ---------------------------------------------------------

    def apply_snapshot(self, payload: dict[str, Any]) -> None:
        """Replace the book with a full snapshot."""
        self.bids = {}
        self.asks = {}
        # A full snapshot supersedes any previously reported best bid/ask.
        self.best_bid_hint = None
        self.best_ask_hint = None
        for level in payload.get("bids") or []:
            price = parse_float(level.get("price"))
            size = parse_float(level.get("size"))
            if price is None or size is None:
                continue
            if size <= 0:
                continue
            self.bids[price] = size
        for level in payload.get("asks") or []:
            price = parse_float(level.get("price"))
            size = parse_float(level.get("size"))
            if price is None or size is None:
                continue
            if size <= 0:
                continue
            self.asks[price] = size

    def apply_level(
        self, price: float, size: float, side: str
    ) -> None:
        """Apply one level update. ``size <= 0`` removes the level."""
        book = self.bids if side.lower() == "buy" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def apply_price_change(self, change: dict[str, Any]) -> None:
        """Apply one entry of a ``price_changes`` array."""
        price = parse_float(change.get("price"))
        size = parse_float(change.get("size"))
        if price is None or size is None:
            return
        side = str(change.get("side") or "")
        # Some frames use "BUY"/"SELL", others "buy"/"sell" / "B" / "S".
        if side.upper().startswith("B"):
            self.apply_level(price, size, "buy")
        elif side.upper().startswith("S"):
            self.apply_level(price, size, "sell")
        else:
            # No side: infer from the book - a price that is already a known
            # bid is a bid, otherwise treat it as an ask.
            side = "buy" if price in self.bids else "sell"
            self.apply_level(price, size, side)

        best_bid = parse_float(change.get("best_bid"))
        if best_bid is not None:
            self.best_bid_hint = best_bid
        best_ask = parse_float(change.get("best_ask"))
        if best_ask is not None:
            self.best_ask_hint = best_ask

    # --- reads -------------------------------------------------------------

    @property
    def best_bid(self) -> Optional[float]:
        # The feed's reported top-of-book wins when present: the local book
        # is only a fallback because price_change frames can omit levels.
        if self.best_bid_hint is not None:
            return self.best_bid_hint
        if self.bids:
            return max(self.bids)
        return None

    @property
    def best_ask(self) -> Optional[float]:
        if self.best_ask_hint is not None:
            return self.best_ask_hint
        if self.asks:
            return min(self.asks)
        return None

    @property
    def mid(self) -> Optional[float]:
        bid = self.best_bid
        ask = self.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return bid if bid is not None else ask

    def is_empty(self) -> bool:
        return not self.bids and not self.asks


class BookTracker:
    """Order books for many tokens, keyed by token id."""

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}

    def get(self, token_id: str) -> OrderBook:
        book = self._books.get(str(token_id))
        if book is None:
            book = OrderBook(token_id=str(token_id))
            self._books[str(token_id)] = book
        return book

    def known(self, token_id: str) -> bool:
        return str(token_id) in self._books

    def apply_book_frame(self, payload: dict[str, Any]) -> Optional[OrderBook]:
        """Apply a ``book`` frame."""
        asset_id = payload.get("asset_id") or payload.get("assetId")
        if not asset_id:
            return None
        book = self.get(str(asset_id))
        book.apply_snapshot(payload)
        return book

    def apply_price_change_frame(self, payload: dict[str, Any]) -> list[OrderBook]:
        """Apply a ``price_change`` frame, which may carry several changes."""
        touched: list[OrderBook] = []
        changes = payload.get("price_changes") or payload.get("priceChanges") or []
        asset_id = payload.get("market") or payload.get("asset_id")
        for change in changes:
            price = parse_float(change.get("price"))
            size = parse_float(change.get("size"))
            if price is None or size is None:
                continue
            # price_change entries may name their own asset; fall back to
            # the frame-level asset id.
            target = change.get("asset_id") or change.get("assetId") or asset_id
            if not target:
                continue
            book = self.get(str(target))
            book.apply_price_change(change)
            touched.append(book)
        return touched

    def set_top_of_book(
        self, token_id: str, best_bid: Optional[float], best_ask: Optional[float]
    ) -> OrderBook:
        """Set the top of book directly.

        Used by the REST re-sync after a reconnect: the CLOB ``/book`` endpoint
        is the authoritative snapshot when the WebSocket has missed updates.
        """
        book = self.get(str(token_id))
        if best_bid is not None:
            book.best_bid_hint = float(best_bid)
        if best_ask is not None:
            book.best_ask_hint = float(best_ask)
        return book

    def best_bid(self, token_id: str) -> Optional[float]:
        return self.get(token_id).best_bid

    def best_ask(self, token_id: str) -> Optional[float]:
        return self.get(token_id).best_ask

    def mid(self, token_id: str) -> Optional[float]:
        return self.get(token_id).mid

    def token_ids(self) -> list[str]:
        return list(self._books)
