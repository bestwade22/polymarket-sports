"""Tests for the schedule-to-event matching."""

from datetime import date, datetime, timedelta, timezone

from src.api.schedule_matcher import (
    game_abbr_pair,
    game_et_date,
    game_start_utc,
    match_game_to_event,
    match_schedule,
    unmatched_label,
)


def test_game_abbr_pair(schedule_game):
    assert game_abbr_pair(schedule_game) == ("phi", "bkn")


def test_game_et_date(schedule_game):
    assert game_et_date(schedule_game) == date(2026, 11, 16)


def test_game_start_utc(schedule_game):
    assert game_start_utc(schedule_game) == datetime(
        2026, 11, 17, 0, 30, tzinfo=timezone.utc
    )


def test_matches_on_the_abbr_pair(schedule_game, moneyline_event):
    matched = match_game_to_event(schedule_game, [moneyline_event])
    assert matched is not None
    assert matched["slug"] == "nba-phi-bkn-2026-11-16"


def test_does_not_match_a_different_matchup(schedule_game, moneyline_event):
    other = dict(moneyline_event)
    other["teams"] = [
        {"name": "Celtics", "abbreviation": "bos", "ordering": "away"},
        {"name": "Heat", "abbreviation": "mia", "ordering": "home"},
    ]
    assert match_game_to_event(schedule_game, [other]) is None


def test_does_not_match_a_different_date(schedule_game, moneyline_event):
    other = dict(moneyline_event)
    other["slug"] = "nba-phi-bkn-2026-12-01"
    assert match_game_to_event(schedule_game, [other]) is None


def test_tolerates_a_one_day_date_difference(schedule_game, moneyline_event):
    # A late ET game can be recorded under the following calendar day.
    other = dict(moneyline_event)
    other["slug"] = "nba-phi-bkn-2026-11-17"
    assert match_game_to_event(schedule_game, [other]) is not None


def test_picks_the_closest_tipoff_when_two_candidates(
    schedule_game, moneyline_event
):
    near = dict(moneyline_event)
    near["slug"] = "nba-phi-bkn-2026-11-16"
    near["startTime"] = schedule_game["start_time"]

    far = dict(moneyline_event)
    far["slug"] = "nba-phi-bkn-2026-11-17"
    far["startTime"] = "2026-11-17T06:00:00Z"

    matched = match_game_to_event(schedule_game, [far, near])
    assert matched["slug"] == "nba-phi-bkn-2026-11-16"


def test_match_schedule_splits_matched_and_unmatched(
    schedule_game, moneyline_event
):
    unmatched_game = dict(schedule_game)
    unmatched_game["home_team"] = {"abbreviation": "MIA", "name": "Heat"}
    unmatched_game["away_team"] = {"abbreviation": "BOS", "name": "Celtics"}

    matched, unmatched = match_schedule(
        [schedule_game, unmatched_game], [moneyline_event]
    )
    assert len(matched) == 1
    assert len(unmatched) == 1
    assert matched[0]["event"]["slug"] == "nba-phi-bkn-2026-11-16"
    assert unmatched_label(unmatched[0]).startswith("BOS@MIA")


def test_a_game_is_not_matched_twice(schedule_game, moneyline_event):
    matched, unmatched = match_schedule(
        [schedule_game, dict(schedule_game)], [moneyline_event]
    )
    assert len(matched) == 1
    assert len(unmatched) == 1
