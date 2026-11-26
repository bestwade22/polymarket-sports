"""Lambda handler: scale the Fargate listener to match the day's window.

Runs on a short schedule (every five minutes). Reads the listener window that
the daily fetch derived from the day's tip-off times and sets the ECS
service's desired count to 1 while the window is open and 0 when it is not.

Idempotent: setting the desired count to the value it already has is a
no-op, so the job can run as often as needed.
"""

import logging
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

from lambda_handlers.common import apply_secrets, configure_logging

logger = logging.getLogger(__name__)


def resolve_date(event: Dict[str, Any]) -> str:
    """The ET date whose window should be applied."""
    raw = event.get("date")
    if raw:
        return str(raw).strip()
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    configure_logging()
    logger.info("JOB START scale-listener")
    
    try:
        apply_secrets()
        logger.info("scale-listener: secrets loaded")
    except Exception as e:
        logger.error("scale-listener: FAILED to load secrets: %s", e, exc_info=True)
        raise

    from datetime import date as _date

    from src.trade.scaler import run_scale_job

    target_date_str = resolve_date(event or {})
    logger.info("scale-listener: target ET date %s", target_date_str)
    
    try:
        target = _date.fromisoformat(target_date_str)
        logger.info("scale-listener: running for ET %s", target.isoformat())
        result = run_scale_job(target_date=target)
        
        window = result.get("window") or {}
        desired_count = result.get("desired_count")
        changed = result.get("changed")
        window_open = result.get("window_open")
        
        logger.info("scale-listener: done, desired=%s changed=%s window_open=%s",
                   desired_count, changed, window_open)
    except Exception as e:
        logger.error("scale-listener: FAILED: %s", e, exc_info=True)
        raise

    logger.info("scale-listener: building response")
    response = {
        "status": "ok",
        "job": "scale-listener",
        "date": target.isoformat(),
        "desired_count": desired_count,
        "changed": changed,
        "window_start_ts": window.get("start_ts"),
        "window_end_ts": window.get("end_ts"),
        "window_open": window_open,
    }
    
    logger.info("JOB END scale-listener ok")
    return response
