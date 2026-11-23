"""Shared Lambda helpers: logging and Secrets Manager loading.

The secret is actually loaded by ``config.settings`` at import time (so that
the settings class sees the values). This module keeps the explicit
``apply_secrets()`` call in the handlers for clarity and for the case where
the settings module was imported before the secret was available.
"""

import json
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import boto3

logger = logging.getLogger(__name__)

SECRET_ENV_KEYS = (
    "PRIVATE_KEY",
    "DEPOSIT_WALLET_ADDRESS",
    "SIGNATURE_TYPE",
    "API_NINJAS_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "DRY_RUN",
)


@lru_cache(maxsize=1)
def load_secrets() -> Dict[str, Any]:
    """Read the JSON secret into a dict."""
    arn = os.environ.get("SECRETS_ARN", "").strip()
    if not arn:
        logger.warning("SECRETS_ARN not set; using existing environment only")
        return {key: os.environ.get(key, "") for key in SECRET_ENV_KEYS}

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=arn)
    payload = response.get("SecretString") or ""
    if not payload:
        raise RuntimeError(f"Secret {arn} has no SecretString")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise RuntimeError(f"Secret {arn} must be a JSON object")
    return data


def apply_secrets(secrets: Optional[Dict[str, Any]] = None) -> None:
    """Copy secret values into the process environment.

    An environment variable that was already set explicitly wins over the
    secret, so a template-level default (for example ``DRY_RUN``) is never
    silently overridden.
    """
    data = secrets if secrets is not None else load_secrets()
    for key in SECRET_ENV_KEYS:
        value = data.get(key)
        if value is None or not str(value).strip():
            continue
        os.environ.setdefault(key, str(value))


def configure_logging() -> None:
    """Ensure INFO logs reach stdout (Lambda pre-configures the root logger)."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setLevel(logging.INFO)
            handler.setFormatter(formatter)
