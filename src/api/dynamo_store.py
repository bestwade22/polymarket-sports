"""DynamoDB single-table access layer.

One table, ``SportsBotState``, holds every piece of state the bot has:

    pk = GAME#nba-phi-bkn-2026-11-16   sk = META
    pk = GAME#nba-phi-bkn-2026-11-16   sk = TRADE#<ts>#<id>
    pk = GAME#nba-phi-bkn-2026-11-16   sk = LOG#<ts>#<seq>
    pk = DATE#2026-11-17               sk = BATCH
    pk = DATE#2026-11-17               sk = WINDOW

GSI1 is ``gsi1pk = DATE#<date>`` with ``gsi1sk = <ts>`` so that the dashboard
can pull a whole day's games, trades and logs in one query.

Every item carries ``expires_at`` (epoch seconds) for DynamoDB TTL.

DynamoDB is the *only* store: there is no git commit and no S3 log archive.

The low-level ``boto3.client("dynamodb")`` API is used throughout rather than
the ``resource`` API, because the two disagree about serialisation: the
resource layer auto-deserialises reads but still expects raw AttributeValues
in ``ExpressionAttributeValues``. Mixing them is a silent-corruption trap, so
everything goes through the client with explicit serialisation.
"""

import logging
import time
from decimal import Decimal
from typing import Any, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from config.settings import settings

logger = logging.getLogger(__name__)

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

GAME_PK_PREFIX = "GAME#"
DATE_PK_PREFIX = "DATE#"
META_SK = "META"
BATCH_SK = "BATCH"
WINDOW_SK = "WINDOW"
GSI1_PK = "gsi1pk"
GSI1_SK = "gsi1sk"


class ConditionalWriteFailed(Exception):
    """Raised when a conditional write is rejected by DynamoDB."""


def game_pk(slug: str) -> str:
    return f"{GAME_PK_PREFIX}{slug}"


def date_pk(day: str) -> str:
    return f"{DATE_PK_PREFIX}{day}"


def json_safe(value: Any) -> Any:
    """Best-effort conversion of nested structures to DynamoDB-safe types."""
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float):
        return Decimal(str(round(value, 10)))
    return value


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    """Drop None values and convert nested structures to DynamoDB-safe types."""
    cleaned: dict[str, Any] = {}
    for key, value in item.items():
        if value is None:
            continue
        if isinstance(value, set):
            cleaned[key] = sorted(str(v) for v in value)
        elif isinstance(value, float):
            cleaned[key] = Decimal(str(round(value, 10)))
        elif isinstance(value, (dict, list, tuple)):
            cleaned[key] = json_safe(value)
        else:
            cleaned[key] = value
    return cleaned


def _serialize(value: Any) -> dict[str, Any]:
    return _serializer.serialize(value)


