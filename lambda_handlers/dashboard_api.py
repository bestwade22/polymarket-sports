"""Lambda handler: dashboard API.

Serves the dashboard payload as JSON. The browser never sees AWS
credentials - it calls this through API Gateway.

Query parameters:
    ``date``   - the ET date, YYYY-MM-DD. Defaults to today.
    ``live``   - ``1`` to fetch live CLOB prices for mark-to-market.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

from lambda_handlers.common import apply_secrets, configure_logging

logger = logging.getLogger(__name__)


def _parse_date(raw: Any) -> str:
    if raw:
        try:
            return datetime.strptime(str(raw), "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _query_params(event: Dict[str, Any]) -> Dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    if isinstance(params, dict):
        return params
    return {}


def _response(status: int, payload: Any) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache",
        },
        "body": json.dumps(payload, default=str),
    }


def fetch_live_prices(games: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Live best bid/ask for each game's monitored token."""
    from src.api.clob_client import ClobPriceClient

    client = ClobPriceClient()
    out: dict[str, dict[str, Any]] = {}
    for game in games:
        token_id = game.get("monitored_token_id")
        slug = game.get("slug")
        if not token_id or not slug:
            continue
        live = client.get_bid_ask(str(token_id))
        out[str(slug)] = live
    return out


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    configure_logging()
    logger.info("=" * 60)
    logger.info("JOB START: dashboard-api (HTTP handler)")
    logger.info("=" * 60)
    
    try:
        apply_secrets()
        logger.info("Step 1/4: Secrets loaded")
    except Exception as e:
        logger.error("Step 1/4 FAILED: %s", e, exc_info=True)
        return _response(500, {"error": "Failed to load secrets"})

    from src.analysis.exporter import build_dashboard

    params = _query_params(event or {})
    day = _parse_date(params.get("date"))
    logger.info("Step 2/4: Parsed date: %s, live_prices=%s", day, params.get("live"))

    try:
        logger.info("Step 3/4: Building dashboard for %s", day)
        payload = build_dashboard(day)
        logger.info("Step 3/4: ✓ Dashboard built: %d games, %d trades, %d logs",
                   len(payload.get("games") or []),
                   len(payload.get("trades") or []),
                   len(payload.get("logs") or []))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Step 3/4 FAILED: Dashboard build failed for %s", day)
        return _response(500, {"error": str(exc), "date": day})

    if str(params.get("live", "")).strip() in ("1", "true", "yes"):
        logger.info("Step 4/4: Fetching live CLOB prices for %d games...",
                   len(payload.get("games") or []))
        try:
            live = fetch_live_prices(payload.get("games") or [])
            logger.info("Step 4/4: ✓ Fetched live prices for %d games", len(live))
            payload = build_dashboard(day, live_prices=live)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Step 4/4 WARNING: Live price fetch failed: %s", exc)
            # Continue without live prices, don't fail the whole response
    else:
        logger.info("Step 4/4: Skipped live price fetch (not requested)")

    logger.info("=" * 60)
    logger.info("JOB SUCCESS: dashboard-api completed")
    logger.info("=" * 60)
    return _response(200, payload)
