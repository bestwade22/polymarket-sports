"""Lambda handler: pre-game price snapshot.

Runs on a short schedule (every 5 minutes). Arms games that are inside the
pre-game window by recording both teams' live book prices and choosing the
favourite to monitor.
"""

import logging
from datetime import datetime
from typing import Any, Dict

from lambda_handlers.common import apply_secrets, configure_logging

logger = logging.getLogger(__name__)


def resolve_date(event: Dict[str, Any]) -> str:
    """The ET date to snapshot: the event payload, else today in ET."""
    raw = event.get("date")
    if raw:
        return str(raw).strip()
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    configure_logging()
    logger.info("JOB START snapshot-price")
    
    try:
        apply_secrets()
        logger.info("snapshot-price: secrets loaded")
    except Exception as e:
        logger.error("snapshot-price: FAILED to load secrets: %s", e, exc_info=True)
        raise

    from datetime import date as _date

    from src.fetch.pregame_snapshot import run_pregame_snapshot

    target_date_str = resolve_date(event or {})
    logger.info("snapshot-price: target ET date %s", target_date_str)
    
    try:
        target = _date.fromisoformat(target_date_str)
        logger.info("snapshot-price: running for ET %s", target.isoformat())
        result = run_pregame_snapshot(target_date=target)
        
        armed_count = len(result.get("armed") or [])
        skipped_count = len(result.get("skipped") or [])
        
        logger.info("snapshot-price: done, armed=%d skipped=%d",
                   armed_count, skipped_count)
    except Exception as e:
        logger.error("snapshot-price: FAILED: %s", e, exc_info=True)
        raise

    logger.info("snapshot-price: building response")
    response = {
        "status": "ok",
        "job": "snapshot-price",
        "date": target.isoformat(),
        "armed": result.get("armed") or [],
        "armed_count": armed_count,
        "skipped": result.get("skipped") or [],
        "skipped_count": skipped_count,
    }
    
    logger.info("JOB END snapshot-price ok")
    return response
