"""Lambda handler: daily NBA schedule fetch.

Runs on an EventBridge Scheduler schedule. Writes games to DynamoDB - there
is no git commit and no S3 log archive.

The handler normally fetches a *range* of ET days: today's, plus the days
ahead of it (see ``DAILY_FETCH_DAYS_AHEAD``). Fetching ahead means the next
evening's slate is staged - games written and its listener window derived -
before it is needed, so a late schedule change or a rescheduled tip-off is
picked up with time to spare.

Comprehensive logging added for each step (Step A-I) with detailed metrics
for diagnosis in CloudWatch. See .cursor/rules/logging.mdc for log format.
"""

import logging
from typing import Any, Dict

from lambda_handlers.common import apply_secrets, configure_logging

logger = logging.getLogger(__name__)


def resolve_fetch_dates(event: Dict[str, Any]) -> list[str]:
    """The ET dates to fetch, as YYYY-MM-DD strings.

    An explicit ``date`` in the event payload wins and fetches only that
    day. Otherwise the configured range is used.
    """
    raw = (event or {}).get("date")
    if raw:
        return [str(raw).strip()]

    from config.settings import fetch_target_dates

    return [day.isoformat() for day in fetch_target_dates()]


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    configure_logging()
    logger.info("JOB START fetch-daily")
    
    try:
        apply_secrets()
        logger.info("fetch-daily: secrets loaded")
    except Exception as e:
        logger.error("fetch-daily: FAILED to load secrets: %s", e, exc_info=True)
        raise

    from datetime import date as _date

    from src.fetch.daily_fetch import run_daily_fetch

    days = resolve_fetch_dates(event or {})
    logger.info("fetch-daily: target ET dates %s", days)

    summaries: list[Dict[str, Any]] = []
    total_matched = 0
    total_unmatched = 0
    
    for day in days:
        target = _date.fromisoformat(day)
        logger.info("fetch-daily: starting ET %s", target.isoformat())
        try:
            result = run_daily_fetch(target_date=target)
            matched_count = len(result.get("matched") or [])
            unmatched_count = len(result.get("unmatched") or [])
            total_matched += matched_count
            total_unmatched += unmatched_count
            
            summaries.append(
                {
                    "date": target.isoformat(),
                    "matched": matched_count,
                    "unmatched": unmatched_count,
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
            )
            logger.info(
                "fetch-daily: ET %s done, %d matched, %d unmatched",
                target.isoformat(),
                matched_count,
                unmatched_count,
            )
        except Exception as e:
            logger.error("fetch-daily: ET %s FAILED: %s", target.isoformat(), e, exc_info=True)
            raise

    logger.info(
        "fetch-daily: complete, %d matched and %d unmatched across %d days",
        total_matched,
        total_unmatched,
        len(days),
    )
    
    response = {
        "status": "ok",
        "job": "fetch-daily",
        "date": days[0] if days else None,
        "dates": days,
        "days": summaries,
        "summary": {
            "total_matched": total_matched,
            "total_unmatched": total_unmatched,
        }
    }
    
    logger.info("JOB END fetch-daily ok")
    return response
