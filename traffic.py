#!/usr/bin/env python3
"""사이트 트래픽 급증·대역폭 감시.

Oracle Always Free는 월 10TB 아웃바운드까지 무료지만, 갑자기 요청이 몰리면
CPU·메모리가 먼저 버티지 못합니다. nginx 로그를 읽어 지난 1시간 사용량을
요약하고, 평소와 다른 급증이 보이면 디스코드 운영 채널로 알립니다.

    python3 traffic.py              # 지난 1시간 요약
    python3 traffic.py --minutes 10 # 구간 변경
    python3 traffic.py --json

cron (매시 5분):
    5 * * * * cd /home/ubuntu/bidradar && /usr/bin/python3 traffic.py >> logs/traffic.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

import config
import notify

KST = timezone(timedelta(hours=9))
LOG_PATH = os.environ.get("BIDRADAR_ACCESS_LOG") or "/var/log/nginx/bidradar.access.log"
STATE_PATH = os.path.join(config.DATA_DIR, "traffic.json")
LINE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<req>[^"]*)" '
    r'(?P<status>\d{3}) (?P<bytes>\d+|-)'
)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def thresholds() -> dict:
    return {
        "requests": _int_env("BIDRADAR_TRAFFIC_ALERT_REQ", 300),
        "ip": _int_env("BIDRADAR_TRAFFIC_ALERT_IP", 150),
        "mb": _int_env("BIDRADAR_TRAFFIC_ALERT_MB", 500),
    }


def _detect_public_ip() -> str:
    """서버 자신의 공인 IP. 자체 점검 트래픽을 급증으로 오인하지 않기 위해 씁니다."""
    try:
        req = urllib.request.Request("https://api.ipify.org",
                                     headers={"User-Agent": "BidRadar/1.0"})
        with urllib.request.urlopen(req, timeout=3) as res:
            ip = res.read().decode("ascii", "replace").strip()
        return ip if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", ip) else ""
    except Exception:
        return ""


def self_ips() -> set[str]:
    """BIDRADAR_SELF_IPS(쉼표 구분) + 자동 감지한 서버 공인 IP."""
    ips = {x.strip() for x in (os.environ.get("BIDRADAR_SELF_IPS") or "").split(",") if x.strip()}
    state = _state()
    cached = [ip for ip in (state.get("self_ips") or []) if isinstance(ip, str)]
    fetched_at = int(state.get("self_ips_at") or 0)
    if not cached or time.time() - fetched_at > 7 * 86400:
        detected = _detect_public_ip()
        if detected:
            cached = [detected]
            state["self_ips"] = cached
            state["self_ips_at"] = int(time.time())
            _save(state)
    ips.update(cached)
    return ips


def _files() -> list[str]:
    paths = [LOG_PATH, LOG_PATH + ".1"]
    return [p for p in paths if os.path.isfile(p)]


def scan(minutes: int = 60) -> dict:
    """최근 N분 로그를 집계합니다. 회전된 로그(.1)도 함께 읽습니다."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    skip = self_ips()
    total = 0
    skipped = 0
    size = 0
    status = Counter()
    ips = Counter()
    paths = Counter()
    for path in _files():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = LINE.match(line)
                    if not m:
                        continue
                    try:
                        ts = datetime.strptime(m.group("ts"), "%d/%b/%Y:%H:%M:%S %z")
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    if m.group("ip") in skip:
                        skipped += 1
                        continue
                    total += 1
                    try:
                        size += int(m.group("bytes"))
                    except ValueError:
                        pass
                    status[m.group("status")] += 1
                    ips[m.group("ip")] += 1
                    parts = m.group("req").split()
                    if len(parts) >= 2:
                        paths[parts[1].split("?")[0]] += 1
        except OSError:
            continue
    return {
        "minutes": minutes,
        "requests": total,
        "bytes": size,
        "mb": round(size / 1048576, 1),
        "unique_ips": len(ips),
        "top_ip": ips.most_common(3),
        "top_paths": paths.most_common(5),
        "errors": status.get("404", 0) + status.get("400", 0) + status.get("444", 0),
        "self_skipped": skipped,
    }


def _state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def breaches(info: dict, limits: dict) -> list[str]:
    out = []
    if info["requests"] >= limits["requests"]:
        out.append(f"요청 {info['requests']:,}건 / {info['minutes']}분 (기준 {limits['requests']:,})")
    if info["top_ip"] and info["top_ip"][0][1] >= limits["ip"]:
        out.append(f"단일 IP {info['top_ip'][0][0]} → {info['top_ip'][0][1]:,}건 (기준 {limits['ip']:,})")
    if info["mb"] >= limits["mb"]:
        out.append(f"전송량 {info['mb']:,}MB (기준 {limits['mb']:,}MB)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="사이트 트래픽 감시")
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    args = ap.parse_args(argv)

    info = scan(max(1, args.minutes))
    limits = thresholds()
    hits = breaches(info, limits)
    info["alerts"] = hits

    if args.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0

    print(f"트래픽 최근 {info['minutes']}분 — {datetime.now(KST).strftime('%m-%d %H:%M')} KST")
    print(f"  요청 {info['requests']:,}건 · 전송 {info['mb']:,}MB · 고유 IP {info['unique_ips']} · 4xx {info['errors']:,}")
    if info["self_skipped"]:
        print(f"  자체 점검 트래픽 {info['self_skipped']:,}건 제외")
    if info["top_ip"]:
        print("  상위 IP: " + ", ".join(f"{ip}({n:,})" for ip, n in info["top_ip"]))
    if info["top_paths"]:
        print("  상위 경로: " + ", ".join(f"{p} {n:,}" for p, n in info["top_paths"]))

    if not hits:
        print("  정상 범위")
        return 0

    print("  ⚠ 급증: " + " · ".join(hits))
    if args.no_alert:
        return 0

    state = _state()
    now = int(time.time())
    if now - int(state.get("last_alert") or 0) < 3600:
        print("  (1시간 내 이미 알림을 보냈습니다)")
        return 0

    text = (
        "📈 BidRadar 트래픽 급증\n"
        + "\n".join(f"· {h}" for h in hits)
        + f"\n최근 {info['minutes']}분: 요청 {info['requests']:,} · {info['mb']:,}MB · IP {info['unique_ips']}"
        + ("\n상위 IP: " + ", ".join(f"{ip}({n:,})" for ip, n in info["top_ip"]) if info["top_ip"] else "")
        + "\n\n확인: `tail -50 /var/log/nginx/bidradar.access.log`"
    )
    ok, detail = notify.send_discord(text)
    notify._log(f"traffic alert -> {detail}")
    state["last_alert"] = now
    state["last_detail"] = info
    _save(state)
    print(f"  디스코드 알림: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
