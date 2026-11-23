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
    apply_secrets()

    from datetime import date as _date

    from src.fetch.pregame_snapshot import run_pregame_snapshot

    target = _date.fromisoformat(resolve_date(event or {}))
    result = run_pregame_snapshot(target_date=target)

    return {
        "status": "ok",
        "job": "snapshot-price",
        "date": target.isoformat(),
        "armed": result.get("armed") or [],
        "skipped_count": len(result.get("skipped") or []),
    }
