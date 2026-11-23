"""Telegram notifications for the NBA trading bot.

Adapted from ``polymarket-trader/src/notify/telegram.py``: same transport and
chunking, but the message formatting is for games and trades rather than
weather cities.
"""

from __future__ import annotations

import html
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

TELEGRAM_MAX_MESSAGE_LEN = 4096
TELEGRAM_PARSE_MODE = "HTML"

ET_TZ = ZoneInfo("America/New_York")
HKT_TZ = ZoneInfo("Asia/Hong_Kong")


def telegram_configured() -> bool:
    if not settings.telegram_notify_enabled:
        return False
    return bool(settings.telegram_bot_token.strip() and str(settings.telegram_chat_id).strip())


def chunk_message(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LEN) -> list[str]:
    """Split a long message into Telegram-sized chunks, line-aligned."""
    text = text.rstrip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    lines = text.split("\n")
    current: list[str] = []
    current_len = 0
    for line in lines:
        add_len = len(line) + (1 if current else 0)
        if current and current_len + add_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        elif not current and len(line) > max_len:
            for i in range(0, len(line), max_len):
                chunks.append(line[i : i + max_len])
            current = []
            current_len = 0
        else:
            current.append(line)
            current_len += add_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_telegram_message(text: str) -> bool:
    """POST a message to Telegram. Soft-fails; never raises into callers."""
    if not telegram_configured():
        return False
    token = settings.telegram_bot_token.strip()
    chat_id = str(settings.telegram_chat_id).strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": TELEGRAM_PARSE_MODE,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            if resp.status >= 400:
                logger.warning("Telegram send failed status=%s body=%s", resp.status, body[:200])
                return False
            return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


def _send_chunks(text: str) -> None:
    for part in chunk_message(text):
        send_telegram_message(part)


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _bold(value: Any) -> str:
    return f"<b>{_esc(value)}</b>"


def _code(value: Any) -> str:
    return f"<code>{_esc(value)}</code>"


def _fmt_price(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value) if value is not None else "?"


def _fmt_order_id(order_id: Any) -> str:
    if not order_id:
        return "-"
    s = str(order_id)
    return s[:10] + "..." if len(s) > 12 else s


def _fmt_et(iso_ts: Any) -> str:
    """Format an ISO timestamp as ET (and HKT) clock times."""
    if not iso_ts:
        return "?"
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
    except ValueError:
        return str(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(ET_TZ).strftime("%H:%M")
    hkt = dt.astimezone(HKT_TZ).strftime("%H:%M")
    return f"{et} ET / {hkt} HKT"


def _matchup(row: dict[str, Any]) -> str:
    away = row.get("away_team") or row.get("away") or "?"
    home = row.get("home_team") or row.get("home") or "?"
    return f"{away} @ {home}"


def format_fetch_message(result: dict[str, Any]) -> str:
    """Summary of the daily schedule fetch."""
    matched = list(result.get("matched") or [])
    unmatched = list(result.get("unmatched") or [])
    lines = [
        f"&#128269; {_bold('NBA SCHEDULE')}",
        f"Matched {_bold(len(matched))} | Unmatched {_bold(len(unmatched))}",
        "",
    ]
    for row in matched:
        tipoff = _fmt_et(row.get("start_time"))
        price = row.get("favourite_price")
        fav = row.get("favourite_team") or "?"
        extra = ""
        if price is not None:
            extra = f" - fav {_code(fav)} {_code(_fmt_price(price))}"
        lines.append(f"&#127919; {_bold(_matchup(row))} - {_code(tipoff)}{extra}")
    if unmatched:
        lines.append("")
        lines.append(f"_Unmatched: {', '.join(_esc(u) for u in unmatched)}_")
    return "\n".join(lines).rstrip() + "\n"


def format_trade_message(result: dict[str, Any]) -> str:
    """Summary of a trade attempt."""
    status = result.get("status") or "unknown"
    icon = {"filled": "✅", "simulated": "🧪", "skipped": "⏭"}.get(status, "📤")
    slug = result.get("slug") or "?"
    team = result.get("team") or "?"
    matchup = result.get("matchup") or slug
    lines = [
        f"{icon} {_bold('TRADE')} {_bold(matchup)}",
        f"team {_code(team)} - status {_code(status)}",
    ]
    if result.get("reason"):
        lines.append(f"reason {_code(result['reason'])}")
    if result.get("trigger_price") is not None:
        lines.append(f"trigger price {_code(_fmt_price(result['trigger_price']))}")
    if result.get("buy_price") is not None:
        lines.append(f"buy {_code(_fmt_price(result['buy_price']))} x {_code(result.get('size', '?'))}")
    if result.get("sell_price") is not None:
        lines.append(f"recovery sell {_code(_fmt_price(result['sell_price']))} (GTC)")
    if result.get("buy_order_id"):
        lines.append(f"buy id {_code(_fmt_order_id(result['buy_order_id']))}")
    if result.get("sell_order_id"):
        lines.append(f"sell id {_code(_fmt_order_id(result['sell_order_id']))}")
    return "\n".join(lines).rstrip() + "\n"


def format_skip_message(result: dict[str, Any]) -> str:
    """Summary of a skipped trigger."""
    slug = result.get("slug") or "?"
    team = result.get("team") or "?"
    matchup = result.get("matchup") or slug
    return (
        f"⏭ {_bold('SKIP')} {_bold(matchup)}\n"
        f"team {_code(team)} - reason {_code(result.get('reason') or 'unknown')}\n"
    )


def notify_fetch(result: dict[str, Any]) -> None:
    if not telegram_configured():
        return
    try:
        _send_chunks(format_fetch_message(result))
    except Exception:
        logger.exception("Telegram fetch notify failed")


def notify_trade(result: dict[str, Any]) -> None:
    if not telegram_configured():
        return
    try:
        _send_chunks(format_trade_message(result))
    except Exception:
        logger.exception("Telegram trade notify failed")


def notify_skip(result: dict[str, Any]) -> None:
    if not telegram_configured():
        return
    try:
        _send_chunks(format_skip_message(result))
    except Exception:
        logger.exception("Telegram skip notify failed")
