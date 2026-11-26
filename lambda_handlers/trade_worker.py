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
    logger.info("JOB START trade-worker")
    
    try:
        apply_secrets()
        logger.info("trade-worker: secrets loaded")
    except Exception as e:
        logger.error("trade-worker: FAILED to load secrets: %s", e, exc_info=True)
        raise

    from src.trade.buy_sell import TradeWorker

    records = (event or {}).get("Records") or []
    logger.info("trade-worker: processing %d SQS messages", len(records))
    
    worker = TradeWorker()

    batch_item_failures: List[Dict[str, Any]] = []
    processed = 0
    failed = 0
    results: List[Dict[str, Any]] = []

    for idx, record in enumerate(records, 1):
        message_id = str(record.get("messageId") or "")
        logger.info("trade-worker: message %d/%d id=%s", idx, len(records), message_id)
        try:
            import json

            body = json.loads(record.get("body") or "{}")
            msg_type = body.get("type", "unknown")
            result = worker.handle_message(body)
            processed += 1
            results.append(result)
            logger.info("trade-worker: message %s type=%s status=%s", message_id, msg_type, result.get("status"))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception("trade-worker: message %s FAILED: %s", message_id, exc)
            batch_item_failures.append({"itemIdentifier": message_id})

    logger.info("trade-worker: batch done, processed=%d failed=%d", processed, failed)
    logger.info("JOB END trade-worker ok")
    
    return {
        "status": "ok",
        "processed": processed,
        "failed": failed,
        "batchItemFailures": batch_item_failures,
    }
        "batchItemFailures": batch_item_failures,
        "results": results,
    }
