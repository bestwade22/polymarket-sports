"""Daily batch job: fetch the NBA schedule and arm Polymarket games.

Pipeline:

1. API Ninjas gives the authoritative schedule (UTC tip-off) and standings.
2. Gamma gives the Polymarket NBA events and their moneyline markets.
3. The two are matched on the team abbreviation pair plus the ET date.
4. One ``GAME#<slug>/META`` item is written per matched game, plus one
   ``DATE#<day>/BATCH`` item holding the whole day's snapshot.

DynamoDB is the only store - nothing is written to git.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from config.settings import settings, today_et
from src.api.dynamo_store import DynamoStore
from src.api.gamma_sports import (
    GammaSportsClient,
    build_team_token_map,
    event_start_utc,
    event_teams,
    summarise_event,
)
from src.api.nba_client import NbaClient
from src.api.schedule_matcher import (
    game_start_utc,
    match_schedule,
    unmatched_label,
)
from src.trade.window import compute_window, describe_window

logger = logging.getLogger(__name__)


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


def build_game_meta(
    game: dict[str, Any],
    event: dict[str, Any],
    standings: dict[str, dict[str, Any]],
    *,
    target_date: date,
) -> dict[str, Any]:
    """Assemble the META item for one matched game."""
    summary = summarise_event(event)
    moneyline = GammaSportsClient.parse_moneyline_market(event) or {}
    team_tokens = build_team_token_map(event, moneyline)

    start = game_start_utc(game) or event_start_utc(event)
    away_abbr = summary.get("away_abbr") or ""
    home_abbr = summary.get("home_abbr") or ""

    away_standing = standings.get(away_abbr)
    home_standing = standings.get(home_abbr)

    meta: dict[str, Any] = {
        "slug": summary.get("slug"),
        "status": "scheduled",
        "start_time": _iso(start),
        "start_ts": int(start.timestamp()) if start else None,
        "et_date": summary.get("et_date"),
        "fetch_date": target_date.isoformat(),
        "event_id": summary.get("event_id"),
        "game_id": summary.get("game_id"),
        "title": summary.get("title"),
        "question": moneyline.get("question"),
        "market_id": summary.get("market_id"),
        "condition_id": summary.get("condition_id"),
        "tick_size": summary.get("tick_size"),
        "order_min_size": summary.get("order_min_size"),
        "neg_risk": summary.get("neg_risk"),
        "open_interest": summary.get("open_interest"),
        "volume": summary.get("volume"),
        "liquidity": summary.get("liquidity"),
        "away_team": summary.get("away_team"),
        "away_abbr": away_abbr,
        "away_token_id": summary.get("away_token_id"),
        "away_price": summary.get("away_price"),
        "away_record": summary.get("away_record"),
        "home_team": summary.get("home_team"),
        "home_abbr": home_abbr,
        "home_token_id": summary.get("home_token_id"),
        "home_price": summary.get("home_price"),
        "home_record": summary.get("home_record"),
        "team_tokens": team_tokens,
        "away_standings": away_standing,
        "home_standings": home_standing,
        # GSI1 partition: the game's own ET date, so that the game's trades and
        # logs land in the same day query as the game itself. The sort key is
        # a zero-padded timestamp string, because GSI1's range key is typed S.
        "gsi1pk": f"DATE#{summary.get('et_date') or target_date.isoformat()}",
        "gsi1sk": f"{int(start.timestamp()):012d}" if start else "0",
    }
    return meta


def pick_favourite(meta: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The team with the higher pre-game price.

    Prefers the Gamma ``outcomePrices`` captured at fetch time; falls back to
    the live book when the Gamma price is missing.
    """
    away = meta.get("away_price")
    home = meta.get("home_price")
    if away is None and home is None:
        return None
    if away is None:
        return {"role": "home", "name": meta.get("home_team"), "price": home}
    if home is None:
        return {"role": "away", "name": meta.get("away_team"), "price": away}
    if float(away) >= float(home):
        return {"role": "away", "name": meta.get("away_team"), "price": away}
    return {"role": "home", "name": meta.get("home_team"), "price": home}


