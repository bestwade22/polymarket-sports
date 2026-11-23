"""Tests for the live score tracker.

The sports feed broadcasts every league, so the NBA filter and the
team-abbreviation join key are the parts worth pinning down.
"""

from src.trade.score_tracker import ScoreTracker


def test_nba_frames_are_tracked():
    tracker = ScoreTracker()
    score = tracker.apply_sports_frame(
        {
            "leagueAbbreviation": "nba",
            "awayTeam": "NYK",
            "homeTeam": "TOR",
            "awayScore": "55",
            "homeScore": "52",
            "period": "2",
            "clock": "3:21",
            "live": True,
            "ended": False,
        }
    )
    assert score is not None
    assert score.away_score == 55
    assert score.home_score == 52
    assert score.period == 2
    assert score.live is True


def test_other_leagues_are_ignored():
    tracker = ScoreTracker()
    assert (
        tracker.apply_sports_frame(
            {
                "leagueAbbreviation": "cs2",
                "awayTeam": "NAVI",
                "homeTeam": "FAZE",
                "awayScore": "10",
                "homeScore": "9",
            }
        )
        is None
    )
    assert tracker.all_scores() == []


def test_frames_without_teams_are_ignored():
    tracker = ScoreTracker()
    assert (
        tracker.apply_sports_frame({"leagueAbbreviation": "nba", "awayScore": "1"})
        is None
    )


def test_scores_are_looked_up_by_abbr_pair():
    tracker = ScoreTracker()
    tracker.apply_sports_frame(
        {
            "leagueAbbreviation": "nba",
            "awayTeam": "NYK",
            "homeTeam": "TOR",
            "awayScore": "55",
            "homeScore": "52",
            "period": "2",
        }
    )
    score = tracker.get("nyk", "tor")
    assert score is not None
    assert score.away_score == 55
    assert tracker.get("TOR", "NYK") is None


def test_rest_scores_are_merged_by_game_id():
    tracker = ScoreTracker()
    tracker.apply_sports_frame(
        {"leagueAbbreviation": "nba", "awayTeam": "NYK", "homeTeam": "TOR"}
    )
    updated = tracker.apply_rest_scores(
        {"nba-20261117-nyk-tor": {"away_score": 60, "home_score": 58, "source": "api_ninjas"}}
    )
    assert updated == 1
    score = tracker.get("nyk", "tor")
    assert score is not None
    assert score.away_score == 60
    assert score.source == "api_ninjas"


def test_rest_scores_for_unknown_games_are_ignored():
    tracker = ScoreTracker()
    assert tracker.apply_rest_scores({"nba-20261117-lal-bos": {}}) == 0


def test_rest_scores_match_a_tracked_game_id():
    """The REST scoreboard is keyed by the schedule game id, which differs
    from the Gamma game id, so the tracked games provide the join."""
    tracker = ScoreTracker()
    tracker.track_games(
        [
            {
                "game_id": "20024130",
                "away_abbr": "nyk",
                "home_abbr": "tor",
            }
        ]
    )
    updated = tracker.apply_rest_scores(
        {
            "20024130": {
                "away_score": 61,
                "home_score": 59,
                "period": 3,
                "source": "api_ninjas",
            }
        }
    )
    assert updated == 1
    score = tracker.get("nyk", "tor")
    assert score is not None
    assert score.away_score == 61
    assert score.period == 3
