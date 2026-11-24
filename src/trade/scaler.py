"""Scale the Fargate listener up and down around the day's games.

The listener is a single ECS task that has to be running while there is a
game to trade. Instead of a fixed cron, the window is *derived* from the
day's tip-off times (see :mod:`src.trade.window`) and stored in DynamoDB by
the daily fetch.

This module drives the ECS service from that window:

* :func:`run_scale_job` is the Lambda entry point. It reads the window and
  sets the service's desired count to 1 when the window is open and 0 when it
  is not.
* :func:`schedule_window_schedules` creates one-shot EventBridge Scheduler
  schedules for the window's start and end, so the service scales up exactly
  when the window opens even if the scale job has not run since.

The scale job is safe to run on a frequent schedule: the desired count is
idempotent, so a repeat invocation inside the window is a no-op.
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from config.settings import settings, today_et
from src.api.dynamo_store import DynamoStore
from src.trade.window import (
    compute_window,
    describe_window,
    parse_schedule_end_expression,
    parse_schedule_expression,
    window_is_open,
)

logger = logging.getLogger(__name__)

# The prefix for the one-shot EventBridge schedules this module creates.
def _schedule_prefix() -> str:
    return f"{settings.stack_name}-listener"


def _update_service_target_arn() -> str:
    """The EventBridge Scheduler universal-target ARN for ECS UpdateService.

    EventBridge Scheduler reaches a service API through a *universal* target:
    the target ARN names the API action to call, not the resource to call it
    on. Targeting an ECS service by its own ARN is rejected by the Scheduler
    with "Provided Arn is not in correct format".

    The action name is camelCase - ``updateService``, not ``UpdateService``.
    """
    return "arn:aws:scheduler:::aws-sdk:ecs:updateService"


def get_service_name() -> str:
    """The ECS service name, from the environment or the stack default."""
    return (
        settings.ecs_service_name
        or f"{settings.stack_name}-listener"
    )


def get_cluster_name() -> str:
    """The ECS cluster name, from the environment or the stack default."""
    return settings.ecs_cluster_name or f"{settings.stack_name}-cluster"


def _ecs_client():
    import boto3

    return boto3.client("ecs", region_name=settings.aws_region)


def _scheduler_client():
    import boto3

    return boto3.client("scheduler", region_name=settings.aws_region)


def set_desired_count(desired: int, *, client=None) -> bool:
    """Set the listener service's desired count.

    Returns True when the call succeeded. A missing service (the stack is
    not deployed) is logged rather than raised, so the job can be run
    locally without an ECS deployment.
    """
    ecs = client or _ecs_client()
    cluster = get_cluster_name()
    service = get_service_name()
    try:
        ecs.update_service(
            cluster=cluster,
            service=service,
            desiredCount=int(desired),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not set desired count %d for %s/%s: %s",
            desired,
            cluster,
            service,
            exc,
        )
        return False
    logger.info("Listener %s/%s desired count set to %d", cluster, service, desired)
    return True


def current_desired_count(*, client=None) -> Optional[int]:
    """The service's current desired count, or None when it cannot be read."""
    ecs = client or _ecs_client()
    try:
        resp = ecs.describe_services(
            cluster=get_cluster_name(),
            services=[get_service_name()],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not describe the listener service: %s", exc)
        return None
    services = resp.get("services") or []
    if not services:
        logger.warning("The listener service does not exist")
        return None
    return int(services[0].get("desiredCount") or 0)


def resolve_window(
    *,
    target_date: Optional[date] = None,
    store: Optional[DynamoStore] = None,
    now_ts: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """The listener window for a date.

    Prefers the window the daily fetch wrote. Falls back to recomputing it
    from the day's stored games, so a window is available even if the daily
    fetch ran before the window logic existed.
    """
    dynamo = store or DynamoStore()
    day = target_date.isoformat() if target_date is not None else today_et().isoformat()

    stored = dynamo.get_schedule_window(day)
    if stored and stored.get("start_ts") and stored.get("end_ts"):
        return stored

    games = [
        item
        for item in dynamo.get_games_for_date(day)
        if item.get("sk") == "META" and item.get("start_ts")
    ]
    if not games:
        logger.info("No games stored for %s; cannot derive a listener window", day)
        return None

    window = compute_window(
        games,
        now_ts=now_ts,
        lead_minutes=settings.listener_lead_minutes,
        trail_minutes=settings.listener_trail_minutes,
    )
    if window:
        dynamo.put_schedule_window(day, window)
    return window


def run_scale_job(
    *,
    target_date: Optional[date] = None,
    store: Optional[DynamoStore] = None,
    client=None,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Scale the listener to match the day's window.

    Returns a summary dict describing what was done.
    """
    dynamo = store or DynamoStore()
    window = resolve_window(target_date=target_date, store=dynamo, now_ts=now_ts)

    if not window:
        # No window means no games to trade - make sure the listener is down
        # so a stale task does not burn money all night.
        changed = set_desired_count(0, client=client)
        logger.info("No listener window; scaled the listener down")
        return {
            "status": "no_window",
            "desired_count": 0,
            "changed": changed,
            "window": None,
        }

    open_now = window_is_open(window, now_ts=now_ts)
    desired = 1 if open_now else 0

    existing = current_desired_count(client=client)
    changed = desired != existing
    if changed:
        set_desired_count(desired, client=client)

    logger.info(
        "Listener window %s: %s (current=%s desired=%d)",
        describe_window(window),
        "open" if open_now else "closed",
        existing,
        desired,
    )

    return {
        "status": "ok",
        "desired_count": desired,
        "changed": changed,
        "window": window,
        "window_open": open_now,
    }


def delete_window_schedules(*, client=None) -> int:
    """Delete the one-shot schedules this module created. Returns the count."""
    scheduler = client or _scheduler_client()
    deleted = 0
    for suffix in ("start", "end"):
        name = f"{_schedule_prefix()}-{suffix}"
        try:
            scheduler.delete_schedule(Name=name)
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("No schedule %s to delete: %s", name, exc)
    return deleted


def schedule_window_schedules(
    window: Optional[dict[str, Any]],
    *,
    role_arn: str,
    client=None,
) -> list[str]:
    """Create one-shot schedules for the window's start and end.

    These fire a Lambda that sets the desired count, so the listener starts
    exactly when the window opens even if the scale job has not run for
    hours. Both schedules are replaced (``UpdateSchedule`` semantics via
    delete-then-create) so a rescheduled game moves them.
    """
    if not window:
        return []

    scheduler = client or _scheduler_client()
    cluster = get_cluster_name()
    service = get_service_name()

    delete_window_schedules(client=scheduler)

    created: list[str] = []
    for suffix, expression, desired in (
        ("start", parse_schedule_expression(window), 1),
        ("end", parse_schedule_end_expression(window), 0),
    ):
        if not expression:
            continue
        name = f"{_schedule_prefix()}-{suffix}"
        try:
            scheduler.create_schedule(
                Name=name,
                ScheduleExpression=f"at({expression})",
                FlexibleTimeWindow={"Mode": "OFF"},
                Target={
                    "Arn": _update_service_target_arn(),
                    "RoleArn": role_arn,
                    "Input": json.dumps(
                        {
                            "Cluster": cluster,
                            "Service": service,
                            "DesiredCount": desired,
                        },
                        separators=(",", ":"),
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not create the %s schedule: %s", name, exc)
            continue
        created.append(name)
        logger.info("Created schedule %s for %s (desired count %d)", name, expression, desired)

    return created


def run_schedule_creation(
    *,
    target_date: Optional[date] = None,
    store: Optional[DynamoStore] = None,
    role_arn: str = "",
    client=None,
) -> dict[str, Any]:
    """Compute the window and (re)create the one-shot schedules for it."""
    dynamo = store or DynamoStore()
    window = resolve_window(target_date=target_date, store=dynamo)
    if not window:
        delete_window_schedules(client=client)
        return {"status": "no_window", "schedules": []}

    created = (
        schedule_window_schedules(window, role_arn=role_arn, client=client)
        if role_arn
        else []
    )
    return {
        "status": "ok",
        "window": window,
        "schedules": created,
        "summary": describe_window(window),
    }


def window_status(
    *,
    target_date: Optional[date] = None,
    store: Optional[DynamoStore] = None,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """A read-only view of the window, for logging and the dashboard."""
    dynamo = store or DynamoStore()
    window = resolve_window(target_date=target_date, store=dynamo, now_ts=now_ts)
    if not window:
        return {"status": "no_window", "window": None}

    now = int(now_ts) if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    return {
        "status": "ok",
        "window": window,
        "open": window_is_open(window, now_ts=now),
        "summary": describe_window(window),
        "now_ts": now,
    }
