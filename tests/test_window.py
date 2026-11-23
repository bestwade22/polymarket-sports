"""Tests for the derived listener window."""

from typing import Any, Optional

from src.trade.window import (
    DEFAULT_LEAD_MINUTES,
    DEFAULT_TRAIL_MINUTES,
    MAX_WINDOW_HOURS,
    MIN_WINDOW_MINUTES,
    collect_start_times,
    compute_window,
    describe_window,
    game_start_ts,
    next_window_start,
    parse_schedule_end_expression,
    parse_schedule_expression,
    window_is_open,
    window_seconds_remaining,
)

# 2026-11-16 19:00 ET = 2026-11-17 00:00 UTC
FIRST_TIPOFF = 1794873600
# 2026-11-16 21:30 ET = 2026-11-17 02:30 UTC
SECOND_TIPOFF = 1794883800


def _game(start_ts: int) -> dict[str, Any]:
    return {"slug": f"nba-x-y-2026-11-16", "start_ts": start_ts}


class TestGameStartTs:
    def test_reads_start_ts(self) -> None:
        assert game_start_ts({"start_ts": FIRST_TIPOFF}) == FIRST_TIPOFF

    def test_reads_start_time_iso(self) -> None:
        assert game_start_ts({"start_time": "2026-11-17T00:00:00Z"}) == FIRST_TIPOFF

    def test_missing_is_none(self) -> None:
        assert game_start_ts({}) is None
        assert game_start_ts({"start_ts": None}) is None

    def test_unparseable_is_none(self) -> None:
        assert game_start_ts({"start_time": "not-a-time"}) is None


class TestCollectStartTimes:
    def test_sorts_and_skips_unknown(self) -> None:
        games = [
            _game(SECOND_TIPOFF),
            {"slug": "no-start"},
            _game(FIRST_TIPOFF),
        ]
        assert collect_start_times(games) == [FIRST_TIPOFF, SECOND_TIPOFF]

    def test_empty(self) -> None:
        assert collect_start_times([]) == []


class TestComputeWindow:
    def test_window_spans_first_to_last_game(self) -> None:
        window = compute_window(
            [_game(FIRST_TIPOFF), _game(SECOND_TIPOFF)],
            now_ts=FIRST_TIPOFF - 3600,
        )
        assert window is not None
        # The window opens the lead before the first tip-off ...
        assert window["start_ts"] == FIRST_TIPOFF - DEFAULT_LEAD_MINUTES * 60
        # ... and closes the trail after the last one.
        assert window["end_ts"] == SECOND_TIPOFF + DEFAULT_TRAIL_MINUTES * 60
        assert window["first_start_ts"] == FIRST_TIPOFF
        assert window["last_start_ts"] == SECOND_TIPOFF
        assert window["game_count"] == 2

    def test_single_game(self) -> None:
        window = compute_window([_game(FIRST_TIPOFF)], now_ts=FIRST_TIPOFF - 3600)
        assert window is not None
        assert window["first_start_ts"] == window["last_start_ts"] == FIRST_TIPOFF

    def test_no_games_returns_none(self) -> None:
        assert compute_window([]) is None

    def test_game_without_start_time_returns_none(self) -> None:
        assert compute_window([{"slug": "no-start"}]) is None

    def test_past_window_returns_none(self) -> None:
        # A game that finished hours ago has nothing to trade.
        now = FIRST_TIPOFF + 6 * 3600
        assert compute_window([_game(FIRST_TIPOFF)], now_ts=now) is None

    def test_start_is_clamped_to_now(self) -> None:
        # The window opened 10 minutes ago; it must not start in the past.
        now = FIRST_TIPOFF - DEFAULT_LEAD_MINUTES * 60 + 600
        window = compute_window([_game(FIRST_TIPOFF)], now_ts=now)
        assert window is not None
        assert window["start_ts"] == now

    def test_window_too_short_is_rejected(self) -> None:
        # The tip-off is 5 minutes away, so the window would be 5 + trail
        # minutes - long enough. Instead test a game that just tipped off
        # with a tiny trail.
        now = FIRST_TIPOFF + 60
        window = compute_window(
            [_game(FIRST_TIPOFF)], now_ts=now, lead_minutes=0, trail_minutes=5
        )
        assert window is None

    def test_window_is_capped(self) -> None:
        # Two games a week apart would be an absurd window.
        far = FIRST_TIPOFF + 7 * 86400
        window = compute_window(
            [_game(FIRST_TIPOFF), _game(far)], now_ts=FIRST_TIPOFF - 3600
        )
        assert window is not None
        assert window["end_ts"] - window["start_ts"] <= MAX_WINDOW_HOURS * 3600

    def test_custom_lead_and_trail(self) -> None:
        window = compute_window(
            [_game(FIRST_TIPOFF)],
            now_ts=FIRST_TIPOFF - 3600,
            lead_minutes=10,
            trail_minutes=20,
        )
        assert window is not None
        assert window["start_ts"] == FIRST_TIPOFF - 600
        assert window["end_ts"] == FIRST_TIPOFF + 1200


class TestWindowIsOpen:
    def test_open_inside(self) -> None:
        window = {"start_ts": 100, "end_ts": 200}
        assert window_is_open(window, now_ts=150) is True

    def test_closed_before_start(self) -> None:
        assert window_is_open({"start_ts": 100, "end_ts": 200}, now_ts=50) is False

    def test_closed_after_end(self) -> None:
        assert window_is_open({"start_ts": 100, "end_ts": 200}, now_ts=200) is False

    def test_none_is_closed(self) -> None:
        assert window_is_open(None, now_ts=150) is False


class TestSecondsRemaining:
    def test_remaining(self) -> None:
        assert window_seconds_remaining({"start_ts": 100, "end_ts": 200}, now_ts=150) == 50

    def test_never_negative(self) -> None:
        assert window_seconds_remaining({"start_ts": 100, "end_ts": 200}, now_ts=500) == 0

    def test_none(self) -> None:
        assert window_seconds_remaining(None, now_ts=150) == 0


class TestScheduleExpressions:
    def test_start_expression_is_utc_iso(self) -> None:
        window = {"start_ts": FIRST_TIPOFF, "end_ts": FIRST_TIPOFF + 3600}
        assert parse_schedule_expression(window) == "2026-11-17T00:00:00"

    def test_end_expression_is_utc_iso(self) -> None:
        window = {"start_ts": FIRST_TIPOFF, "end_ts": FIRST_TIPOFF + 3600}
        assert parse_schedule_end_expression(window) == "2026-11-17T01:00:00"

    def test_none_window(self) -> None:
        assert parse_schedule_expression(None) is None
        assert parse_schedule_end_expression(None) is None


class TestNextWindowStart:
    def test_picks_earliest_future(self) -> None:
        windows = [
            {"start_ts": 100},
            {"start_ts": 300},
            {"start_ts": 50},
        ]
        assert next_window_start(windows, now_ts=200) == 300

    def test_none_when_all_past(self) -> None:
        assert next_window_start([{"start_ts": 100}], now_ts=200) is None
        assert next_window_start([], now_ts=200) is None


class TestDescribeWindow:
    def test_describes(self) -> None:
        window = compute_window([_game(FIRST_TIPOFF)], now_ts=FIRST_TIPOFF - 3600)
        text = describe_window(window)
        assert "2026-11-17" in text
        assert "1 games" in text

    def test_no_window(self) -> None:
        assert describe_window(None) == "no window"
