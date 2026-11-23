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
    apply_secrets()

    from src.analysis.exporter import build_dashboard

    params = _query_params(event or {})
    day = _parse_date(params.get("date"))

    try:
        payload = build_dashboard(day)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Dashboard build failed for %s", day)
        return _response(500, {"error": str(exc), "date": day})

    if str(params.get("live", "")).strip() in ("1", "true", "yes"):
        try:
            live = fetch_live_prices(payload.get("games") or [])
            payload = build_dashboard(day, live_prices=live)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Live price fetch failed; serving without MTM: %s", exc)

    return _response(200, payload)
