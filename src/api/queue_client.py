"""SQS queue access for trigger messages.

The listener publishes a ``trigger`` message when the price rule fires; the
trade-worker Lambda consumes it. A ``fill`` message is published for late
fills that arrive on the user WebSocket after the worker has already moved on.

Both the AWS SQS API and the local moto emulator are supported through the
``SQS_ENDPOINT_URL`` environment variable.
"""

import json
import logging
from typing import Any, Optional

import boto3

from config.settings import settings

logger = logging.getLogger(__name__)

TRIGGER_MESSAGE = "trigger"
FILL_MESSAGE = "fill"


class TradeQueue:
    """Publish and consume trade messages."""

    def __init__(
        self,
        queue_url: Optional[str] = None,
        queue_name: Optional[str] = None,
        endpoint_url: Optional[str] = None,
    ):
        self.queue_url = queue_url or settings.sqs_queue_url
        self.queue_name = queue_name or "SportsBotTrades"
        self.endpoint_url = endpoint_url or settings.sqs_endpoint_url or None
        self._client = None

    @property
    def client(self):
        if self._client is None:
            kwargs: dict[str, Any] = {"region_name": settings.aws_region}
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            self._client = boto3.client("sqs", **kwargs)
        return self._client

    def resolve_queue_url(self) -> str:
        """Look up the queue URL by name when it is not configured."""
        if self.queue_url:
            return self.queue_url
        resp = self.client.get_queue_url(QueueName=self.queue_name)
        self.queue_url = resp["QueueUrl"]
        return self.queue_url

    def publish(self, message: dict[str, Any]) -> str:
        """Publish a message and return its SQS message id."""
        queue_url = self.resolve_queue_url()
        resp = self.client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message, default=str),
        )
        logger.info("Published %s message to SQS: %s", message.get("type"), message.get("slug"))
        return str(resp.get("MessageId", ""))

    def publish_trigger(self, payload: dict[str, Any]) -> str:
        body = dict(payload)
        body["type"] = TRIGGER_MESSAGE
        return self.publish(body)

    def publish_fill(self, payload: dict[str, Any]) -> str:
        body = dict(payload)
        body["type"] = FILL_MESSAGE
        return self.publish(body)

    def receive(self, max_messages: int = 1, wait_seconds: int = 10) -> list[dict[str, Any]]:
        """Receive messages, returning parsed bodies with receipt handles."""
        queue_url = self.resolve_queue_url()
        resp = self.client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=120,
        )
        out: list[dict[str, Any]] = []
        for raw in resp.get("Messages", []):
            try:
                body = json.loads(raw.get("Body") or "{}")
            except (TypeError, ValueError):
                body = {"raw": raw.get("Body")}
            out.append(
                {
                    "body": body,
                    "receipt_handle": raw.get("ReceiptHandle", ""),
                    "message_id": raw.get("MessageId", ""),
                }
            )
        return out

    def delete(self, receipt_handle: str) -> bool:
        """Delete a processed message."""
        if not receipt_handle:
            return False
        queue_url = self.resolve_queue_url()
        self.client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        return True

    def ensure_queue(self) -> str:
        """Create the queue if it does not exist (local dev convenience)."""
        try:
            resp = self.client.get_queue_url(QueueName=self.queue_name)
            self.queue_url = resp["QueueUrl"]
            return self.queue_url
        except Exception:
            pass
        resp = self.client.create_queue(QueueName=self.queue_name)
        self.queue_url = resp["QueueUrl"]
        logger.info("Created local SQS queue %s", self.queue_name)
        return self.queue_url
