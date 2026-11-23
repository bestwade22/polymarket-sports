"""Tests for loading credentials from an AWS Secrets Manager secret.

The bot can either own its secret (``polymarket-sports/bot``, populated by
CloudFormation) or share an existing one - in practice the
``polymarket-trader`` secret, so the wallet private key never has to be
pasted into GitHub. Both paths go through :func:`load_secret_env`, and the
key names it reads must stay in sync with what that shared secret holds.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

SECRET_ENV_KEYS = (
    "PRIVATE_KEY",
    "DEPOSIT_WALLET_ADDRESS",
    "SIGNATURE_TYPE",
    "API_NINJAS_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DRY_RUN",
)

# The polymarket-trader secret, as it exists in ap-east-1. It carries two
# extra keys that this bot does not use; they must be ignored, not fatal.
SHARED_SECRET = {
    "API_NINJAS_KEY": "ninjas-key",
    "PRIVATE_KEY": "0xabc",
    "DEPOSIT_WALLET_ADDRESS": "0xdef",
    "GITHUB_PAT": "github_pat_unused",
    "DRY_RUN": "true",
    "TELEGRAM_BOT_TOKEN": "123:ABC",
    "TELEGRAM_CHAT_ID": "456",
    "WEATHER_COM_API_KEY": "weather-key",
}


@pytest.fixture
def fresh_settings():
    """Re-import config.settings with a clean environment.

    ``config.settings`` calls ``load_dotenv`` at import time, which would
    otherwise pull the developer's local ``.env`` into the environment and
    mask what the secret actually loaded.
    """
    for key in (*SECRET_ENV_KEYS, "SECRETS_ARN"):
        os.environ.pop(key, None)
    sys.modules.pop("config.settings", None)
    with patch("dotenv.load_dotenv", return_value=False):
        module = importlib.import_module("config.settings")
    yield module
    sys.modules.pop("config.settings", None)


def _fake_boto3(payload: dict) -> ModuleType:
    boto3 = MagicMock()
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(payload)}
    boto3.client.return_value = client
    return boto3


def test_loads_credentials_from_the_shared_secret(fresh_settings):
    os.environ["SECRETS_ARN"] = "arn:aws:secretsmanager:ap-east-1:1:secret:shared"

    with patch.dict(sys.modules, {"boto3": _fake_boto3(SHARED_SECRET)}):
        fresh_settings.load_secret_env()

    assert os.environ["PRIVATE_KEY"] == "0xabc"
    assert os.environ["DEPOSIT_WALLET_ADDRESS"] == "0xdef"
    assert os.environ["API_NINJAS_KEY"] == "ninjas-key"
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "123:ABC"
    assert os.environ["TELEGRAM_CHAT_ID"] == "456"
    assert os.environ["DRY_RUN"] == "true"


def test_unrelated_secret_keys_are_ignored(fresh_settings):
    """The shared secret carries GITHUB_PAT and a weather key."""
    os.environ["SECRETS_ARN"] = "arn:aws:secretsmanager:ap-east-1:1:secret:shared"

    with patch.dict(sys.modules, {"boto3": _fake_boto3(SHARED_SECRET)}):
        fresh_settings.load_secret_env()

    assert "GITHUB_PAT" not in os.environ
    assert "WEATHER_COM_API_KEY" not in os.environ


def test_signature_type_defaults_to_one_when_absent(fresh_settings):
    """The shared secret has no SIGNATURE_TYPE; the default must be 1."""
    payload = {k: v for k, v in SHARED_SECRET.items() if k != "SIGNATURE_TYPE"}
    os.environ["SECRETS_ARN"] = "arn:aws:secretsmanager:ap-east-1:1:secret:shared"

    with patch.dict(sys.modules, {"boto3": _fake_boto3(payload)}):
        fresh_settings.load_secret_env()

    assert "SIGNATURE_TYPE" not in os.environ
    assert fresh_settings.settings.signature_type == 1


def test_explicit_environment_wins_over_the_secret(fresh_settings):
    """A value set in the environment must not be overwritten by the secret."""
    os.environ["SECRETS_ARN"] = "arn:aws:secretsmanager:ap-east-1:1:secret:shared"
    os.environ["PRIVATE_KEY"] = "0xfrom-env"

    with patch.dict(sys.modules, {"boto3": _fake_boto3(SHARED_SECRET)}):
        fresh_settings.load_secret_env()

    assert os.environ["PRIVATE_KEY"] == "0xfrom-env"


def test_no_secret_arn_is_a_no_op(fresh_settings):
    os.environ.pop("SECRETS_ARN", None)

    with patch.dict(sys.modules, {"boto3": _fake_boto3(SHARED_SECRET)}):
        fresh_settings.load_secret_env()

    assert "PRIVATE_KEY" not in os.environ


def test_a_failing_secret_does_not_raise(fresh_settings):
    """A missing secret only disables live trading; it must not crash."""
    os.environ["SECRETS_ARN"] = "arn:aws:secretsmanager:ap-east-1:1:secret:missing"

    boto3 = MagicMock()
    client = MagicMock()
    client.get_secret_value.side_effect = RuntimeError("not found")
    boto3.client.return_value = client

    with patch.dict(sys.modules, {"boto3": boto3}):
        fresh_settings.load_secret_env()  # must not raise

    assert "PRIVATE_KEY" not in os.environ


def test_secret_env_keys_match_the_shared_secret_contract():
    """Every key the bot needs must be in SECRET_ENV_KEYS."""
    for key in (
        "PRIVATE_KEY",
        "DEPOSIT_WALLET_ADDRESS",
        "API_NINJAS_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        assert key in SECRET_ENV_KEYS
