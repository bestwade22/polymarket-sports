"""Pure trigger-rule evaluation.

No I/O, no clock reads, no network - every input is passed in. This is the
rule that decides whether a live price tick should fire a trade, so it is
kept side-effect free and unit tested.

The rule:

1. the game must be armed (a pre-game snapshot exists);
2. it must not have fired already (``triggered_at`` unset);
3. the monitored team must be the one whose price dropped;
4. the live bid must have fallen to ``PRICE_DROP_RATIO`` of the pre-game mid
   (mid is used as the fallback trigger price when no bid is available);
5. the monitored team's price must be inside the absolute price bounds;
6. optionally, the opponent's price must have risen
   (``REQUIRE_OPPONENT_RISE``).

The result is a :class:`TriggerDecision` explaining *why* it did or did not
fire, so the listener can log it without re-deriving the reasoning.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

# Reason codes. Stable strings, used in logs and DynamoDB records.
NOT_ARMED = "not_armed"
ALREADY_TRIGGERED = "already_triggered"
BEFORE_TIPOFF = "before_tipoff"
AFTER_CUTOFF = "after_cutoff"
NOT_MONITORED_TEAM = "not_monitored_team"
NO_TRIGGER_PRICE = "no_trigger_price"
NO_PRE_GAME_PRICE = "no_pre_game_price"
PRICE_NOT_DROPPED = "price_not_dropped"
BELOW_MIN_PRICE = "below_min_price"
ABOVE_MAX_PRICE = "above_max_price"
OPPONENT_NOT_RISEN = "opponent_not_risen"
TRIGGERED = "triggered"


@dataclass
class TriggerDecision:
    """The outcome of evaluating the trigger rule for one price tick."""

    fired: bool
    reason: str
    trigger_price: Optional[float] = None
    pre_game_mid: Optional[float] = None
    threshold: Optional[float] = None
    monitored_team: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fired": self.fired,
            "reason": self.reason,
            "trigger_price": self.trigger_price,
            "pre_game_mid": self.pre_game_mid,
            "threshold": self.threshold,
            "monitored_team": self.monitored_team,
            "details": self.details,
        }


def _price_for_trigger(best_bid: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    """The price the trigger keys off: the best bid, falling back to the mid.

    The strategy's own definition keys on the *bid* - that is the price at
    which the position could be exited immediately. When the book has no bid
    (one-sided), the mid of bid/ask is used instead.
    """
    if best_bid is not None:
        return best_bid
    if best_ask is not None:
        return best_ask
    return None


def evaluate_trigger(
    *,
    status: Optional[str],
    triggered_at: Optional[int],
    now_ts: int,
    start_ts: Optional[int],
    monitored_team: Optional[str],
    team: str,
    best_bid: Optional[float],
    best_ask: Optional[float],
    pre_game_mid: Optional[float],
    price_drop_ratio: float,
    stop_after_period: int = 2,
    max_minutes: int = 150,
    require_opponent_rise: bool = False,
    opponent_best_bid: Optional[float] = None,
    opponent_pre_game_mid: Optional[float] = None,
    min_price: float = 0.01,
    max_price: float = 0.99,
    elapsed_minutes: Optional[float] = None,
    period: Optional[int] = None,
) -> TriggerDecision:
    """Evaluate the trigger rule for one price tick.

    ``elapsed_minutes`` and ``period`` are optional live-game signals from
    the sports feed. They are only used to enforce the halftime cutoff; the
    rule never *depends* on them, because the sports socket is unreliable.
    """
    if status != "armed":
        return TriggerDecision(False, NOT_ARMED, monitored_team=monitored_team)

    if triggered_at:
        return TriggerDecision(False, ALREADY_TRIGGERED, monitored_team=monitored_team)

    if start_ts is None:
        return TriggerDecision(False, BEFORE_TIPOFF, monitored_team=monitored_team)

    # DynamoDB returns numbers as Decimal; normalise before doing arithmetic.
    try:
        start_ts = float(start_ts)
    except (TypeError, ValueError):
        return TriggerDecision(False, BEFORE_TIPOFF, monitored_team=monitored_team)

    minutes_since_tipoff = (now_ts - start_ts) / 60.0
    if minutes_since_tipoff < 0:
        return TriggerDecision(
            False,
            BEFORE_TIPOFF,
            monitored_team=monitored_team,
            details={"minutes_since_tipoff": round(minutes_since_tipoff, 2)},
        )

    if minutes_since_tipoff > max_minutes:
        return TriggerDecision(
            False,
            AFTER_CUTOFF,
            monitored_team=monitored_team,
            details={"minutes_since_tipoff": round(minutes_since_tipoff, 2)},
        )

    if elapsed_minutes is None and period is not None and stop_after_period > 0:
        # Period 1 = first half, 2 = halftime. Stop once the period has
        # reached the configured cutoff.
        if period >= stop_after_period + 1:
            return TriggerDecision(
                False,
                AFTER_CUTOFF,
                monitored_team=monitored_team,
                details={"period": period},
            )

    if not monitored_team:
        return TriggerDecision(False, NOT_MONITORED_TEAM)

    if team != monitored_team:
        return TriggerDecision(
            False, NOT_MONITORED_TEAM, monitored_team=monitored_team, details={"team": team}
        )

    if pre_game_mid is None:
        return TriggerDecision(False, NO_PRE_GAME_PRICE, monitored_team=monitored_team)

    # DynamoDB returns numbers as Decimal; normalise before doing arithmetic.
    try:
        pre_game_mid = float(pre_game_mid)
    except (TypeError, ValueError):
        return TriggerDecision(False, NO_PRE_GAME_PRICE, monitored_team=monitored_team)

    trigger_price = _price_for_trigger(best_bid, best_ask)
    if trigger_price is None:
        return TriggerDecision(False, NO_TRIGGER_PRICE, monitored_team=monitored_team)
    trigger_price = float(trigger_price)

    threshold = pre_game_mid * price_drop_ratio

    # The absolute bounds are checked before the drop condition: they are a
    # guard against a degenerate pre-game price, not a consequence of the drop.
    if trigger_price < min_price:
        return TriggerDecision(
            False,
            BELOW_MIN_PRICE,
            trigger_price=trigger_price,
            pre_game_mid=pre_game_mid,
            threshold=threshold,
            monitored_team=monitored_team,
        )

    if trigger_price > max_price:
        return TriggerDecision(
            False,
            ABOVE_MAX_PRICE,
            trigger_price=trigger_price,
            pre_game_mid=pre_game_mid,
            threshold=threshold,
            monitored_team=monitored_team,
        )

    if trigger_price > threshold:
        return TriggerDecision(
            False,
            PRICE_NOT_DROPPED,
            trigger_price=trigger_price,
            pre_game_mid=pre_game_mid,
            threshold=threshold,
            monitored_team=monitored_team,
            details={"drop_ratio": round(1.0 - (trigger_price / pre_game_mid), 4)},
        )

    if require_opponent_rise:
        if opponent_best_bid is None or opponent_pre_game_mid is None:
            return TriggerDecision(
                False,
                OPPONENT_NOT_RISEN,
                trigger_price=trigger_price,
                pre_game_mid=pre_game_mid,
                threshold=threshold,
                monitored_team=monitored_team,
                details={"reason_detail": "opponent price unknown"},
            )
        if opponent_best_bid <= opponent_pre_game_mid:
            return TriggerDecision(
                False,
                OPPONENT_NOT_RISEN,
                trigger_price=trigger_price,
                pre_game_mid=pre_game_mid,
                threshold=threshold,
                monitored_team=monitored_team,
                details={
                    "opponent_bid": opponent_best_bid,
                    "opponent_pre_game_mid": opponent_pre_game_mid,
                },
            )

    return TriggerDecision(
        True,
        TRIGGERED,
        trigger_price=trigger_price,
        pre_game_mid=pre_game_mid,
        threshold=threshold,
        monitored_team=monitored_team,
        details={
            "best_bid": best_bid,
            "best_ask": best_ask,
            "minutes_since_tipoff": round(minutes_since_tipoff, 2),
            "drop_ratio": round(1.0 - (trigger_price / pre_game_mid), 4)
            if pre_game_mid
            else None,
        },
    )
