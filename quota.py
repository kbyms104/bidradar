#!/usr/bin/env python3
"""공공데이터포털 API 일일 호출 한도 관리.

개발계정 한도는 하루 1,000회입니다. 15분 주기 수집은 유형 5개 × 1~2페이지라
하루 480~960회를 씁니다. 공고가 몰리는 날에는 한도에 닿아 수집이 멈출 수 있어,
호출 수를 파일에 기록하고 **한도에 닿기 전에 자동으로 절전 모드**로 들어갑니다.

    python3 quota.py            # 오늘 사용량
    python3 quota.py --reset    # 오늘 카운터 초기화 (운영자 수동)

동작
- 하루 예산 = 한도 - 예비(기본 100). 예비는 백필·수동 점검용으로 남겨둡니다.
- 사용률 70%를 넘으면 collector가 유형별 1페이지만 받습니다(호출 절반).
- 70% / 90% / 100%를 넘는 순간 디스코드 운영 채널로 한 번씩 알립니다.
- 한도 초과 오류(코드 22/23)를 받으면 그날은 차단 상태로 기록합니다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import config
import notify

KST = timezone(timedelta(hours=9))
PATH = os.path.join(config.DATA_DIR, "api_quota.json")
THRESHOLDS = (70, 90, 100)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def limit() -> int:
    """포털 개발계정 일일 한도 (기본 1,000)."""
    return max(50, _int_env("BIDRADAR_API_DAILY_LIMIT", 1000))


def reserve() -> int:
    """수동 작업용으로 남겨두는 호출 수."""
    return max(0, min(_int_env("BIDRADAR_API_RESERVE", 100), limit() // 2))


def budget() -> int:
    return max(1, limit() - reserve())


def today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _blank() -> dict:
    return {"date": today(), "calls": 0, "alerted": [], "blocked": None}


def load() -> dict:
    data = _blank()
    try:
        with open(PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return data
    if not isinstance(saved, dict) or saved.get("date") != data["date"]:
        return data  # 날짜가 바뀌면 자동 리셋
    if isinstance(saved.get("calls"), int) and saved["calls"] >= 0:
        data["calls"] = saved["calls"]
    if isinstance(saved.get("alerted"), list):
        data["alerted"] = [t for t in saved["alerted"] if t in THRESHOLDS]
    if isinstance(saved.get("blocked"), str):
        data["blocked"] = saved["blocked"][:200]
    return data


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PATH), prefix=".quota-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def used() -> int:
    return load()["calls"]


def remaining() -> int:
    data = load()
    if data["blocked"]:
        return 0
    return max(0, budget() - data["calls"])


def percent() -> float:
    return used() / budget() * 100


def _alert(data: dict, threshold: int) -> None:
    pct = data["calls"] / budget() * 100
    if data["blocked"]:
        head = "🚫 BidRadar API 한도 소진"
    elif threshold >= 100:
        head = "🚫 BidRadar API 예산 소진"
    elif threshold >= 90:
        head = "⚠️ BidRadar API 한도 임박"
    else:
        head = "⚠️ BidRadar API 사용량 주의"
    text = (
        f"{head}\n"
        f"오늘 호출 {data['calls']:,} / 예산 {budget():,}회 ({pct:.0f}%)\n"
        f"포털 한도 {limit():,}회 · 예비 {reserve():,}회\n"
        f"리셋: 내일 00:00 KST"
    )
    if threshold >= 70 and data["calls"] < budget():
        text += "\n→ 수집을 유형별 1페이지로 줄여서 계속합니다."
    elif threshold >= 100 or data["blocked"]:
        text += "\n→ 다음 리셋까지 수집이 멈춥니다. 고객 알림은 기존 공고로 계속 발송됩니다."
    ok, detail = notify.send_discord(text)
    notify._log(f"quota alert {threshold}% ({data['calls']}/{budget()}) -> {detail}")


def take(n: int = 1) -> bool:
    """호출 n회를 예산에서 미리 차감합니다. 예산이 없으면 False."""
    data = load()
    if data["blocked"]:
        return False
    if data["calls"] + n > budget():
        return False
    data["calls"] += n
    for threshold in THRESHOLDS:
        if threshold not in data["alerted"] and data["calls"] / budget() * 100 >= threshold:
            data["alerted"].append(threshold)
            save(data)
            _alert(data, threshold)
    save(data)
    return True


def release(n: int = 1) -> None:
    """연결 자체가 실패해 포털에 도달하지 못한 호출을 되돌립니다."""
    data = load()
    data["calls"] = max(0, data["calls"] - n)
    save(data)


def note_quota_error(message: str = "") -> None:
    """포털이 한도 초과를 알려오면 그날 수집을 멈춥니다."""
    data = load()
    data["blocked"] = (message or "quota exceeded")[:200]
    save(data)
    _alert(data, 100)


def looks_like_quota_error(code: str, message: str) -> bool:
    code = str(code or "").strip()
    message = (message or "").upper()
    return code in ("22", "23") or "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in message


def pages_per_type(default: int = 2) -> int:
    """사용률이 70%를 넘으면 유형별 1페이지만 받아 호출을 절반으로 줄입니다."""
    if percent() >= 70 or load()["blocked"]:
        return 1
    return max(1, default)


def status() -> dict:
    data = load()
    return {
        "date": data["date"],
        "calls": data["calls"],
        "budget": budget(),
        "limit": limit(),
        "reserve": reserve(),
        "percent": round(data["calls"] / budget() * 100, 1),
        "remaining": remaining(),
        "blocked": data["blocked"],
        "throttled": percent() >= 70,
    }


def _bar(pct: float, width: int = 20) -> str:
    filled = min(width, int(round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="공공데이터 API 일일 호출 한도")
    ap.add_argument("--reset", action="store_true", help="오늘 카운터 초기화")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    args = ap.parse_args(argv)

    if args.reset:
        save(_blank())
        print("오늘 호출 카운터를 초기화했습니다.")

    info = status()
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0

    print(f"공공데이터 API — {info['date']} (KST)")
    print(f"  사용 {info['calls']:,} / 예산 {info['budget']:,}회  {_bar(info['percent'])} {info['percent']:.0f}%")
    print(f"  포털 한도 {info['limit']:,}회 · 예비 {info['reserve']:,}회 · 남음 {info['remaining']:,}회")
    if info["blocked"]:
        print(f"  차단됨: {info['blocked']}")
    elif info["throttled"]:
        print("  절전 모드: 유형별 1페이지로 수집 중 (70% 초과)")
    else:
        print("  정상: 유형별 최대 2페이지 수집")
    return 0


if __name__ == "__main__":
    sys.exit(main())
