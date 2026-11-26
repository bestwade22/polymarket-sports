"""Environment-driven settings for the NBA Polymarket trading bot.

Mirrors the style of ``polymarket-trader/config/settings.py``: a plain class
with class-level attributes read from the environment at import time, plus
module-level path constants.

Every knob that changes trading behaviour lives here so that a deployment is
fully described by environment variables + Secrets Manager.
"""

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
EXPORT_DIR = DATA_DIR / "exports"

logger = logging.getLogger(__name__)

load_dotenv(PROJECT_ROOT / ".env")


# --- Secret loading ---------------------------------------------------------

# The keys that live in Secrets Manager. They are copied into the process
# environment *before* the Settings class body is evaluated, because every
# setting is read from the environment at import time.
SECRET_ENV_KEYS = (
    "PRIVATE_KEY",
    "DEPOSIT_WALLET_ADDRESS",
    "SIGNATURE_TYPE",
    "API_NINJAS_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DRY_RUN",
)


def load_secret_env() -> None:
    """Copy the AWS secret into the environment, if there is one.

    Both the Lambda functions and the Fargate listener need the CLOB signing
    key, and neither of them has a ``.env`` file. Loading it here - at import
    time, before the settings are read - means a single code path serves
    local, Lambda and Fargate.

    Failures are non-fatal: a missing secret only disables live trading, and
    the caller still gets the environment variables that were set explicitly.
    """
    arn = os.getenv("SECRETS_ARN", "").strip()
    if not arn:
        return
    if os.getenv("PRIVATE_KEY", "").strip():
        # Already loaded (e.g. by the Lambda handler).
        return

    try:
        import boto3

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=arn)
        payload = response.get("SecretString") or ""
        if not payload:
            raise RuntimeError(f"Secret {arn} has no SecretString")
        import json

        data = json.loads(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load secret %s: %s", arn, exc)
        return

    if not isinstance(data, dict):
        logger.warning("Secret %s is not a JSON object; ignoring", arn)
        return

    for key in SECRET_ENV_KEYS:
        value = data.get(key)
        if value is None or not str(value).strip():
            continue
        # An explicitly-set environment variable wins over the secret.
        os.environ.setdefault(key, str(value))
    logger.info("Loaded secret %s", arn)


load_secret_env()


# --- Public endpoints -------------------------------------------------------

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"
CLOB_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
API_NINJAS_BASE = "https://api.api-ninjas.com/v1"
ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
)
CHAIN_ID = 137

# Polymarket NBA series id (verified via GET gamma-api.polymarket.com/sports).
POLYMARKET_NBA_SERIES_ID = "10345"

# The US Eastern timezone is what NBA tip-off times are published in and what
# the Polymarket event slug encodes.
US_EAST_TZ = "America/New_York"


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def today_et() -> date:
    """Today in US Eastern time.

    Every date in this bot is an *ET* date: the NBA schedule, the
    Polymarket event slugs and the DynamoDB ``day`` keys are all keyed on
    the ET calendar day. Using the machine's local date would be wrong on
    the Lambda hosts, which run in UTC.
    """
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(US_EAST_TZ)).date()


def parse_event_date(value: Optional[str] = None) -> date:
    """Parse YYYY-MM-DD from CLI/env, defaulting to today in ET."""
    raw = value or os.getenv("EVENT_DATE") or os.getenv("FETCH_DATE")
    if raw:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    return today_et()


def fetch_target_date() -> date:
    """The primary ET day the scheduled daily fetch should fetch.

    This is today's ET date. The scheduled fetch also covers the days
    *ahead* of it (see ``DAILY_FETCH_DAYS_AHEAD``), so that the next
    evening's slate is staged before it is needed.

    The date is resolved in **ET**, not in UTC, so the behaviour does not
    change with the season's daylight-saving switch.
    """
    return today_et()


def fetch_target_dates() -> list[date]:
    """Every ET day the scheduled daily fetch should fetch.

    Today's ET date first, then the days ahead of it. The list is ordered
    oldest-first so that the day's own listener window is written before
    tomorrow's.
    """
    base = fetch_target_date()
    ahead = max(0, int(settings.daily_fetch_days_ahead))
    return [base + timedelta(days=offset) for offset in range(ahead + 1)]