def _schedule_window(window: dict[str, Any], day: str) -> None:
    """Create the one-shot schedules that scale the listener for this window.

    Best effort: a failure here must not fail the daily fetch, because the
    periodic scale job is the fallback that will still bring the listener up.

    The schedules are named per day, so the days the fetch covers at once
    (today and the days ahead) each keep their own pair.
    """
    role_arn = settings.scheduler_role_arn
    if not role_arn:
        logger.info(
            "No SCHEDULER_ROLE_ARN configured; relying on the periodic scale job"
        )
        return
    try:
        from src.trade.scaler import schedule_window_schedules

        created = schedule_window_schedules(window, role_arn=role_arn, day=day)
        logger.info("Listener window schedules: %s", ", ".join(created) or "(none)")
    except Exception:
        logger.exception("Could not create the listener window schedules")


def run_daily_fetch(
    *,
    target_date: Optional[date] = None,
    store: Optional[DynamoStore] = None,
    nba_client: Optional[NbaClient] = None,
    gamma_client: Optional[GammaSportsClient] = None,
    notify: bool = True,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Fetch, match and persist the day's NBA games.

    Returns a summary dict with ``matched``, ``unmatched``, ``games`` and
    ``standings``.
    """
    target = target_date or today_et()
    logger.info("[daily_fetch] Starting for ET date: %s", target.isoformat())
    
    nba = nba_client or NbaClient()
    gamma = gamma_client or GammaSportsClient()
    dynamo = store or DynamoStore()

    # Step A: Fetch NBA schedule
    try:
        logger.info("[daily_fetch] Step A: Fetching NBA schedule from API Ninjas...")
        games = nba.fetch_games(settings.sports_league, target_date=target)
        logger.info("[daily_fetch] Step A: ✓ Got %d games from NBA schedule", len(games))
    except Exception as e:
        logger.error("[daily_fetch] Step A FAILED: NBA schedule fetch: %s", e, exc_info=True)
        raise

    # Step B: Fetch standings
    try:
        logger.info("[daily_fetch] Step B: Fetching standings...")
        standings = nba.fetch_standings(settings.sports_league)
        logger.info("[daily_fetch] Step B: ✓ Got standings for %d teams", len(standings))
    except Exception as e:
        logger.error("[daily_fetch] Step B FAILED: Standings fetch: %s", e, exc_info=True)
        raise

    # Step C: Fetch Polymarket events
    try:
        logger.info("[daily_fetch] Step C: Fetching Polymarket NBA events (series %s)...", 
                   settings.polymarket_series_id)
        events = gamma.list_nba_events(target_date=target)
        logger.info("[daily_fetch] Step C: ✓ Got %d Polymarket events", len(events))
    except Exception as e:
        logger.error("[daily_fetch] Step C FAILED: Polymarket events fetch: %s", e, exc_info=True)
        raise

    # Step D: Match schedule with events
    try:
        logger.info("[daily_fetch] Step D: Matching NBA schedule with Polymarket events...")
        matched_pairs, unmatched = match_schedule(games, events)
        logger.info("[daily_fetch] Step D: ✓ Matched %d pairs, %d unmatched games", 
                   len(matched_pairs), len(unmatched))
    except Exception as e:
        logger.error("[daily_fetch] Step D FAILED: Schedule matching: %s", e, exc_info=True)
        raise

    # Step E: Build and persist game metadata
    matched: list[dict[str, Any]] = []
    game_metas: list[dict[str, Any]] = []
    try:
        logger.info("[daily_fetch] Step E: Building and persisting game metadata...")
        for idx, pair in enumerate(matched_pairs, 1):
            game = pair["game"]
            event = pair["event"]
            meta = build_game_meta(game, event, standings, target_date=target)
            if not meta.get("slug"):
                logger.warning("[daily_fetch]   Matched pair #%d: no slug generated; skipping", idx)
                continue
            
            favourite = pick_favourite(meta)
            if favourite:
                meta["favourite_team"] = favourite.get("name")
                meta["favourite_price"] = favourite.get("price")
            
            dynamo.put_game(str(meta["slug"]), meta)
            game_metas.append(meta)
            matched.append(
                {
                    "slug": meta.get("slug"),
                    "matchup": f"{meta.get('away_team')} @ {meta.get('home_team')}",
                    "start_time": meta.get("start_time"),
                    "favourite_team": meta.get("favourite_team"),
                    "favourite_price": meta.get("favourite_price"),
                    "away_team": meta.get("away_team"),
                    "home_team": meta.get("home_team"),
                }
            )
            logger.debug("[daily_fetch]   Game #%d: %s (%s)", idx, 
                        meta.get("slug"), meta.get("start_time"))
        
        logger.info("[daily_fetch] Step E: ✓ Persisted %d game metadata items", len(game_metas))
    except Exception as e:
        logger.error("[daily_fetch] Step E FAILED: Metadata persistence: %s", e, exc_info=True)
        raise

    # Step F: Write daily batch summary
    try:
        logger.info("[daily_fetch] Step F: Writing daily batch summary to DynamoDB...")
        batch = {
            "date": target.isoformat(),
            "fetched_at": int(datetime.now(timezone.utc).timestamp()),
            "games": game_metas,
            "standings": standings,
            "game_count": len(game_metas),
        }
        dynamo.put_daily_batch(target.isoformat(), batch)
        logger.info("[daily_fetch] Step F: ✓ Daily batch written")
    except Exception as e:
        logger.error("[daily_fetch] Step F FAILED: Daily batch write: %s", e, exc_info=True)
        raise

    # Step G: Compute listener window
    try:
        logger.info("[daily_fetch] Step G: Computing listener window...")
        window = compute_window(
            game_metas,
            now_ts=now_ts,
            lead_minutes=settings.listener_lead_minutes,
            trail_minutes=settings.listener_trail_minutes,
        )
        if window:
            dynamo.put_schedule_window(target.isoformat(), window)
            logger.info("[daily_fetch] Step G: ✓ Listener window computed and stored: %s", 
                       describe_window(window))
            
            # Step H: Schedule window one-shots
            try:
                logger.info("[daily_fetch] Step H: Scheduling listener window scales...")
                _schedule_window(window, target.isoformat())
                logger.info("[daily_fetch] Step H: ✓ Window scales scheduled (or fallback noted)")
            except Exception as e:
                logger.error("[daily_fetch] Step H WARNING: Could not schedule window scales: %s", e)
        else:
            logger.warning("[daily_fetch] Step G: No listener window (no games?) - Fargate will not auto-scale")
    except Exception as e:
        logger.error("[daily_fetch] Step G FAILED: Window computation: %s", e, exc_info=True)
        raise

    # Step I: Notifications
    if notify:
        try:
            logger.info("[daily_fetch] Step I: Sending Telegram notification...")
            result = {
                "date": target.isoformat(),
                "matched": matched,
                "unmatched": [unmatched_label(g) for g in unmatched],
                "games": game_metas,
                "standings": standings,
                "event_count": len(events),
                "schedule_count": len(games),
                "window": window,
            }
            from src.notify.telegram import notify_fetch
            notify_fetch(result)
            logger.info("[daily_fetch] Step I: ✓ Telegram notification sent")
        except Exception:
            logger.exception("[daily_fetch] Step I WARNING: Telegram notification failed")

    result = {
        "date": target.isoformat(),
        "matched": matched,
        "unmatched": [unmatched_label(g) for g in unmatched],
        "games": game_metas,
        "standings": standings,
        "event_count": len(events),
        "schedule_count": len(games),
        "window": window,
    }

    logger.info("[daily_fetch] ✓ COMPLETE: %d matched, %d unmatched (of %d scheduled)", 
                len(matched), len(unmatched), len(games))
    return result