def _to_python(item: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not item:
        return None
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


class DynamoStore:
    """Read/write access to the SportsBotState table."""

    def __init__(self, table_name: Optional[str] = None, endpoint_url: Optional[str] = None):
        self.table_name = table_name or settings.dynamodb_table
        self.endpoint_url = endpoint_url or settings.dynamodb_endpoint_url or None
        self._client = None

    # --- client ------------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            kwargs: dict[str, Any] = {"region_name": settings.aws_region}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            self._client = boto3.client("dynamodb", **kwargs)
        return self._client

    # --- writes ------------------------------------------------------------

    def put_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Write an item, refreshing the TTL attribute."""
        payload = _clean(item)
        payload["expires_at"] = self._ttl_timestamp()
        self.client.put_item(
            TableName=self.table_name,
            Item={k: _serialize(v) for k, v in payload.items()},
        )
        return payload

    def put_game(self, slug: str, meta: dict[str, Any]) -> dict[str, Any]:
        """Write the META item for a game."""
        payload = dict(meta)
        payload.update({"pk": game_pk(slug), "sk": META_SK})
        payload.setdefault("slug", slug)
        return self.put_item(payload)

    def update_game(self, slug: str, updates: dict[str, Any]) -> bool:
        """Update fields on a game's META item.

        Returns False when the item does not exist.
        """
        payload = _clean(updates)
        payload.pop("pk", None)
        payload.pop("sk", None)
        payload.pop("expires_at", None)
        if not payload:
            return True
        payload["expires_at"] = self._ttl_timestamp()
        self.client.update_item(
            TableName=self.table_name,
            Key={"pk": _serialize(game_pk(slug)), "sk": _serialize(META_SK)},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in payload),
            ExpressionAttributeNames={f"#{k}": k for k in payload},
            ExpressionAttributeValues={f":{k}": _serialize(v) for k, v in payload.items()},
        )
        return True

    def put_trade(
        self, slug: str, trade: dict[str, Any], *, trade_id: Optional[str] = None
    ) -> str:
        """Write a TRADE item under a game and return its sort key.

        The item inherits the game's GSI1 keys so that a day query returns
        the game, its trades and its logs together.
        """
        ts = int(trade.get("ts") or time.time())
        tid = trade_id or str(trade.get("trade_id") or f"{ts}")
        sort_key = f"TRADE#{ts}#{tid}"
        payload = dict(trade)
        payload.update({"pk": game_pk(slug), "sk": sort_key})
        payload["ts"] = ts
        payload["trade_id"] = tid
        payload.setdefault("slug", slug)
        self._inherit_gsi_keys(slug, payload, ts)
        self.put_item(payload)
        return sort_key

    def put_log(
        self, slug: str, message: str, fields: Optional[dict[str, Any]] = None
    ) -> str:
        """Write a LOG item under a game and return its sort key."""
        ts = int(time.time())
        seq = f"{ts}#{len(message):04d}"
        sort_key = f"LOG#{seq}"
        payload: dict[str, Any] = {
            "pk": game_pk(slug),
            "sk": sort_key,
            "ts": ts,
            "message": message,
            "slug": slug,
        }
        if fields:
            payload["fields"] = fields
        self._inherit_gsi_keys(slug, payload, ts)
        self.put_item(payload)
        return sort_key

    def _inherit_gsi_keys(self, slug: str, payload: dict[str, Any], ts: int) -> None:
        """Copy the game's GSI1 partition key onto a child item.

        The sort key is the item's own timestamp so that trades and logs
        interleave with the game in chronological order.
        """
        if payload.get(GSI1_PK):
            return
        game = self.get_game(slug)
        if game and game.get(GSI1_PK):
            payload[GSI1_PK] = game[GSI1_PK]
        else:
            logger.debug(
                "No GSI1 key found for game %s; child item may not appear in day queries",
                slug,
            )
        payload.setdefault(GSI1_SK, f"{ts:012d}")

    def put_daily_batch(self, day: str, batch: dict[str, Any]) -> None:
        """Write the DATE#<day>/BATCH item holding the day's snapshot."""
        payload = dict(batch)
        payload.update({"pk": date_pk(day), "sk": BATCH_SK})
        payload["date"] = day
        self.put_item(payload)

    def put_schedule_window(self, day: str, window: dict[str, Any]) -> None:
        """Write the DATE#<day>/WINDOW item holding the listener window.

        The window is the derived on-window for the Fargate listener: when it
        should scale up and when it should scale down. It is written next to
        the day's batch so that the scaler and the dashboard can read it.
        """
        payload = dict(window)
        payload.update({"pk": date_pk(day), "sk": WINDOW_SK})
        payload["date"] = day
        self.put_item(payload)

    def get_schedule_window(self, day: str) -> Optional[dict[str, Any]]:
        """The listener window for a date, or None when it has not been computed."""
        resp = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": _serialize(date_pk(day)), "sk": _serialize(WINDOW_SK)},
            ConsistentRead=True,
        )
        return _to_python(resp.get("Item"))

    # --- conditional writes ------------------------------------------------

    def claim_trigger(self, slug: str, claimed_at: Optional[int] = None) -> bool:
        """Atomically claim the trigger for a game.

        Uses a conditional update on ``triggered_at`` so that two listener
        replicas can never both fire for the same game. Returns True for the
        caller that won the claim.
        """
        ts = int(claimed_at if claimed_at is not None else time.time())
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={"pk": _serialize(game_pk(slug)), "sk": _serialize(META_SK)},
                UpdateExpression="SET triggered_at = :ts, trigger_claimed_at = :ts",
                ConditionExpression="attribute_exists(pk) AND attribute_not_exists(triggered_at)",
                ExpressionAttributeValues={":ts": _serialize(ts)},
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                logger.info("Trigger for %s already claimed; skipping", slug)
                return False
            raise

    def claim_game_state(
        self, slug: str, field: str, value: Any, *, expected_absent: bool = True
    ) -> bool:
        """Set a field on a game only if it is (still) absent."""
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key={"pk": _serialize(game_pk(slug)), "sk": _serialize(META_SK)},
                UpdateExpression=f"SET #{field} = :v",
                ConditionExpression=(
                    f"attribute_exists(pk) AND attribute_not_exists(#{field})"
                    if expected_absent
                    else "attribute_exists(pk)"
                ),
                ExpressionAttributeNames={f"#{field}": field},
                ExpressionAttributeValues={":v": _serialize(value)},
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False
            raise

    # --- reads -------------------------------------------------------------

    def get_game(self, slug: str) -> Optional[dict[str, Any]]:
        resp = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": _serialize(game_pk(slug)), "sk": _serialize(META_SK)},
            ConsistentRead=True,
        )
        return _to_python(resp.get("Item"))

    def get_games_for_date(self, day: str) -> list[dict[str, Any]]:
        """All items for a date via GSI1, sorted by gsi1sk."""
        resp = self.client.query(
            TableName=self.table_name,
            IndexName="GSI1",
            KeyConditionExpression=f"{GSI1_PK} = :pk",
            ExpressionAttributeValues={":pk": _serialize(date_pk(day))},
        )
        return [_to_python(i) for i in resp.get("Items", [])]

    def get_game_items(self, slug: str) -> list[dict[str, Any]]:
        """All items under one game (meta, trades, logs)."""
        resp = self.client.query(
            TableName=self.table_name,
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": _serialize(game_pk(slug))},
            ConsistentRead=True,
        )
        return [_to_python(i) for i in resp.get("Items", [])]

    def get_trades(self, slug: str) -> list[dict[str, Any]]:
        """Trade items for a game, oldest first."""
        items = self.get_game_items(slug)
        trades = [i for i in items if str(i.get("sk", "")).startswith("TRADE#")]
        trades.sort(key=lambda i: int(i.get("ts") or 0))
        return trades

    def get_logs(self, slug: str) -> list[dict[str, Any]]:
        """Log items for a game, oldest first."""
        items = self.get_game_items(slug)
        logs = [i for i in items if str(i.get("sk", "")).startswith("LOG#")]
        logs.sort(key=lambda i: str(i.get("sk", "")))
        return logs

    def get_daily_batch(self, day: str) -> Optional[dict[str, Any]]:
        resp = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": _serialize(date_pk(day)), "sk": _serialize(BATCH_SK)},
            ConsistentRead=True,
        )
        return _to_python(resp.get("Item"))

    def scan_armed_games(self, day: Optional[str] = None) -> list[dict[str, Any]]:
        """Games with status 'armed' for a date (or every armed game)."""
        filter_expression = "#sk = :sk AND #status = :status"
        names = {"#sk": "sk", "#status": "status"}
        values = {":sk": _serialize(META_SK), ":status": _serialize("armed")}
        if day:
            resp = self.client.query(
                TableName=self.table_name,
                IndexName="GSI1",
                KeyConditionExpression=f"{GSI1_PK} = :pk",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={**values, ":pk": _serialize(date_pk(day))},
                FilterExpression=filter_expression,
            )
        else:
            resp = self.client.scan(
                TableName=self.table_name,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                FilterExpression=filter_expression,
            )
        return [_to_python(i) for i in resp.get("Items", [])]

    def scan_scheduled_games(self, day: Optional[str] = None) -> list[dict[str, Any]]:
        """Games with status 'scheduled' for a date (or every scheduled game)."""
        filter_expression = "#sk = :sk AND #status = :status"
        names = {"#sk": "sk", "#status": "status"}
        values = {":sk": _serialize(META_SK), ":status": _serialize("scheduled")}
        if day:
            resp = self.client.query(
                TableName=self.table_name,
                IndexName="GSI1",
                KeyConditionExpression=f"{GSI1_PK} = :pk",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={**values, ":pk": _serialize(date_pk(day))},
                FilterExpression=filter_expression,
            )
        else:
            resp = self.client.scan(
                TableName=self.table_name,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                FilterExpression=filter_expression,
            )
        return [_to_python(i) for i in resp.get("Items", [])]

    # --- helpers -----------------------------------------------------------

    def _ttl_timestamp(self) -> int:
        return int(time.time()) + settings.ttl_days * 86400

    def ensure_table(self) -> None:
        """Create the table if it does not exist (local dev convenience)."""
        try:
            self.client.describe_table(TableName=self.table_name)
            return
        except ClientError:
            pass
        self.client.create_table(
            TableName=self.table_name,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": GSI1_PK, "AttributeType": "S"},
                {"AttributeName": GSI1_SK, "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": GSI1_PK, "KeyType": "HASH"},
                        {"AttributeName": GSI1_SK, "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        try:
            self.client.update_time_to_live(
                TableName=self.table_name,
                TimeToLiveSpecification={
                    "Enabled": True,
                    "AttributeName": "expires_at",
                },
            )
        except ClientError as exc:
            logger.warning("Could not enable TTL: %s", exc)
