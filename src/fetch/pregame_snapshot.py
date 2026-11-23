"""Pre-game price snapshot.

Runs every few minutes. For each game that is inside the pre-game window
(tip-off within ``PRE_GAME_SNAPSHOT_MINUTES``), it reads the live CLOB book
for **both** teams' tokens and records:

    pre_game_prices = {team: {bid, ask, mid, ts}}
    monitored_team   = the team with the higher mid
    status           = "armed"

The snapshot must be taken **before** tip-off: Polymarket clears resting
orders at the official start, so a price captured after tip-off is not the
pre-game price the recovery sell is based on.

Idempotent: a game that already has ``pre_game_snapshotted_at`` is skipped.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from config.settings import settings
from src.api.clob_client import ClobPriceClient
from src.api.dynamo_store import DynamoStore
from src.utils.market_parser import mid_from_bid_ask

logger = logging.getLogger(__name__)


def _now_ts() -> int:
    return int(time.time())


def _parse_start_ts(game: dict[str, Any]) -> Optional[int]:
    raw = game.get("start_ts")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    raw_iso = game.get("start_time")
    if not raw_iso:
        return None
    try:
        dt = datetime.fromisoformat(str(raw_iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def games_in_pregame_window(
    games: list[dict[str, Any]],
    *,
    now_ts: Optional[int] = None,
    window_minutes: Optional[int] = None,
    min_lead_minutes: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Games whose tip-off falls inside the pre-game window.

    The window is ``(now + min_lead, now + window]``. Games that have
    already started are excluded - their resting orders are cleared.
    """
    now = now_ts if now_ts is not None else _now_ts()
    window = window_minutes if window_minutes is not None else settings.pre_game_snapshot_minutes
    lead = min_lead_minutes if min_lead_minutes is not None else settings.snapshot_min_lead_minutes

    out: list[dict[str, Any]] = []
    for game in games:
        start = _parse_start_ts(game)
        if start is None:
            continue
        minutes = (start - now) / 60.0
        if lead <= minutes <= window:
            out.append(game)
    return out


def choose_monitored_team(
    prices: dict[str, dict[str, Any]]
) -> Optional[str]:
    """The team with the higher mid price - the pre-game favourite.

    Ties are broken by the higher ask, then alphabetically for determinism.
    """
    scored: list[tuple[float, float, str]] = []
    for team, snapshot in prices.items():
        mid = snapshot.get("mid")
        if mid is None:
            continue
        ask = snapshot.get("ask")
        scored.append((float(mid), float(ask) if ask is not None else -1.0, team))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    return scored[0][2]


def snapshot_game_prices(
    game: dict[str, Any], client: Optional[ClobPriceClient] = None
) -> dict[str, dict[str, Any]]:
    """Read the live book for both teams of a game."""
    clob = client or ClobPriceClient()
    teams = game.get("team_tokens") or {}

    prices: dict[str, dict[str, Any]] = {}
    ts = _now_ts()

    for role in ("away", "home"):
        entry = teams.get(role) or {}
        token_id = entry.get("token_id")
        name = entry.get("name") or role
        if not token_id:
            continue
        live = clob.get_bid_ask(str(token_id))
        mid = mid_from_bid_ask(live.get("best_bid"), live.get("best_ask"))
        prices[str(name)] = {
            "role": role,
            "abbreviation": entry.get("abbreviation"),
            "token_id": str(token_id),
            "bid": live.get("best_bid"),
            "ask": live.get("best_ask"),
            "mid": mid,
            "ts": ts,
        }
        logger.info(
            "Snapshot %s %s: bid=%s ask=%s mid=%s",
            game.get("slug"),
            name,
            live.get("best_bid"),
            live.get("best_ask"),
            mid,
        )
    return prices


def run_pregame_snapshot(
    *,
    target_date=None,
    store: Optional[DynamoStore] = None,
    client: Optional[ClobPriceClient] = None,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Snapshot every game inside the pre-game window.

    Returns ``{"armed": [...], "skipped": [...]}``.
    """
    dynamo = store or DynamoStore()
    clob = client or ClobPriceClient()
    now = now_ts if now_ts is not None else _now_ts()

    day = target_date.isoformat() if target_date is not None else None
    if day:
        items = dynamo.get_games_for_date(day)
    else:
        items = dynamo.scan_scheduled_games()
    games = [i for i in items if i.get("status") == "scheduled"]

    armed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for game in games:
        slug = str(game.get("slug") or "")
        if not slug:
            continue
        if game.get("pre_game_snapshotted_at"):
            skipped.append({"slug": slug, "reason": "already_snapshotted"})
            continue

        if not _in_window(game, now):
            skipped.append({"slug": slug, "reason": "outside_pregame_window"})
            continue

        prices = snapshot_game_prices(game, clob)
        if not prices:
            skipped.append({"slug": slug, "reason": "no_token_prices"})
            continue

        monitored = choose_monitored_team(prices)
        if monitored is None:
            skipped.append({"slug": slug, "reason": "no_mid_price"})
            continue

        threshold = prices[monitored]["mid"] * settings.price_drop_ratio
        dynamo.update_game(
            slug,
            {
                "pre_game_prices": prices,
                "pre_game_snapshotted_at": now,
                "pre_game_snapshot_ts": now,
                "monitored_team": monitored,
                "monitored_token_id": prices[monitored]["token_id"],
                "monitored_pre_game_mid": prices[monitored]["mid"],
                "monitored_pre_game_bid": prices[monitored]["bid"],
                "monitored_pre_game_ask": prices[monitored]["ask"],
                "opponent_team": next(
                    (t for t in prices if t != monitored),
                    None,
                ),
                "trigger_threshold": threshold,
                "status": "armed",
            },
        )
        dynamo.put_log(
            slug,
            "armed",
            {
                "monitored_team": monitored,
                "pre_game_mid": prices[monitored]["mid"],
                "trigger_threshold": threshold,
                "prices": prices,
            },
        )
        armed.append(
            {
                "slug": slug,
                "monitored_team": monitored,
                "pre_game_mid": prices[monitored]["mid"],
                "trigger_threshold": threshold,
            }
        )
        logger.info(
            "Armed %s: monitoring %s at mid %.4f, trigger at <= %.4f",
            slug,
            monitored,
            prices[monitored]["mid"],
            threshold,
        )

    logger.info(
        "Pre-game snapshot: %d armed, %d skipped", len(armed), len(skipped)
    )
    return {"armed": armed, "skipped": skipped, "timestamp": now}


def _in_window(game: dict[str, Any], now_ts: int) -> bool:
    start = _parse_start_ts(game)
    if start is None:
        return False
    minutes = (start - now_ts) / 60.0
    return settings.snapshot_min_lead_minutes <= minutes <= settings.pre_game_snapshot_minutes