class Settings:
    # --- Credentials --------------------------------------------------------
    private_key: str = os.getenv("PRIVATE_KEY", "")
    deposit_wallet_address: str = os.getenv("DEPOSIT_WALLET_ADDRESS", "")
    signature_type: int = _env_int("SIGNATURE_TYPE", 1)
    api_ninjas_key: str = os.getenv("API_NINJAS_KEY", "")

    # --- Strategy knobs ----------------------------------------------------
    # Number of shares to buy on the trigger.
    share_count: int = _env_int("SHARE_COUNT", 5)
    # Trigger when the monitored team's live bid falls to <= ratio * pre-game mid.
    price_drop_ratio: float = _env_float("PRICE_DROP_RATIO", 0.5)
    # Take the pre-game snapshot this many minutes before tip-off.
    pre_game_snapshot_minutes: int = _env_int("PRE_GAME_SNAPSHOT_MINUTES", 30)
    # Stop monitoring after this NBA period (1=pre, 2=halftime, 3=after 3rd).
    monitor_stop_after_period: int = _env_int("MONITOR_STOP_AFTER_PERIOD", 2)
    # Hard cap on how long after tip-off we keep monitoring.
    monitor_max_minutes: int = _env_int("MONITOR_MAX_MINUTES", 150)
    # The buy order is GTD, expiring this many minutes after placement.
    buy_order_expiry_minutes: int = _env_int("BUY_ORDER_EXPIRY_MINUTES", 5)
    # 0 means GTC (no expiry) for the recovery sell.
    sell_order_expiry_hours: float = _env_float("SELL_ORDER_EXPIRY_HOURS", 0.0)
    # Optional confirmation filter: the opponent's price must also have risen.
    require_opponent_rise: bool = _env_bool("REQUIRE_OPPONENT_RISE", False)
    # Minimum absolute price for the monitored team at trigger time.
    trigger_min_price: float = _env_float("TRIGGER_MIN_PRICE", 0.01)
    # Maximum absolute price for the monitored team at trigger time.
    trigger_max_price: float = _env_float("TRIGGER_MAX_PRICE", 0.99)

    # --- Behaviour ---------------------------------------------------------
    dry_run: bool = _env_bool("DRY_RUN", True)
    sports_league: str = os.getenv("SPORTS_LEAGUE", "nba").strip().lower()
    polymarket_series_id: str = os.getenv(
        "POLYMARKET_SERIES_ID", POLYMARKET_NBA_SERIES_ID
    ).strip()
    # Minimum minutes before tip-off for the snapshot job to consider a game.
    snapshot_min_lead_minutes: int = _env_int("SNAPSHOT_MIN_LEAD_MINUTES", 0)
    # Days of retention for DynamoDB items (TTL).
    ttl_days: int = _env_int("TTL_DAYS", 3)

    # --- AWS / local endpoints --------------------------------------------
    aws_region: str = os.getenv("AWS_REGION", "ap-east-1")
    aws_account_id: str = os.getenv("AWS_ACCOUNT_ID", "").strip()
    dynamodb_table: str = os.getenv("DYNAMODB_TABLE", "SportsBotState")
    sqs_queue_url: str = os.getenv("SQS_QUEUE_URL", "")
    dynamodb_endpoint_url: str = os.getenv("DYNAMODB_ENDPOINT_URL", "")
    sqs_endpoint_url: str = os.getenv("SQS_ENDPOINT_URL", "")
    # The stack name the resources were deployed with. The ECS cluster and
    # service names are derived from it, so the scaler can find them without
    # a CloudFormation lookup.
    stack_name: str = os.getenv("STACK_NAME", "polymarket-sports")
    ecs_cluster_name: str = os.getenv("ECS_CLUSTER_NAME", "").strip()
    ecs_service_name: str = os.getenv("ECS_SERVICE_NAME", "").strip()
    # The role EventBridge Scheduler assumes to drive the ECS service.
    scheduler_role_arn: str = os.getenv("SCHEDULER_ROLE_ARN", "").strip()

    # --- WebSocket tuning --------------------------------------------------
    ws_ping_interval: int = _env_int("WS_PING_INTERVAL", 10)
    ws_reconnect_max_seconds: int = _env_int("WS_RECONNECT_MAX_SECONDS", 30)
    ws_idle_timeout_seconds: int = _env_int("WS_IDLE_TIMEOUT_SECONDS", 300)
    ws_user_enabled: bool = _env_bool("WS_USER_ENABLED", True)

    # --- Notifications -----------------------------------------------------
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_notify_enabled: bool = _env_bool("TELEGRAM_NOTIFY_ENABLED", True)

    # --- Scheduling --------------------------------------------------------
    # The daily fetch runs at DAILY_FETCH_HOUR_UTC:DAILY_FETCH_MINUTE_UTC in
    # **UTC** (AWS cron expressions are always UTC).
    #
    # The hour matters because every date in this bot is an *ET* date. A fetch
    # that runs before 05:00 UTC falls on the *previous* ET day, so the
    # schedule would pick up yesterday's slate instead of today's. The
    # default of 16:00 UTC is 11:00 ET - comfortably after ET midnight and
    # eight hours before the earliest tip-off (19:00 ET / 00:00 UTC).
    daily_fetch_hour_utc: int = _env_int("DAILY_FETCH_HOUR_UTC", 16)
    daily_fetch_minute_utc: int = _env_int("DAILY_FETCH_MINUTE_UTC", 0)
    # How many ET days ahead of today the scheduled fetch also covers.
    # 1 = fetch today and tomorrow.
    #
    # The bot trades the *upcoming* slate, and the schedule and the
    # Polymarket markets for the next evening are published well in advance.
    # Fetching a day ahead means the next slate is already staged (with its
    # listener window) before it is needed, so a late addition or a
    # reschedule is picked up with time to spare.
    daily_fetch_days_ahead: int = _env_int("DAILY_FETCH_DAYS_AHEAD", 1)
    snapshot_interval_minutes: int = _env_int("SNAPSHOT_INTERVAL_MINUTES", 5)
    # How long before the first tip-off the Fargate listener must be up. The
    # snapshot arms a game at T-PRE_GAME_SNAPSHOT_MINUTES and the listener's
    # watch loop polls every SNAPSHOT_INTERVAL_MINUTES, so the lead has to
    # cover both.
    listener_lead_minutes: int = _env_int("LISTENER_LEAD_MINUTES", 40)
    # How long after the last tip-off the listener stays up, to cover the
    # halftime cutoff and any in-flight buy's recovery sell.
    listener_trail_minutes: int = _env_int("LISTENER_TRAIL_MINUTES", 90)
    # How many heartbeat lines the listener keeps in its DynamoDB health
    # record. The dashboard shows the most recent ones.
    listener_health_log_lines: int = _env_int("LISTENER_HEALTH_LOG_LINES", 20)


settings = Settings()


def ensure_dirs() -> None:
    for path in (DATA_DIR, LOGS_DIR, EXPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)
