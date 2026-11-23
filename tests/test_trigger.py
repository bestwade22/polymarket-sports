"""Tests for the pure trigger rule."""

from src.trade import trigger as T


BASE = dict(
    status="armed",
    triggered_at=None,
    now_ts=1_000_000,
    start_ts=1_000_000 - 600,
    monitored_team="76ers",
    team="76ers",
    best_bid=0.34,
    best_ask=0.35,
    pre_game_mid=0.68,
    price_drop_ratio=0.5,
)


def test_fires_when_price_halves():
    decision = T.evaluate_trigger(**BASE)
    assert decision.fired
    assert decision.reason == T.TRIGGERED
    assert decision.threshold == 0.34
    assert decision.pre_game_mid == 0.68


def test_does_not_fire_above_threshold():
    decision = T.evaluate_trigger(**{**BASE, "best_bid": 0.35, "best_ask": 0.36})
    assert not decision.fired
    assert decision.reason == T.PRICE_NOT_DROPPED


def test_fires_exactly_at_threshold():
    decision = T.evaluate_trigger(**{**BASE, "best_bid": 0.34, "best_ask": 0.34})
    assert decision.fired


def test_not_armed():
    decision = T.evaluate_trigger(**{**BASE, "status": "scheduled"})
    assert not decision.fired
    assert decision.reason == T.NOT_ARMED


def test_already_triggered():
    decision = T.evaluate_trigger(**{**BASE, "triggered_at": 999})
    assert not decision.fired
    assert decision.reason == T.ALREADY_TRIGGERED


def test_before_tipoff():
    # start_ts is 10 minutes before BASE["now_ts"], so go an hour earlier.
    decision = T.evaluate_trigger(**{**BASE, "now_ts": BASE["start_ts"] - 3600})
    assert not decision.fired
    assert decision.reason == T.BEFORE_TIPOFF


def test_after_max_minutes():
    # 200 minutes after tip-off, past the 150-minute cap.
    decision = T.evaluate_trigger(**{**BASE, "now_ts": BASE["start_ts"] + 200 * 60})
    assert not decision.fired
    assert decision.reason == T.AFTER_CUTOFF


def test_period_cutoff_stops_monitoring():
    # Period 3 means we are past halftime (period 2 is the cutoff).
    decision = T.evaluate_trigger(**{**BASE, "period": 3, "stop_after_period": 2})
    assert not decision.fired
    assert decision.reason == T.AFTER_CUTOFF


def test_period_two_still_triggers():
    decision = T.evaluate_trigger(**{**BASE, "period": 2, "stop_after_period": 2})
    assert decision.fired


def test_not_monitored_team():
    decision = T.evaluate_trigger(**{**BASE, "team": "Nets"})
    assert not decision.fired
    assert decision.reason == T.NOT_MONITORED_TEAM


def test_no_pre_game_price():
    decision = T.evaluate_trigger(**{**BASE, "pre_game_mid": None})
    assert not decision.fired
    assert decision.reason == T.NO_PRE_GAME_PRICE


def test_no_price_at_all():
    decision = T.evaluate_trigger(**{**BASE, "best_bid": None, "best_ask": None})
    assert not decision.fired
    assert decision.reason == T.NO_TRIGGER_PRICE


def test_below_min_price():
    decision = T.evaluate_trigger(
        **{**BASE, "best_bid": 0.005, "best_ask": 0.006, "min_price": 0.01}
    )
    assert not decision.fired
    assert decision.reason == T.BELOW_MIN_PRICE


def test_above_max_price():
    decision = T.evaluate_trigger(
        **{**BASE, "pre_game_mid": 0.68, "best_bid": 0.95, "best_ask": 0.96, "max_price": 0.9}
    )
    assert not decision.fired
    assert decision.reason == T.ABOVE_MAX_PRICE


def test_opponent_rise_required_and_satisfied():
    decision = T.evaluate_trigger(
        **BASE,
        require_opponent_rise=True,
        opponent_best_bid=0.40,
        opponent_pre_game_mid=0.32,
    )
    assert decision.fired


def test_opponent_rise_required_and_not_satisfied():
    decision = T.evaluate_trigger(
        **BASE,
        require_opponent_rise=True,
        opponent_best_bid=0.30,
        opponent_pre_game_mid=0.32,
    )
    assert not decision.fired
    assert decision.reason == T.OPPONENT_NOT_RISEN


def test_opponent_rise_required_but_unknown():
    decision = T.evaluate_trigger(
        **BASE, require_opponent_rise=True, opponent_best_bid=None
    )
    assert not decision.fired
    assert decision.reason == T.OPPONENT_NOT_RISEN


def test_uses_ask_when_no_bid():
    decision = T.evaluate_trigger(**{**BASE, "best_bid": None, "best_ask": 0.30})
    assert decision.fired
    assert decision.trigger_price == 0.30


def test_decision_to_dict_is_serialisable():
    decision = T.evaluate_trigger(**BASE)
    data = decision.to_dict()
    assert data["fired"] is True
    assert data["reason"] == T.TRIGGERED
    assert data["details"]["drop_ratio"] == 0.5
