"""End-to-end dry-run of the full pipeline.

Exercises the whole chain in one pass, against the in-process moto emulator:

    daily fetch -> DynamoDB (scheduled)
      -> pre-game snapshot -> DynamoDB (armed)
      -> market frame -> trigger evaluation -> SQS
      -> trade worker -> buy + recovery sell -> DynamoDB (traded)
      -> dashboard export

No network access and no real orders: every external dependency is a stub and
the trade executor runs in dry-run mode.
"""

import asyncio
import json
import time
from typing import Any, Optional

import boto3
import pytest
from moto import mock_aws

from config.settings import settings
from src.analysis.exporter import build_dashboard
from src.api.dynamo_store import DynamoStore
from src.api.queue_client import TradeQueue
from src.fetch.daily_fetch import run_daily_fetch
from src.fetch.pregame_snapshot import run_pregame_snapshot
from src.trade.buy_sell import TradeWorker

TABLE = "SportsBotStateE2E"
QUEUE = "SportsBotTradesE2E"

SLUG = "nba-phi-bkn-2026-11-16"
ET_DATE = "2026-11-16"

# The tip-off is relative to "now" so that the trigger lands inside the
# monitoring window (start .. start + MONITOR_MAX_MINUTES).
START_TS = int(time.time()) - 20 * 60


class FakeNbaClient:
    """Stands in for API Ninjas."""

    def __init__(self, games: list[dict[str, Any]], standings: dict[str, Any]):
        self._games = games
        self._standings = standings

    def fetch_games(self, league: str = "nba") -> list[dict[str, Any]]:
        return self._games

    def fetch_standings(self, league: str = "nba") -> dict[str, Any]:
        return self._standings


class FakeGammaClient:
    """Stands in for the Gamma API."""

    def __init__(self, events: list[dict[str, Any]]):
        self._events = events

    def list_nba_events(self, **_: Any) -> list[dict[str, Any]]:
        return self._events


class FakeClob:
    """Stands in for the CLOB REST client."""

    def __init__(self, books: dict[str, dict[str, Any]]):
        self.books = books
        self.requested: list[str] = []

    def get_bid_ask(self, token_id: str) -> dict[str, Any]:
        self.requested.append(str(token_id))
        return self.books.get(str(token_id), {"best_bid": None, "best_ask": None, "mid": None})


class FakeExecutor:
    """Stands in for the CLOB order executor; records what would be sent."""

    def __init__(self) -> None:
        self.buys: list[dict[str, Any]] = []
        self.sells: list[dict[str, Any]] = []

    def buy_team(self, **kwargs: Any) -> dict[str, Any]:
        self.buys.append(kwargs)
        return {
            "dry_run": True,
            "status": "simulated",
            "order_id": "buy-order-1",
            "price": kwargs.get("price"),
        }

    def sell_team(self, **kwargs: Any) -> dict[str, Any]:
        self.sells.append(kwargs)
        return {
            "dry_run": True,
            "status": "simulated",
            "order_id": "sell-order-1",
            "price": kwargs.get("price"),
        }

    def get_order(self, order_id: str) -> Optional[dict[str, Any]]:
        return None


class NoOrdersChecker:
    """Pre-trade checks that always pass (no live CLOB access)."""

    def game_has_open_buy_order(self, token_ids: list[str]):
        return False, []

    def token_has_open_sell_order(self, token_id: str):
        return False, []

    def game_has_position(self, token_ids: list[str]):
        return False, []


@pytest.fixture
def aws_stack(monkeypatch: pytest.MonkeyPatch):
    """A moto-backed DynamoDB table and SQS queue."""
    monkeypatch.setattr(settings, "dynamodb_endpoint_url", "")
    monkeypatch.setattr(settings, "sqs_endpoint_url", "")
    monkeypatch.setattr(settings, "dynamodb_table", TABLE)
    monkeypatch.setattr(settings, "dry_run", True)

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
        sqs = boto3.client("sqs", region_name=settings.aws_region)
        queue_url = sqs.create_queue(QueueName=QUEUE)["QueueUrl"]
        monkeypatch.setattr(settings, "sqs_queue_url", queue_url)
        yield


