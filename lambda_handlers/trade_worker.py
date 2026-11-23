"""Lambda handler: SQS trade worker.

Consumes ``trigger`` and ``fill`` messages from SQS. The handler is a thin
adapter - all the logic lives in ``src/trade/buy_sell.py`` so the same code
runs locally and in AWS.

The SQS event source mapping is configured with ``ReportBatchItemFailures``,
so a message that raises is returned as a batch item failure and is
redriven rather than being lost.
"""

import logging
from typing import Any, Dict, List

from lambda_handlers.common import apply_secrets, configure_logging

logger = logging.getLogger(__name__)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    configure_logging()
    apply_secrets()

    from src.trade.buy_sell import TradeWorker

    records = (event or {}).get("Records") or []
    worker = TradeWorker()

    batch_item_failures: List[Dict[str, Any]] = []
    processed = 0
    results: List[Dict[str, Any]] = []

    for record in records:
        message_id = str(record.get("messageId") or "")
        try:
            import json

            body = json.loads(record.get("body") or "{}")
            result = worker.handle_message(body)
            processed += 1
            results.append(result)
            logger.info("Processed message %s: %s", message_id, result.get("status"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to process message %s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {
        "status": "ok",
        "processed": processed,
        "failed": len(batch_item_failures),
        "batchItemFailures": batch_item_failures,
        "results": results,
    }
