"""Tests for the scheduled daily fetch's date handling.

The scheduled fetch has to get three things right, and each was a bug at
some point:

1. the **ET** day it targets - a fetch that runs before 05:00 UTC falls on
   the previous ET day, so the old 00:30 UTC cron fetched yesterday's slate;
2. the **date filter** on the API Ninjas schedule - the unfiltered endpoint
   only returns the current slate, so a fetch for another day came back
   empty;
3. the **coverage** of the days ahead, so the next evening's slate is staged
   before it is needed.
"""

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from config.settings import fetch_target_date, fetch_target_dates, settings
from lambda_handlers.fetch_daily import resolve_fetch_dates


class TestFetchTargetDates:
    def test_includes_today_first(self) -> None:
        days = fetch_target_dates()
        assert days[0] == fetch_target_date()

    def test_covers_the_days_ahead(self) -> None:
        days = fetch_target_dates()
        assert len(days) == settings.daily_fetch_days_ahead + 1

    def test_the_days_are_consecutive_and_ordered(self) -> None:
        days = fetch_target_dates()
        for earlier, later in zip(days, days[1:]):
            assert later - earlier == timedelta(days=1)

    def test_the_offset_is_applied_in_et_not_utc(self) -> None:
        # The offset must not depend on the host's timezone: the Lambda
        # hosts run in UTC while the bot's dates are ET dates.
        days = fetch_target_dates()
        expected = datetime.now(ZoneInfo("America/New_York")).date()
        assert days[0] == expected


class TestResolveFetchDates:
    def test_an_explicit_date_wins(self) -> None:
        assert resolve_fetch_dates({"date": "2026-11-25"}) == ["2026-11-25"]

    def test_without_a_date_the_configured_range_is_used(self) -> None:
        days = resolve_fetch_dates({})
        assert len(days) == settings.daily_fetch_days_ahead + 1

    def test_the_dates_are_iso_formatted(self) -> None:
        for day in resolve_fetch_dates({}):
            date.fromisoformat(day)


class TestNbaClientDateFilter:
    """The API Ninjas schedule must be filtered by date."""

    def _client(self, payload: Any):
        from src.api.nba_client import NbaClient

        client = NbaClient(api_key="test")

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def get(self, url, params=None, **kwargs):
                self.calls.append(params or {})
                return type(
                    "R",
                    (),
                    {
                        "json": lambda self: payload,
                        "raise_for_status": lambda self: None,
                    },
                )()

        client.session = FakeSession()
        return client

    def test_a_target_date_is_sent_as_a_filter(self) -> None:
        client = self._client([])
        client.fetch_games("nba", target_date=date(2026, 11, 25))
        assert client.session.calls[0]["date"] == "2026-11-25"

    def test_without_a_target_date_no_filter_is_sent(self) -> None:
        # The live-score lookups want the unfiltered current slate.
        client = self._client([])
        client.fetch_games("nba")
        assert "date" not in client.session.calls[0]

    def test_the_league_is_always_sent(self) -> None:
        client = self._client([])
        client.fetch_games("nba", target_date=date(2026, 11, 25))
        assert client.session.calls[0]["league"] == "nba"


class TestGammaTargetDateOrdering:
    """The Gamma event list must be ordered by start time for a target fetch.

    The default (unsorted) order is not stable across requests, so a
    target-date fetch cannot rely on the day's events landing in the first
    page.
    """

    def _client(self, payload: Any):
        from src.api.gamma_sports import GammaSportsClient

        client = GammaSportsClient()

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def get(self, url, params=None, **kwargs):
                self.calls.append(params or {})
                return type(
                    "R",
                    (),
                    {
                        "json": lambda self: payload,
                        "raise_for_status": lambda self: None,
                    },
                )()

        client.session = FakeSession()
        return client

    def test_a_target_date_requests_the_events_in_start_time_order(self) -> None:
        client = self._client([])
        client.list_nba_events(target_date=date(2026, 11, 25))
        params = client.session.calls[0]
        assert params["order"] == "startTime"
        assert params["ascending"] == "true"

    def test_without_a_target_date_the_order_is_left_alone(self) -> None:
        client = self._client([])
        client.list_nba_events()
        params = client.session.calls[0]
        assert "order" not in params
        assert "ascending" not in params


class TestDailyFetchUsesTheTargetDate:
    """The daily fetch must pass its target date down to both clients."""

    def test_the_target_date_reaches_both_clients(self) -> None:
        from src.fetch.daily_fetch import run_daily_fetch

        seen: dict[str, Any] = {}

        class FakeNba:
            def fetch_games(self, league="nba", target_date=None):
                seen["nba"] = target_date
                return []

            def fetch_standings(self, league="nba"):
                return {}

        class FakeGamma:
            def list_nba_events(self, target_date=None):
                seen["gamma"] = target_date
                return []

        class FakeStore:
            def put_game(self, slug, meta):
                return {}

            def put_daily_batch(self, day, batch):
                return None

            def put_schedule_window(self, day, window):
                return None

        target = date(2026, 11, 25)
        run_daily_fetch(
            target_date=target,
            store=FakeStore(),
            nba_client=FakeNba(),
            gamma_client=FakeGamma(),
            notify=False,
        )

        assert seen["nba"] == target
        assert seen["gamma"] == target


class TestWindowScheduleNamesArePerDay:
    """Each day must keep its own pair of one-shot window schedules.

    The daily fetch covers several days at once, so a single fixed schedule
    name would mean each day's fetch deleted the previous day's schedules.
    """

    def test_the_day_is_part_of_the_name(self) -> None:
        from src.trade.scaler import _schedule_prefix

        assert _schedule_prefix("2026-11-25").endswith("-2026-11-25")
        assert _schedule_prefix("2026-11-25") != _schedule_prefix("2026-11-26")

    def test_a_days_schedules_do_not_replace_another_days(self) -> None:
        from src.trade.scaler import schedule_window_schedules

        class FakeScheduler:
            def __init__(self) -> None:
                self.created: list[str] = []
                self.deleted: list[str] = []
                self.existing: set[str] = set()

            def create_schedule(self, **kwargs):
                self.created.append(kwargs["Name"])
                self.existing.add(kwargs["Name"])
                return {}

            def delete_schedule(self, *, Name):
                if Name not in self.existing:
                    raise RuntimeError(f"no such schedule: {Name}")
                self.existing.discard(Name)
                self.deleted.append(Name)
                return {}

        window = {"start_ts": 1794873600, "end_ts": 1794891600}

        scheduler = FakeScheduler()
        schedule_window_schedules(
            window, role_arn="r", day="2026-11-25", client=scheduler
        )
        schedule_window_schedules(
            window, role_arn="r", day="2026-11-26", client=scheduler
        )

        # The second day must not have deleted the first day's schedules.
        assert any("2026-11-25" in name for name in scheduler.created)
        assert any("2026-11-26" in name for name in scheduler.created)
        assert not any("2026-11-25" in name for name in scheduler.deleted)