def _schedule_game() -> list[dict[str, Any]]:
    return [
        {
            "game_id": "nba-20261116-phi-bkn",
            "league": "nba",
            "date": ET_DATE,
            "start_time": "2026-11-17T00:30:00Z",
            "start_ts": START_TS,
            "status": "scheduled",
            "home_team": {
                "team_id": "nba-bkn",
                "name": "Brooklyn Nets",
                "abbreviation": "BKN",
            },
            "away_team": {
                "team_id": "nba-phi",
                "name": "Philadelphia 76ers",
                "abbreviation": "PHI",
            },
        }
    ]


def _standings() -> dict[str, Any]:
    return {
        "phi": {
            "abbreviation": "phi",
            "name": "Philadelphia 76ers",
            "wins": 3,
            "losses": 2,
            "win_pct": 0.6,
            "streak": "W1",
            "last_10": "5-5",
        },
        "bkn": {
            "abbreviation": "bkn",
            "name": "Brooklyn Nets",
            "wins": 1,
            "losses": 4,
            "win_pct": 0.2,
            "streak": "L2",
            "last_10": "3-7",
        },
    }


def _gamma_event() -> dict[str, Any]:
    return {
        "id": "999001",
        "slug": SLUG,
        "gameId": "20023912",
        "title": "76ers vs. Nets",
        "startTime": "2026-11-17T00:30:00Z",
        "openInterest": 123456,
        "volume": 999999,
        "liquidity": 50000,
        "teams": [
            {
                "name": "76ers",
                "abbreviation": "phi",
                "ordering": "away",
                "record": "3-2",
            },
            {
                "name": "Nets",
                "abbreviation": "bkn",
                "ordering": "home",
                "record": "1-4",
            },
        ],
        "markets": [
            {
                "id": "200",
                "conditionId": "0xabc",
                "question": "76ers vs. Nets",
                "sportsMarketType": "moneyline",
                "outcomes": '["76ers", "Nets"]',
                "clobTokenIds": '["token-phi", "token-bkn"]',
                "outcomePrices": '["0.68", "0.32"]',
                "orderPriceMinTickSize": 0.001,
                "orderMinSize": 5,
                "negRisk": False,
                "closed": False,
                "active": True,
            }
        ],
    }


