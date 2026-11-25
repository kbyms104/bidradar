#!/usr/bin/env python3
"""브라우저 진단 로그(logs/client.log) 확인 도구.

    python3 clientlog.py               # 최근 30건 요약
    python3 clientlog.py --tail 100    # 최근 100건 원문
    python3 clientlog.py --errors      # 오류만
    python3 clientlog.py --clear       # 로그 비우기
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

import config

PATH = os.path.join(config.BASE, "logs", "client.log")
KST = timezone(timedelta(hours=9))


def load(limit: int) -> list[dict]:
    if not os.path.isfile(PATH):
        return []
    with open(PATH, encoding="utf-8") as fh:
        lines = fh.readlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def label(item: dict) -> str:
    kind = item.get("kind", "?")
    stamp = datetime.fromtimestamp(item.get("ts", 0), KST).strftime("%m-%d %H:%M:%S")
    bits = [f"{stamp} {kind:<10} {item.get('path','')}"]
    if item.get("message"):
        bits.append(f"  {item['message']}")
    if item.get("src"):
        bits.append(f"  at {item['src']}")
    extra = item.get("extra") or {}
    if extra:
        bits.append("  " + " · ".join(f"{k}={v}" for k, v in extra.items()))
    if item.get("ua"):
        bits.append(f"  ua: {item['ua'][:110]}")
    return "\n".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description="브라우저 진단 로그 확인")
    ap.add_argument("--tail", type=int, default=30)
    ap.add_argument("--errors", action="store_true")
    ap.add_argument("--clear", action="store_true")
    args = ap.parse_args()

    if args.clear:
        open(PATH, "w").close()
        print("로그를 비웠습니다.")
        return 0

    items = load(args.tail)
    if args.errors:
        items = [i for i in items if i.get("kind") not in ("hello", "demo_click")]
    if not items:
        print("기록이 없습니다.")
        return 0
    for item in items:
        print(label(item))
        print("-" * 70)
    counts = Counter(i.get("kind") for i in items)
    print("요약:", dict(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
