"""Lambda handler: daily NBA schedule fetch.

Runs on an EventBridge Scheduler schedule. Writes games to DynamoDB - there
is no git commit and no S3 log archive.
"""

import logging
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

from lambda_handlers.common import apply_secrets, configure_logging

logger = logging.getLogger(__name__)


def resolve_fetch_date(event: Dict[str, Any]) -> str:
    """The ET date to fetch: the event payload, else today in ET."""
    raw = event.get("date")
    if raw:
        return str(raw).strip()
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    configure_logging()
    apply_secrets()

    from datetime import date as _date

    from src.fetch.daily_fetch import run_daily_fetch

    target = _date.fromisoformat(resolve_fetch_date(event or {}))
    result = run_daily_fetch(target_date=target)

    return {
        "status": "ok",
        "job": "fetch-daily",
        "date": target.isoformat(),
        "matched": len(result.get("matched") or []),
        "unmatched": len(result.get("unmatched") or []),
        "games": [
            {
                "slug": g.get("slug"),
                "matchup": f"{g.get('away_team')} @ {g.get('home_team')}",
                "start_time": g.get("start_time"),
                "favourite_team": g.get("favourite_team"),
                "favourite_price": g.get("favourite_price"),
            }
            for g in result.get("matched") or []
        ],
    }
