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
    apply_secrets()

    from datetime import date as _date

    from src.trade.scaler import run_scale_job

    target = _date.fromisoformat(resolve_date(event or {}))
    result = run_scale_job(target_date=target)

    window = result.get("window") or {}
    return {
        "status": "ok",
        "job": "scale-listener",
        "date": target.isoformat(),
        "desired_count": result.get("desired_count"),
        "changed": result.get("changed"),
        "window_start_ts": window.get("start_ts"),
        "window_end_ts": window.get("end_ts"),
        "window_open": result.get("window_open"),
    }
