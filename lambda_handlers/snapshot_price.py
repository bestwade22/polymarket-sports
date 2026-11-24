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
    logger.info("=" * 60)
    logger.info("JOB START: snapshot-price (Lambda handler)")
    logger.info("=" * 60)
    
    try:
        apply_secrets()
        logger.info("Step 1/3: Secrets loaded")
    except Exception as e:
        logger.error("Step 1/3 FAILED: %s", e, exc_info=True)
        raise

    from datetime import date as _date

    from src.fetch.pregame_snapshot import run_pregame_snapshot

    target_date_str = resolve_date(event or {})
    logger.info("Step 2/3: Resolved target date: %s", target_date_str)
    
    try:
        target = _date.fromisoformat(target_date_str)
        logger.info("Step 2/3: Running pre-game snapshot for ET date %s", target.isoformat())
        result = run_pregame_snapshot(target_date=target)
        
        armed_count = len(result.get("armed") or [])
        skipped_count = len(result.get("skipped") or [])
        
        logger.info("Step 2/3: ✓ Snapshot completed: armed=%d skipped=%d",
                   armed_count, skipped_count)
    except Exception as e:
        logger.error("Step 2/3 FAILED: %s", e, exc_info=True)
        raise

    logger.info("Step 3/3: Preparing response")
    response = {
        "status": "ok",
        "job": "snapshot-price",
        "date": target.isoformat(),
        "armed": result.get("armed") or [],
        "armed_count": armed_count,
        "skipped": result.get("skipped") or [],
        "skipped_count": skipped_count,
    }
    
    logger.info("=" * 60)
    logger.info("JOB SUCCESS: snapshot-price completed")
    logger.info("=" * 60)
    return response