def test_full_pipeline_dry_run(aws_stack, monkeypatch: pytest.MonkeyPatch):
    store = DynamoStore(table_name=TABLE)
    queue = TradeQueue(queue_url=settings.sqs_queue_url)

    # --- 1. daily fetch ----------------------------------------------------
    fetch_result = run_daily_fetch(
        target_date=None,
        store=store,
        nba_client=FakeNbaClient(_schedule_game(), _standings()),
        gamma_client=FakeGammaClient([_gamma_event()]),
        notify=False,
    )
    assert len(fetch_result["matched"]) == 1
    assert fetch_result["matched"][0]["slug"] == SLUG

    game = store.get_game(SLUG)
    assert game is not None
    assert game["status"] == "scheduled"
    assert game["start_ts"] == START_TS
    assert game["team_tokens"]["away"]["token_id"] == "token-phi"
    assert game["team_tokens"]["home"]["token_id"] == "token-bkn"
    assert game["away_standings"]["wins"] == 3

    # --- 2. pre-game snapshot ---------------------------------------------
    clob = FakeClob(
        {
            "token-phi": {"best_bid": 0.67, "best_ask": 0.69, "mid": 0.68},
            "token-bkn": {"best_bid": 0.31, "best_ask": 0.33, "mid": 0.32},
        }
    )
    snapshot = run_pregame_snapshot(
        target_date=None, store=store, client=clob, now_ts=START_TS - 20 * 60
    )
    assert len(snapshot["armed"]) == 1
    assert snapshot["armed"][0]["monitored_team"] == "76ers"

    armed = store.get_game(SLUG)
    assert armed["status"] == "armed"
    assert float(armed["monitored_pre_game_mid"]) == pytest.approx(0.68)
    assert float(armed["trigger_threshold"]) == pytest.approx(0.34)
    assert armed["monitored_token_id"] == "token-phi"
    assert armed["opponent_team"] == "Nets"

    # --- 3. a live price tick fires the trigger ---------------------------
    from src.trade.listener import Listener

    listener = Listener(store=store, queue=queue, dry_run=True)
    listener.games = [armed]

    # The bid has fallen from 0.67 to 0.30 - below half the pre-game mid.
    frame = json.dumps(
        {
            "event_type": "price_change",
            "market": "token-phi",
            "price_changes": [
                {"price": "0.30", "size": "500", "side": "BUY", "best_bid": "0.30", "best_ask": "0.32"}
            ],
        }
    )
    asyncio.run(listener.handle_market_frame(frame))

    triggered = store.get_game(SLUG)
    assert triggered["status"] == "triggered"
    assert float(triggered["trigger_price"]) == pytest.approx(0.30)
    assert triggered["triggered_at"]

    # --- 4. the trade worker consumes the SQS message ---------------------
    executor = FakeExecutor()
    worker = TradeWorker(
        store=store,
        queue=queue,
        executor=executor,
        clob=FakeClob(
            {
                "token-phi": {"best_bid": 0.30, "best_ask": 0.32, "mid": 0.31},
                "token-bkn": {"best_bid": 0.68, "best_ask": 0.70, "mid": 0.69},
            }
        ),
        dry_run=True,
    )
    worker._order_checker = NoOrdersChecker()
    worker._position_checker = NoOrdersChecker()

    result = worker.run_once(wait_seconds=1)
    assert result is not None
    assert result["status"] == "simulated"
    assert result["reason"] == ""

    # Bought at the best ask, sold back at the pre-game mid.
    assert len(executor.buys) == 1
    assert executor.buys[0]["price"] == pytest.approx(0.32)
    assert executor.buys[0]["token_id"] == "token-phi"
    assert len(executor.sells) == 1
    assert executor.sells[0]["price"] == pytest.approx(0.68)
    assert executor.sells[0]["size"] == settings.share_count

    traded = store.get_game(SLUG)
    assert traded["status"] == "traded"
    assert float(traded["buy_price"]) == pytest.approx(0.32)
    assert float(traded["sell_price"]) == pytest.approx(0.68)

    # The queue is drained: the message was consumed and deleted.
    assert queue.receive(max_messages=1, wait_seconds=0) == []

    # --- 5. dashboard ------------------------------------------------------
    payload = build_dashboard(ET_DATE, store=store)
    assert payload["summary"]["game_count"] == 1
    assert payload["summary"]["trade_count"] == 1
    assert payload["summary"]["traded_count"] == 1
    # Bought at 0.32, recovery sell at 0.68 -> 0.36 x SHARE_COUNT shares.
    assert payload["summary"]["realised_pnl"] == pytest.approx(
        0.36 * settings.share_count
    )

    game_view = payload["games"][0]
    assert game_view["matchup"] == "76ers @ Nets"
    assert game_view["monitored_team"] == "76ers"
    assert game_view["buy_price"] == pytest.approx(0.32)
    assert game_view["sell_price"] == pytest.approx(0.68)
    assert len(payload["logs"]) >= 3  # armed, trigger fired, trade executed


def test_pipeline_skips_the_trigger_when_the_price_holds(aws_stack):
    """A price that does not halve must not produce a trade."""
    store = DynamoStore(table_name=TABLE)
    queue = TradeQueue(queue_url=settings.sqs_queue_url)

    run_daily_fetch(
        target_date=None,
        store=store,
        nba_client=FakeNbaClient(_schedule_game(), _standings()),
        gamma_client=FakeGammaClient([_gamma_event()]),
        notify=False,
    )
    clob = FakeClob(
        {
            "token-phi": {"best_bid": 0.67, "best_ask": 0.69, "mid": 0.68},
            "token-bkn": {"best_bid": 0.31, "best_ask": 0.33, "mid": 0.32},
        }
    )
    run_pregame_snapshot(
        target_date=None, store=store, client=clob, now_ts=START_TS - 20 * 60
    )

    from src.trade.listener import Listener

    listener = Listener(store=store, queue=queue, dry_run=True)
    listener.games = [store.get_game(SLUG)]

    # A tick to 0.60 is not a 50% drop from the 0.68 mid.
    frame = json.dumps(
        {
            "event_type": "price_change",
            "market": "token-phi",
            "price_changes": [
                {"price": "0.60", "size": "500", "side": "BUY", "best_bid": "0.60", "best_ask": "0.62"}
            ],
        }
    )
    asyncio.run(listener.handle_market_frame(frame))

    assert store.get_game(SLUG)["status"] == "armed"
    assert queue.receive(max_messages=1, wait_seconds=0) == []
