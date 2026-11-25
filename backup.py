#!/usr/bin/env python3
"""고객 계정·상담 DB 백업.

`data/app.db`(계정·조건·발송 이력)와 `data/leads.db`(상담 신청)는 다시 만들 수 없습니다.
WAL 모드에서도 안전하도록 SQLite 백업 API를 사용합니다.

    python3 backup.py                 # 기본: ~/backup/bidradar, 14일 보관
    python3 backup.py --keep 30       # 30일 보관
    python3 backup.py --dir /path     # 저장 위치 변경
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time

import config

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "backup", "bidradar")
SOURCES = (("app.db", "app"), ("leads.db", "leads"))


def backup_one(src: str, dest: str) -> int:
    source = sqlite3.connect(src, timeout=15)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    finally:
        target.close()
        source.close()
    os.chmod(dest, 0o600)
    return os.path.getsize(dest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BidRadar DB 백업")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--keep", type=int, default=14, help="보관 일수 (기본 14)")
    args = ap.parse_args(argv)

    os.makedirs(args.dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    for filename, prefix in SOURCES:
        src = os.path.join(config.DATA_DIR, filename)
        if not os.path.isfile(src):
            print(f"건너뜀: {src} 없음")
            continue
        dest = os.path.join(args.dir, f"{prefix}-{stamp}.db")
        try:
            size = backup_one(src, dest)
            print(f"백업: {dest} ({size / 1024:.0f} KB)")
        except sqlite3.Error as exc:
            print(f"실패: {filename} → {exc}")
            return 1

    cutoff = time.time() - max(1, args.keep) * 86400
    removed = 0
    for name in os.listdir(args.dir):
        if not name.endswith(".db"):
            continue
        path = os.path.join(args.dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        print(f"오래된 백업 {removed}개 삭제 (보관 {args.keep}일)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
