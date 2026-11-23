"""Tests for the API Ninjas / ESPN client.

The ESPN scoreboard is the fallback for the live score, and its payload
shape is not stable: ``status.period`` is a nested dict in some responses
and a plain integer in others, and the competitors live on the competition
in the older payloads and on the event in the newer ones.
"""

from typing import Any

from src.api.nba_client import NbaClient


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, payload: Any):
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, params: Any = None, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, params or {}))
        return FakeResponse(self.payload)


def _espn_payload(period: Any, competitors_on: str = "competition") -> dict[str, Any]:
    competitors = [
        {"homeAway": "home", "score": "110"},
        {"homeAway": "away", "score": "105"},
    ]
    event: dict[str, Any] = {"id": "401909994"}
    competition: dict[str, Any] = {
        "id": "401909994",
        "status": {
            "period": period,
            "displayClock": "12:00",
            "type": {"description": "Final"},
        },
    }
    if competitors_on == "competition":
        competition["competitors"] = competitors
    else:
        event["competitors"] = competitors
    event["competitions"] = [competition]
    return {"events": [event]}


def test_espn_period_as_a_dict_is_read():
    client = NbaClient(api_key="test")
    client.session = FakeSession(_espn_payload({"number": 3}))

    scores = client.fetch_espn_scores("nba")

    assert scores["401909994"]["period"] == 3
    assert scores["401909994"]["home_score"] == "110"
    assert scores["401909994"]["away_score"] == "105"


def test_espn_period_as_an_integer_is_read():
    """ESPN sends ``status.period`` as a plain int in some responses."""
    client = NbaClient(api_key="test")
    client.session = FakeSession(_espn_payload(4))

    scores = client.fetch_espn_scores("nba")

    assert scores["401909994"]["period"] == 4


def test_espn_competitors_on_the_event_are_read():
    client = NbaClient(api_key="test")
    client.session = FakeSession(_espn_payload(2, competitors_on="event"))

    scores = client.fetch_espn_scores("nba")

    assert scores["401909994"]["home_score"] == "110"


def test_an_empty_espn_payload_yields_no_scores():
    client = NbaClient(api_key="test")
    client.session = FakeSession({"events": []})

    assert client.fetch_espn_scores("nba") == {}
