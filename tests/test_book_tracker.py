"""Tests for the local order book tracker."""

from src.trade.book_tracker import BookTracker


def test_applies_a_book_snapshot():
    tracker = BookTracker()
    tracker.apply_book_frame(
        {
            "asset_id": "tok",
            "bids": [{"price": "0.63", "size": "100"}, {"price": "0.62", "size": "50"}],
            "asks": [{"price": "0.64", "size": "80"}, {"price": "0.66", "size": "20"}],
        }
    )
    book = tracker.get("tok")
    assert book.best_bid == 0.63
    assert book.best_ask == 0.64
    assert book.mid == 0.635


def test_price_change_updates_the_book():
    tracker = BookTracker()
    tracker.apply_book_frame(
        {
            "asset_id": "tok",
            "bids": [{"price": "0.63", "size": "100"}],
            "asks": [{"price": "0.64", "size": "80"}, {"price": "0.66", "size": "20"}],
        }
    )
    tracker.apply_price_change_frame(
        {
            "market": "tok",
            "price_changes": [
                {"price": "0.62", "size": "40", "side": "BUY"},
                {"price": "0.64", "size": "0", "side": "SELL"},
            ],
        }
    )
    book = tracker.get("tok")
    # The 0.64 ask was removed, so the next ask (0.66) should surface.
    assert book.best_ask == 0.66
    assert book.best_bid == 0.63


def test_zero_size_removes_a_level():
    tracker = BookTracker()
    tracker.apply_book_frame(
        {
            "asset_id": "tok",
            "bids": [{"price": "0.63", "size": "100"}, {"price": "0.61", "size": "10"}],
            "asks": [{"price": "0.64", "size": "80"}],
        }
    )
    tracker.apply_price_change_frame(
        {
            "market": "tok",
            "price_changes": [{"price": "0.63", "size": "0", "side": "BUY"}],
        }
    )
    assert tracker.best_bid("tok") == 0.61


def test_uses_the_best_bid_ask_hints_when_present():
    tracker = BookTracker()
    tracker.apply_price_change_frame(
        {
            "market": "tok",
            "price_changes": [
                {
                    "price": "0.50",
                    "size": "10",
                    "side": "BUY",
                    "best_bid": "0.63",
                    "best_ask": "0.64",
                }
            ],
        }
    )
    book = tracker.get("tok")
    assert book.best_bid == 0.63
    assert book.best_ask == 0.64
    assert book.mid == 0.635


def test_handles_several_changes_in_one_frame():
    tracker = BookTracker()
    tracker.apply_price_change_frame(
        {
            "market": "tok",
            "price_changes": [
                {"price": "0.60", "size": "10", "side": "BUY"},
                {"price": "0.61", "size": "20", "side": "BUY"},
                {"price": "0.70", "size": "30", "side": "SELL"},
            ],
        }
    )
    book = tracker.get("tok")
    assert book.best_bid == 0.61
    assert book.best_ask == 0.70


def test_price_change_with_its_own_asset_id():
    tracker = BookTracker()
    tracker.apply_price_change_frame(
        {
            "price_changes": [
                {"asset_id": "tok", "price": "0.60", "size": "10", "side": "BUY"}
            ],
        }
    )
    assert tracker.best_bid("tok") == 0.60


def test_ignores_malformed_levels():
    tracker = BookTracker()
    tracker.apply_price_change_frame(
        {
            "market": "tok",
            "price_changes": [
                {"price": None, "size": "10", "side": "BUY"},
                {"price": "0.60", "size": None, "side": "BUY"},
            ],
        }
    )
    assert tracker.get("tok").is_empty()


def test_mid_falls_back_to_one_sided_book():
    tracker = BookTracker()
    tracker.apply_book_frame(
        {"asset_id": "tok", "bids": [{"price": "0.40", "size": "10"}], "asks": []}
    )
    assert tracker.mid("tok") == 0.40
