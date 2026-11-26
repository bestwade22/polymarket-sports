"""Tests for the listener health record and the dashboard's health panel."""

import time
from datetime import date
from typing import Any, Optional

from src.analysis.exporter import build_dashboard, listener_health_view
from src.trade.listener import Listener


class FakeStore:
    """A store that returns a fixed health, scale and window record."""

    def __init__(
        self,
        items: Optional[list[dict[str, Any]]] = None,
        health: Optional[dict[str, Any]] = None,
        scale: Optional[dict[str, Any]] = None,
        window: Optional[dict[str, Any]] = None,
    ):
        self.items = items or []
        self.health = health
        self.scale = scale
        self.window = window
        self.health_writes: list[tuple[str, dict[str, Any]]] = []
        self.scale_writes: list[tuple[str, int, Optional[int]]] = []

    def get_games_for_date(self, day: str) -> list[dict[str, Any]]:
        return list(self.items)

    def get_listener_health(self, day: str) -> Optional[dict[str, Any]]:
        return self.health

    def get_scale_record(self, day: str) -> Optional[dict[str, Any]]:
        return self.scale

    def get_schedule_window(self, day: str) -> Optional[dict[str, Any]]:
        return self.window

    def put_listener_health(self, day: str, health: dict[str, Any]) -> None:
        self.health_writes.append((day, health))

    def record_scale(
        self, day: str, *, desired: int, ts: Optional[int] = None
    ) -> dict[str, Any]:
        self.scale_writes.append((day, desired, ts))
        return {}


class FakeQueue:
    def publish_trigger(self, payload: dict[str, Any]) -> str:
        return "msg-1"

    def publish_fill(self, payload: dict[str, Any]) -> str:
        return "msg-2"


def _listener(store: FakeStore) -> Listener:
    listener = Listener(store=store, queue=FakeQueue(), dry_run=True)
    listener.target_date = date(2026, 11, 27)
    listener.games = [
        {
            "slug": "nba-phi-bkn-2026-11-27",
            "monitored_team": "76ers",
            "monitored_token_id": "token-phi",
        }
    ]
    return listener


class TestListenerHealthRecord:
    """The listener writes its heartbeat into DynamoDB."""

    def test_the_heartbeat_is_written_to_the_health_record(self) -> None:
        store = FakeStore()
        listener = _listener(store)

        listener.log_heartbeat()

        assert len(store.health_writes) == 1
        day, health = store.health_writes[0]
        assert day == "2026-11-27"
        assert health["state"] == "running"
        assert health["last_heartbeat_ts"] > 0
        assert "Heartbeat" in health["last_message"]

    def test_the_health_record_keeps_the_recent_messages(self) -> None:
        store = FakeStore()
        listener = _listener(store)

        listener.log_heartbeat()
        listener.log_heartbeat()

        _day, health = store.health_writes[-1]
        assert len(health["messages"]) == 2
        assert all("ts" in m and "message" in m for m in health["messages"])

    def test_a_failing_health_write_does_not_raise(self) -> None:
        # A health write must never take the listener down.
        store = FakeStore()

        def _boom(day: str, health: dict[str, Any]) -> None:
            raise RuntimeError("DynamoDB is unhappy")

        store.put_listener_health = _boom
        listener = _listener(store)

        listener.log_heartbeat()  # must not raise

    def test_stopping_is_recorded(self) -> None:
        store = FakeStore()
        listener = _listener(store)

        listener.record_stopped("Listener window has closed")

        _day, health = store.health_writes[-1]
        assert health["state"] == "stopped"
        assert health["last_message"] == "Listener window has closed"


class TestListenerHealthView:
    """The dashboard's view of the listener's health."""

    def test_a_running_listener_is_reported_as_running(self) -> None:
        now = int(time.time())
        view = listener_health_view(
            {"state": "running", "last_heartbeat_ts": now, "last_message": "ok"},
            None,
            None,
        )
        assert view["state"] == "running"
        assert view["last_heartbeat_ts"] == now
        assert view["last_message"] == "ok"

    def test_an_old_heartbeat_is_reported_as_stale(self) -> None:
        stale = int(time.time()) - 3600
        view = listener_health_view(
            {"state": "running", "last_heartbeat_ts": stale}, None, None
        )
        assert view["state"] == "stale"
        assert view["heartbeat_age_seconds"] >= 3600

    def test_the_scale_up_and_down_times_are_exposed(self) -> None:
        view = listener_health_view(
            None,
            {"scale_up_at": 1794870000, "scale_down_at": 1794880000, "desired_count": 0},
            None,
        )
        assert view["scale_up_at"] == 1794870000
        assert view["scale_down_at"] == 1794880000

    def test_the_window_is_exposed(self) -> None:
        view = listener_health_view(
            None,
            None,
            {"start_ts": 1794870000, "end_ts": 1794880000},
        )
        assert view["window_start_ts"] == 1794870000
        assert view["window_end_ts"] == 1794880000

    def test_nothing_recorded_yields_an_unknown_state(self) -> None:
        view = listener_health_view(None, None, None)
        assert view["state"] == "unknown"
        assert view["scale_up_at"] is None
        assert view["messages"] == []


class TestDashboardIncludesTheListener:
    """The dashboard payload carries the listener's health."""

    def test_the_payload_has_a_listener_section(self) -> None:
        store = FakeStore(
            health={"state": "running", "last_heartbeat_ts": int(time.time())},
            scale={"scale_up_at": 1794870000},
            window={"start_ts": 1794870000, "end_ts": 1794880000},
        )
        payload = build_dashboard("2026-11-27", store=store)
        listener = payload["listener"]
        assert listener["state"] == "running"
        assert listener["scale_up_at"] == 1794870000
        assert listener["window_start_ts"] == 1794870000

    def test_a_store_without_the_records_still_builds(self) -> None:
        payload = build_dashboard("2026-11-27", store=FakeStore())
        assert payload["listener"]["state"] == "unknown"

    def test_an_unreadable_record_does_not_break_the_dashboard(self) -> None:
        class BrokenStore(FakeStore):
            def get_listener_health(self, day: str):
                raise RuntimeError("DynamoDB is unhappy")

        payload = build_dashboard("2026-11-27", store=BrokenStore())
        assert payload["listener"]["state"] == "unknown"
