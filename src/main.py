"""CLI entry point for the NBA Polymarket trading bot.

Every command is a thin adapter over a plain function in ``src/``, so the same
logic runs locally and in AWS.

    python -m src.main fetch-daily          # schedule + standings + Polymarket match -> DynamoDB
    python -m src.main snapshot-price       # pre-game prices for games inside T-30min
    python -m src.main listen               # the live WebSocket listener
    python -m src.main trade-worker --once  # consume one SQS message and act
    python -m src.main trade-worker --loop  # continuous SQS consumer
    python -m src.main run-scheduler        # all jobs on local timers
    python -m src.main scale-listener       # scale the Fargate listener to the day's window
    python -m src.main dashboard-export     # dump today's games/trades to a local JSON
    python -m src.main bootstrap-local      # create the local table + queue
"""

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import ensure_dirs, parse_event_date, settings, today_et

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """Configure root logging once, for CLI runs."""
    root = logging.getLogger()
    if root.handlers:
        return
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root.addHandler(handler)
    root.setLevel(level)


# --- commands ---------------------------------------------------------------


def cmd_bootstrap_local(_args: argparse.Namespace) -> None:
    """Create the DynamoDB table and SQS queue against the local emulator."""
    from src.api.dynamo_store import DynamoStore
    from src.api.queue_client import TradeQueue

    store = DynamoStore()
    store.ensure_table()
    logger.info("DynamoDB table ready: %s", store.table_name)

    queue = TradeQueue()
    queue.ensure_queue()
    logger.info("SQS queue ready: %s", queue.queue_name)


def cmd_fetch_daily(args: argparse.Namespace) -> None:
    from src.fetch.daily_fetch import run_daily_fetch

    target = parse_event_date(args.date)
    result = run_daily_fetch(target_date=target)
    logger.info(
        "Daily fetch complete for %s: %d matched, %d unmatched",
        target.isoformat(),
        len(result.get("matched") or []),
        len(result.get("unmatched") or []),
    )


def cmd_snapshot_price(args: argparse.Namespace) -> None:
    from src.fetch.pregame_snapshot import run_pregame_snapshot

    result = run_pregame_snapshot(target_date=parse_event_date(args.date))
    logger.info(
        "Snapshot complete: %d armed, %d skipped",
        len(result.get("armed") or []),
        len(result.get("skipped") or []),
    )


def cmd_listen(args: argparse.Namespace) -> None:
    import asyncio

    from src.trade.listener import run_listener

    asyncio.run(run_listener(target_date=parse_event_date(args.date), dry_run=args.dry_run))


def cmd_trade_worker(args: argparse.Namespace) -> None:
    from src.trade.buy_sell import run_trade_worker

    if args.loop:
        run_trade_worker(loop=True, dry_run=args.dry_run)
    else:
        result = run_trade_worker(loop=False, dry_run=args.dry_run)
        logger.info("Trade worker result: %s", result)


def cmd_scale_listener(args: argparse.Namespace) -> None:
    from src.trade.scaler import run_scale_job, window_status

    target = parse_event_date(args.date)
    if args.status:
        result = window_status(target_date=target)
        logger.info("Listener window: %s", result.get("summary"))
        return

    result = run_scale_job(target_date=target)
    logger.info(
        "Scale job: desired count %s (changed=%s)",
        result.get("desired_count"),
        result.get("changed"),
    )


def cmd_run_scheduler(args: argparse.Namespace) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from src.fetch.daily_fetch import run_daily_fetch
    from src.fetch.pregame_snapshot import run_pregame_snapshot
    from src.trade.scaler import run_scale_job

    scheduler = BlockingScheduler()
    scheduler.add_job(
        lambda: run_daily_fetch(target_date=today_et()),
        CronTrigger(
            hour=settings.daily_fetch_hour_utc,
            minute=settings.daily_fetch_minute_utc,
        ),
        id="daily_fetch",
    )
    scheduler.add_job(
        lambda: run_pregame_snapshot(target_date=today_et()),
        CronTrigger(minute=f"*/{settings.snapshot_interval_minutes}"),
        id="pregame_snapshot",
    )
    scheduler.add_job(
        lambda: run_scale_job(target_date=today_et()),
        CronTrigger(minute=f"*/{settings.snapshot_interval_minutes}"),
        id="scale_listener",
    )
    logger.info(
        "Scheduler started: daily fetch at %02d:%02d UTC, snapshot and scale every %d min",
        settings.daily_fetch_hour_utc,
        settings.daily_fetch_minute_utc,
        settings.snapshot_interval_minutes,
    )
    scheduler.start()


def cmd_dashboard_export(args: argparse.Namespace) -> None:
    from src.analysis.exporter import export_dashboard

    target = parse_event_date(args.date)
    path = export_dashboard(target)
    logger.info("Dashboard export written to %s", path)


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NBA Polymarket trading bot")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap-local", help="Create the local DynamoDB table and SQS queue")

    fetch_parser = sub.add_parser("fetch-daily", help="Fetch today's NBA schedule and match Polymarket")
    fetch_parser.add_argument("--date", metavar="YYYY-MM-DD", default=None)

    snap_parser = sub.add_parser("snapshot-price", help="Take pre-game prices for imminent games")
    snap_parser.add_argument("--date", metavar="YYYY-MM-DD", default=None)

    listen_parser = sub.add_parser("listen", help="Run the live WebSocket listener")
    listen_parser.add_argument("--date", metavar="YYYY-MM-DD", default=None)
    listen_parser.add_argument("--dry-run", action="store_true", default=None)

    worker_parser = sub.add_parser("trade-worker", help="Consume SQS trade messages")
    worker_parser.add_argument("--once", action="store_true", help="Process a single message")
    worker_parser.add_argument("--loop", action="store_true", help="Poll continuously")
    worker_parser.add_argument("--dry-run", action="store_true", default=None)

    sub.add_parser("run-scheduler", help="Run daily fetch + snapshot + scale on timers")

    scale_parser = sub.add_parser(
        "scale-listener", help="Scale the Fargate listener to match the day's window"
    )
    scale_parser.add_argument("--date", metavar="YYYY-MM-DD", default=None)
    scale_parser.add_argument(
        "--status", action="store_true", help="Print the window without scaling"
    )

    export_parser = sub.add_parser("dashboard-export", help="Dump today's games and trades to JSON")
    export_parser.add_argument("--date", metavar="YYYY-MM-DD", default=None)

    return parser


def main() -> None:
    ensure_dirs()
    args = build_parser().parse_args()
    setup_logging(args.verbose)

    commands = {
        "bootstrap-local": cmd_bootstrap_local,
        "fetch-daily": cmd_fetch_daily,
        "snapshot-price": cmd_snapshot_price,
        "listen": cmd_listen,
        "trade-worker": cmd_trade_worker,
        "run-scheduler": cmd_run_scheduler,
        "scale-listener": cmd_scale_listener,
        "dashboard-export": cmd_dashboard_export,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
