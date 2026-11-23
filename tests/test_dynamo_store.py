"""Tests for the DynamoDB single-table store."""

from typing import Any

import boto3
import pytest
from moto import mock_aws

from config.settings import settings
from src.api.dynamo_store import DynamoStore, game_pk

TABLE = "SportsBotStateTest"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> DynamoStore:
    # The local dev stack points at a moto server; unit tests use moto in-process.
    monkeypatch.setattr(settings, "dynamodb_endpoint_url", "")
    with mock_aws():
        dynamo = boto3.client("dynamodb", region_name=settings.aws_region)
        dynamo.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoStore(table_name=TABLE)


def _game_meta(slug: str = "nba-phi-bkn-2026-11-16") -> dict[str, Any]:
    return {
        "slug": slug,
        "away_abbr": "PHI",
        "home_abbr": "BKN",
        "tipoff_ts": 1794875400,
        "gsi1pk": "DATE#2026-11-16",
        "gsi1sk": "1794875400",
        "status": "scheduled",
    }


def test_put_and_get_game(store: DynamoStore):
    store.put_game("nba-phi-bkn-2026-11-16", _game_meta())
    game = store.get_game("nba-phi-bkn-2026-11-16")
    assert game is not None
    assert game["away_abbr"] == "PHI"
    assert game["pk"] == game_pk("nba-phi-bkn-2026-11-16")
    assert game["sk"] == "META"
    assert "expires_at" in game


def test_update_game_merges_fields(store: DynamoStore):
    store.put_game("nba-phi-bkn-2026-11-16", _game_meta())
    store.update_game("nba-phi-bkn-2026-11-16", {"status": "armed", "pre_game_mid": 0.68})
    game = store.get_game("nba-phi-bkn-2026-11-16")
    assert game["status"] == "armed"
    assert float(game["pre_game_mid"]) == 0.68
    # untouched fields survive
    assert game["away_abbr"] == "PHI"


def test_claim_trigger_only_succeeds_once(store: DynamoStore):
    store.put_game("nba-phi-bkn-2026-11-16", _game_meta())
    assert store.claim_trigger("nba-phi-bkn-2026-11-16", claimed_at=1000) is True
    assert store.claim_trigger("nba-phi-bkn-2026-11-16", claimed_at=1001) is False
    game = store.get_game("nba-phi-bkn-2026-11-16")
    assert int(game["triggered_at"]) == 1000


def test_put_and_get_schedule_window(store: DynamoStore):
    window = {
        "start_ts": 1794873600,
        "end_ts": 1794891600,
        "first_start_ts": 1794875400,
        "last_start_ts": 1794883800,
        "game_count": 2,
    }
    store.put_schedule_window("2026-11-16", window)
    stored = store.get_schedule_window("2026-11-16")
    assert stored is not None
    assert int(stored["start_ts"]) == 1794873600
    assert int(stored["end_ts"]) == 1794891600
    assert stored["sk"] == "WINDOW"
    assert stored["pk"] == "DATE#2026-11-16"


def test_get_schedule_window_missing_returns_none(store: DynamoStore):
    assert store.get_schedule_window("2026-11-16") is None


def test_claim_game_state_sets_field_once(store: DynamoStore):
    store.put_game("nba-phi-bkn-2026-11-16", _game_meta())
    assert store.claim_game_state("nba-phi-bkn-2026-11-16", "monitored_token", "tok") is True
    assert store.claim_game_state("nba-phi-bkn-2026-11-16", "monitored_token", "other") is False
    game = store.get_game("nba-phi-bkn-2026-11-16")
    assert game["monitored_token"] == "tok"


def test_put_trade_and_get_trades(store: DynamoStore):
    store.put_game("nba-phi-bkn-2026-11-16", _game_meta())
    store.put_trade("nba-phi-bkn-2026-11-16", {"side": "BUY", "price": 0.3, "size": 5}, trade_id="t1")
    store.put_trade("nba-phi-bkn-2026-11-16", {"side": "SELL", "price": 0.68, "size": 5}, trade_id="t2")
    trades = store.get_trades("nba-phi-bkn-2026-11-16")
    assert len(trades) == 2
    assert trades[0]["side"] == "BUY"
    assert trades[0]["trade_id"] == "t1"


def test_put_log_and_get_logs(store: DynamoStore):
    store.put_game("nba-phi-bkn-2026-11-16", _game_meta())
    store.put_log("nba-phi-bkn-2026-11-16", "armed game", {"token": "tok"})
    logs = store.get_logs("nba-phi-bkn-2026-11-16")
    assert len(logs) == 1
    assert logs[0]["message"] == "armed game"
    assert logs[0]["fields"]["token"] == "tok"


def test_daily_batch_round_trip(store: DynamoStore):
    store.put_daily_batch("2026-11-16", {"game_count": 3, "games": ["a", "b"]})
    batch = store.get_daily_batch("2026-11-16")
    assert batch["game_count"] == 3
    assert batch["games"] == ["a", "b"]


def test_get_games_for_date_uses_gsi1(store: DynamoStore):
    store.put_game("nba-phi-bkn-2026-11-16", _game_meta())
    items = store.get_games_for_date("2026-11-16")
    assert any(i["sk"] == "META" for i in items)


def test_scan_scheduled_and_armed_games(store: DynamoStore):
    store.put_game("nba-phi-bkn-2026-11-16", _game_meta())
    store.update_game("nba-phi-bkn-2026-11-16", {"status": "armed"})
    assert len(store.scan_scheduled_games()) == 0
    armed = store.scan_armed_games()
    assert len(armed) == 1
    assert armed[0]["slug"] == "nba-phi-bkn-2026-11-16"


def test_nested_structures_are_serialised(store: DynamoStore):
    store.put_game(
        "nba-phi-bkn-2026-11-16",
        {**_game_meta(), "teams": {"away": {"abbr": "PHI", "name": "76ers"}}},
    )
    game = store.get_game("nba-phi-bkn-2026-11-16")
    assert game["teams"]["away"]["abbr"] == "PHI"
