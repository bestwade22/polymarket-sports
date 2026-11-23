"""Tests for the Gamma NBA event and moneyline parsing."""

from datetime import date

from src.api.gamma_sports import (
    GammaSportsClient,
    build_team_token_map,
    event_et_date,
    event_start_utc,
    event_teams,
    match_outcome_to_team,
    parse_slug,
    summarise_event,
)


def test_parse_slug_extracts_teams_and_date():
    parsed = parse_slug("nba-nyk-tor-2026-11-17")
    assert parsed == {
        "away_abbr": "nyk",
        "home_abbr": "tor",
        "date": date(2026, 11, 17),
    }


def test_parse_slug_rejects_non_nba():
    assert parse_slug("ncaa-phi-bkn-2026-11-16") is None
    assert parse_slug("") is None
    assert parse_slug("nba-phi-bkn-notadate") is None


def test_parse_slug_handles_dashes_in_the_date():
    # The date itself contains dashes, so the slug has six parts.
    parsed = parse_slug("nba-nyk-tor-2026-11-17")
    assert parsed is not None
    assert parsed["date"] == date(2026, 11, 17)


def test_finds_the_moneyline_market_only(moneyline_event):
    market = GammaSportsClient.find_moneyline_market(moneyline_event)
    assert market is not None
    assert market["id"] == "200"
    # The first-half moneyline and the spread market must not be picked.
    assert market["sportsMarketType"] == "moneyline"


def test_parse_moneyline_market_maps_outcomes_to_tokens(moneyline_event):
    parsed = GammaSportsClient.parse_moneyline_market(moneyline_event)
    assert parsed["market_id"] == "200"
    assert parsed["condition_id"] == "0xabc"
    assert parsed["tokens"] == {"76ers": "token-phi", "Nets": "token-bkn"}
    assert parsed["prices"] == {"76ers": 0.68, "Nets": 0.32}
    assert parsed["tick_size"] == "0.001"
    assert parsed["order_min_size"] == 5
    assert parsed["neg_risk"] is False


def test_parse_moneyline_returns_none_without_moneyline():
    event = {"markets": [{"sportsMarketType": "spreads"}]}
    assert GammaSportsClient.parse_moneyline_market(event) is None


def test_event_teams_reads_ordering(moneyline_event):
    teams = event_teams(moneyline_event)
    assert teams["away"]["name"] == "76ers"
    assert teams["home"]["name"] == "Nets"


def test_outcome_matches_team_by_name(moneyline_event):
    team = match_outcome_to_team("76ers", moneyline_event)
    assert team is not None
    assert team["abbreviation"] == "phi"


def test_outcome_matching_is_case_insensitive(moneyline_event):
    team = match_outcome_to_team("76ERS", moneyline_event)
    assert team is not None
    assert team["name"] == "76ers"


def test_outcome_matching_never_uses_index_order(moneyline_event):
    # The first outcome is "76ers" and it is the away team, but the matching
    # must be by name, not by position in event["teams"].
    teams = event_teams(moneyline_event)
    assert teams["away"]["name"] == "76ers"
    matched = match_outcome_to_team("Nets", moneyline_event)
    assert matched["abbreviation"] == "bkn"


def test_outcome_matches_by_abbreviation_fallback(moneyline_event):
    team = match_outcome_to_team("bkn", moneyline_event)
    assert team is not None
    assert team["name"] == "Nets"


def test_unknown_outcome_returns_none(moneyline_event):
    assert match_outcome_to_team("Heat", moneyline_event) is None


def test_build_team_token_map(moneyline_event):
    moneyline = GammaSportsClient.parse_moneyline_market(moneyline_event)
    mapping = build_team_token_map(moneyline_event, moneyline)
    assert mapping["away"]["token_id"] == "token-phi"
    assert mapping["away"]["name"] == "76ers"
    assert mapping["away"]["price"] == 0.68
    assert mapping["home"]["token_id"] == "token-bkn"
    assert mapping["home"]["price"] == 0.32


def test_event_et_date_uses_the_slug(moneyline_event):
    # startTime is 2026-11-17T00:30:00Z, which is still 2026-11-16 in ET.
    assert event_et_date(moneyline_event) == date(2026, 11, 16)
    assert event_start_utc(moneyline_event).isoformat() == "2026-11-17T00:30:00+00:00"


def test_summarise_event(moneyline_event):
    summary = summarise_event(moneyline_event)
    assert summary["slug"] == "nba-phi-bkn-2026-11-16"
    assert summary["away_team"] == "76ers"
    assert summary["home_team"] == "Nets"
    assert summary["away_token_id"] == "token-phi"
    assert summary["home_token_id"] == "token-bkn"
    assert summary["away_price"] == 0.68
    assert summary["home_price"] == 0.32
    assert summary["market_id"] == "200"
    assert summary["et_date"] == "2026-11-16"


def test_outcome_prices_are_json_decoded(moneyline_event):
    # The raw payload has JSON-encoded strings; the parser must decode them.
    market = moneyline_event["markets"][1]
    assert isinstance(market["outcomePrices"], str)
    parsed = GammaSportsClient.parse_moneyline_market(moneyline_event)
    assert parsed["prices"]["76ers"] == 0.68
