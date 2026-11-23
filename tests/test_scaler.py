"""Tests for the listener scaler."""

from typing import Any, Optional

import pytest

from src.trade.scaler import (
    _schedule_prefix,
    current_desired_count,
    delete_window_schedules,
    resolve_window,
    run_scale_job,
    schedule_window_schedules,
    window_status,
)

FIRST_TIPOFF = 1794873600
SECOND_TIPOFF = 1794883800


class FakeStore:
    def __init__(self, windows: Optional[dict[str, dict]] = None, games=None):
        self.windows = windows or {}
        self.games = games or []
        self.written: list[tuple[str, dict[str, Any]]] = []

    def get_schedule_window(self, day: str):
        return self.windows.get(day)

    def put_schedule_window(self, day: str, window: dict[str, Any]) -> None:
        self.windows[day] = window
        self.written.append((day, window))

    def get_games_for_date(self, day: str):
        return list(self.games)


class FakeEcs:
    def __init__(self, desired: int = 0, missing: bool = False):
        self.desired = desired
        self.missing = missing
        self.calls: list[int] = []

    def update_service(self, *, cluster: str, service: str, desiredCount: int):
        self.calls.append(desiredCount)
        self.desired = desiredCount
        return {}

    def describe_services(self, *, cluster: str, services):
        if self.missing:
            return {"services": []}
        return {"services": [{"desiredCount": self.desired}]}


class FakeScheduler:
    def __init__(self):
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.existing: set[str] = set()

    def create_schedule(self, **kwargs):
        self.created.append(kwargs)
        self.existing.add(kwargs["Name"])
        return {}

    def delete_schedule(self, *, Name: str):
        if Name not in self.existing:
            raise RuntimeError(f"no such schedule: {Name}")
        self.existing.discard(Name)
        self.deleted.append(Name)
        return {}


def _window(start: int = FIRST_TIPOFF - 2400, end: int = SECOND_TIPOFF + 5400):
    return {
        "start_ts": start,
        "end_ts": end,
        "first_start_ts": FIRST_TIPOFF,
        "last_start_ts": SECOND_TIPOFF,
        "game_count": 2,
    }


class TestResolveWindow:
    def test_prefers_the_stored_window(self) -> None:
        store = FakeStore({"2026-11-16": _window()})
        window = resolve_window(store=store, target_date=_date())
        assert window is not None
        assert window["start_ts"] == FIRST_TIPOFF - 2400

    def test_recomputes_from_the_stored_games(self) -> None:
        games = [
            {"sk": "META", "start_ts": FIRST_TIPOFF},
            {"sk": "META", "start_ts": SECOND_TIPOFF},
        ]
        store = FakeStore(games=games)
        window = resolve_window(
            store=store, target_date=_date(), now_ts=FIRST_TIPOFF - 3600
        )
        assert window is not None
        assert window["first_start_ts"] == FIRST_TIPOFF
        assert window["last_start_ts"] == SECOND_TIPOFF
        # It was written back so the next read is a plain lookup.
        assert store.written

    def test_no_games_returns_none(self) -> None:
        store = FakeStore()
        assert resolve_window(store=store, target_date=_date()) is None

    def test_non_meta_items_are_ignored(self) -> None:
        games = [{"sk": "TRADE#1", "start_ts": FIRST_TIPOFF}]
        store = FakeStore(games=games)
        assert resolve_window(store=store, target_date=_date()) is None


class TestRunScaleJob:
    def test_scales_up_inside_the_window(self) -> None:
        store = FakeStore({"2026-11-16": _window()})
        ecs = FakeEcs(desired=0)
        now = FIRST_TIPOFF
        result = run_scale_job(store=store, client=ecs, target_date=_date(), now_ts=now)
        assert result["desired_count"] == 1
        assert result["changed"] is True
        assert ecs.calls == [1]

    def test_scales_down_outside_the_window(self) -> None:
        store = FakeStore({"2026-11-16": _window()})
        ecs = FakeEcs(desired=1)
        now = SECOND_TIPOFF + 10_000
        result = run_scale_job(store=store, client=ecs, target_date=_date(), now_ts=now)
        assert result["desired_count"] == 0
        assert result["changed"] is True
        assert ecs.calls == [0]

    def test_no_change_when_already_correct(self) -> None:
        store = FakeStore({"2026-11-16": _window()})
        ecs = FakeEcs(desired=1)
        result = run_scale_job(
            store=store, client=ecs, target_date=_date(), now_ts=FIRST_TIPOFF
        )
        assert result["desired_count"] == 1
        assert result["changed"] is False
        assert ecs.calls == []

    def test_no_window_scales_down(self) -> None:
        store = FakeStore()
        ecs = FakeEcs(desired=1)
        result = run_scale_job(store=store, client=ecs, target_date=_date())
        assert result["status"] == "no_window"
        assert result["desired_count"] == 0
        assert ecs.calls == [0]


class TestCurrentDesiredCount:
    def test_reads_the_count(self) -> None:
        assert current_desired_count(client=FakeEcs(desired=3)) == 3

    def test_missing_service(self) -> None:
        assert current_desired_count(client=FakeEcs(missing=True)) is None


class TestScheduleWindowSchedules:
    def test_creates_start_and_end_schedules(self) -> None:
        scheduler = FakeScheduler()
        created = schedule_window_schedules(
            _window(), role_arn="arn:aws:iam::123:role/r", client=scheduler
        )
        assert len(created) == 2
        assert len(scheduler.created) == 2

        by_name = {c["Name"]: c for c in scheduler.created}
        start = by_name[f"{_schedule_prefix()}-start"]
        end = by_name[f"{_schedule_prefix()}-end"]

        assert start["ScheduleExpression"].startswith("at(")
        assert '"desiredCount":1' in start["Target"]["Input"]
        assert '"desiredCount":0' in end["Target"]["Input"]
        assert start["Target"]["RoleArn"] == "arn:aws:iam::123:role/r"

    def test_replaces_the_previous_schedules(self) -> None:
        scheduler = FakeScheduler()
        schedule_window_schedules(_window(), role_arn="r", client=scheduler)
        schedule_window_schedules(_window(), role_arn="r", client=scheduler)
        assert len(scheduler.deleted) == 2

    def test_no_window_creates_nothing(self) -> None:
        scheduler = FakeScheduler()
        assert schedule_window_schedules(None, role_arn="r", client=scheduler) == []
        assert scheduler.created == []

    def test_deletes_the_schedules(self) -> None:
        scheduler = FakeScheduler()
        schedule_window_schedules(_window(), role_arn="r", client=scheduler)
        assert delete_window_schedules(client=scheduler) == 2


class TestWindowStatus:
    def test_reports_the_window(self) -> None:
        store = FakeStore({"2026-11-16": _window()})
        result = window_status(store=store, target_date=_date(), now_ts=FIRST_TIPOFF)
        assert result["status"] == "ok"
        assert result["open"] is True

    def test_no_window(self) -> None:
        result = window_status(store=FakeStore(), target_date=_date())
        assert result["status"] == "no_window"


def _date():
    from datetime import date

    return date(2026, 11, 16)
