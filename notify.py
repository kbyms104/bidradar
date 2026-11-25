#!/usr/bin/env python3
"""상담 신청 알림.

디스코드 웹훅 또는 텔레그램 봇이 설정되어 있으면 새 상담 신청을 즉시
보내고, 없으면 `data/notifications.log`에만 남깁니다. 알림 실패가 폼
접수를 막지 않습니다. 디스코드가 설정되어 있으면 디스코드를 우선 사용합니다.

.env
    DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
    TELEGRAM_BOT_TOKEN=123456:ABC...   (선택)
    TELEGRAM_CHAT_ID=123456789          (선택)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import config

LOG_PATH = os.path.join(config.DATA_DIR, "notifications.log")
USER_AGENT = "BidRadar/0.2"
DISCORD_LIMIT = 1900  # Discord content 최대 2000자


def _log(line: str) -> None:
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def discord_ready() -> bool:
    url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    return url.startswith("https://discord.com/api/webhooks/")


def telegram_ready() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def channel() -> str:
    """설정된 알림 채널: discord, telegram, 또는 빈 문자열."""
    if discord_ready():
        return "discord"
    if telegram_ready():
        return "telegram"
    return ""


def is_webhook(url: str) -> bool:
    """디스코드 웹훅 URL 형식인지 확인합니다."""
    return (url or "").strip().startswith("https://discord.com/api/webhooks/")


def send_discord(text: str, timeout: float = 6.0) -> tuple[bool, str]:
    return send_discord_to(os.environ.get("DISCORD_WEBHOOK_URL") or "", text, timeout)


def send_discord_to(url: str, text: str, timeout: float = 6.0) -> tuple[bool, str]:
    """지정한 웹훅으로 보냅니다 (고객별 채널 발송용)."""
    url = (url or "").strip()
    if not url:
        _log("skip discord (not configured)")
        return False, "not configured"

    body = json.dumps(
        {"content": text[:DISCORD_LIMIT], "allowed_mentions": {"parse": []}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            ok = 200 <= res.status < 300
        _log(f"discord {'sent' if ok else 'failed'}")
        return ok, "sent" if ok else "failed"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        _log(f"discord HTTP {exc.code}: {detail}")
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log(f"discord error: {exc}")
        return False, str(exc)


def send_telegram(text: str, timeout: float = 5.0) -> tuple[bool, str]:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        _log("skip telegram (not configured)")
        return False, "not configured"

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            ok = 200 <= res.status < 300
        _log(f"telegram {'sent' if ok else 'failed'}")
        return ok, "sent" if ok else "failed"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        _log(f"telegram HTTP {exc.code}: {body}")
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log(f"telegram error: {exc}")
        return False, str(exc)


def send_notification(text: str) -> tuple[bool, str]:
    """설정된 채널로 알림을 보냅니다. 디스코드 우선, 없으면 텔레그램."""
    ch = channel()
    if ch == "discord":
        return send_discord(text)
    if ch == "telegram":
        return send_telegram(text)
    _log("skip notify (not configured)")
    return False, "not configured"


def lead_message(lead_id: int, lead: dict) -> str:
    lines = [
        f"[BidRadar] 새 상담 신청 #{lead_id}",
        f"회사: {lead.get('company', '-')}",
        f"담당자: {lead.get('name', '-')}",
        f"연락처: {lead.get('contact', '-')}",
    ]
    if lead.get("keywords"):
        lines.append(f"키워드/지역: {lead['keywords']}")
    if lead.get("method"):
        lines.append(f"현재 확인 방법: {lead['method']}")
    if lead.get("message"):
        lines.append(f"문의: {lead['message'][:300]}")
    lines.append("확인: python3 ~/bidradar/leads.py")
    return "\n".join(lines)


def notify_lead(lead_id: int, lead: dict) -> None:
    send_notification(lead_message(lead_id, lead))


if __name__ == "__main__":
    config.load_env()
    print("channel:", channel() or "not configured")
    ok, detail = send_notification("[BidRadar] 알림 테스트")
    print("result:", ok, detail)
