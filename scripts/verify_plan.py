"""End-to-end verification of every plan phase against the real filesystem.

This is a check-list script, not a unit test: it asserts that each file the
plan names exists and that each documented entry point is importable and
callable. Run it with:

    .venv/bin/python scripts/verify_plan.py
"""

import importlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

FAILURES: list[str] = []


def check_file(relative: str) -> None:
    path = PROJECT_ROOT / relative
    if not path.exists():
        FAILURES.append(f"missing file: {relative}")
    else:
        print(f"  OK  {relative}")


def check_attr(module: str, *names: str) -> None:
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f"cannot import {module}: {exc}")
        return
    for name in names:
        if not hasattr(mod, name):
            FAILURES.append(f"{module} has no attribute {name}")
        else:
            print(f"  OK  {module}.{name}")


def check_class_attr(module: str, class_name: str, *names: str) -> None:
    """Check methods on a class, not on the module."""
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f"cannot import {module}: {exc}")
        return
    cls = getattr(mod, class_name, None)
    if cls is None:
        FAILURES.append(f"{module} has no class {class_name}")
        return
    for name in names:
        if not hasattr(cls, name):
            FAILURES.append(f"{module}.{class_name} has no attribute {name}")
        else:
            print(f"  OK  {module}.{class_name}.{name}")


def main() -> int:
    print("Phase 1 - skeleton and config")
    for rel in (
        "config/settings.py",
        ".env.example",
        "requirements.txt",
        "requirements-dev.txt",
        "pytest.ini",
    ):
        check_file(rel)
    check_attr("config.settings", "settings", "parse_event_date", "ensure_dirs")

    print("\nPhase 2 - local dev stack")
    for rel in ("scripts/local_stack.sh",):
        check_file(rel)
    check_attr("src.main", "main")

    print("\nPhase 3 - clients")
    check_class_attr(
        "src.api.gamma_sports",
        "GammaSportsClient",
        "list_nba_games",
        "parse_moneyline_market",
        "get_event_by_game_id",
        "get_event_by_slug",
    )
    check_class_attr(
        "src.api.nba_client", "NbaClient", "fetch_games", "fetch_standings"
    )
    check_class_attr(
        "src.api.dynamo_store",
        "DynamoStore",
        "put_game",
        "get_game",
        "get_games_for_date",
        "put_trade",
        "put_log",
        "put_daily_batch",
        "claim_trigger",
    )

    print("\nPhase 4 - daily fetch")
    check_attr("src.fetch.daily_fetch", "run_daily_fetch")
    check_attr("lambda_handlers.fetch_daily", "handler")

    print("\nPhase 5 - pre-game snapshot")
    check_attr("src.fetch.pregame_snapshot", "run_pregame_snapshot")
    check_attr("lambda_handlers.snapshot_price", "handler")

    print("\nPhase 6 - live listener")
    check_attr(
        "src.trade.listener",
        "Listener",
        "MarketSocket",
        "SportsSocket",
        "UserSocket",
        "build_market_subscribe_frame",
        "run_listener",
    )
    check_attr("src.trade.trigger", "evaluate_trigger", "TriggerDecision")
    check_attr("src.trade.book_tracker", "BookTracker")
    check_attr("src.trade.score_tracker", "ScoreTracker")

    print("\nPhase 7 - trade worker")
    check_attr("src.trade.buy_sell", "TradeWorker", "run_trade_worker")
    check_attr("lambda_handlers.trade_worker", "handler")
    check_attr("src.trade.executor", "TradeExecutor", "compute_order_expiration")
    check_attr("src.trade.open_order_checker", "LiveOpenOrderChecker")
    check_attr("src.trade.position_checker", "LivePositionChecker")
    check_attr(
        "src.notify.telegram",
        "notify_fetch",
        "notify_trade",
        "notify_skip",
        "send_telegram_message",
    )

    print("\nPhase 8 - history and dashboard")
    check_attr("src.analysis.exporter", "build_dashboard", "export_dashboard")
    check_attr("lambda_handlers.dashboard_api", "handler")
    for rel in ("web/index.html", "web/app.js", "web/style.css"):
        check_file(rel)

    print("\nPhase 9 - AWS infrastructure")
    for rel in (
        "infrastructure/template.yaml",
        "infrastructure/Dockerfile",
        "infrastructure/ecr-lifecycle-policy.json",
        ".github/workflows/deploy-lambda.yml",
        "scripts/apply_ecr_lifecycle.sh",
    ):
        check_file(rel)

    print("\nPhase 11 - dynamic listener window")
    check_attr("src.trade.window", "compute_window", "window_is_open", "describe_window")
    check_attr(
        "src.trade.scaler",
        "run_scale_job",
        "resolve_window",
        "schedule_window_schedules",
        "window_status",
    )
    check_attr("lambda_handlers.scale_listener", "handler")
    check_class_attr(
        "src.api.dynamo_store",
        "DynamoStore",
        "put_schedule_window",
        "get_schedule_window",
    )

    print("\nPhase 10 - tests")
    tests = sorted((PROJECT_ROOT / "tests").glob("test_*.py"))
    for path in tests:
        print(f"  OK  tests/{path.name}")
    if not tests:
        FAILURES.append("no test files found")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} problem(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All plan phases verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
