#!/usr/bin/env python3
"""BidRadar 상담 신청(리드) 조회 도구.

랜딩페이지 폼이 저장한 data/leads.db 내용을 터미널에서 확인합니다.

    python3 leads.py            # 최근 20건
    python3 leads.py --limit 50
    python3 leads.py --csv > leads.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LEADS_DB = os.path.join(BASE, "data", "leads.db")
KST = timezone(timedelta(hours=9))
COLUMNS = ("id", "created_at", "company", "name", "contact", "keywords", "method", "message")


def connect() -> sqlite3.Connection | None:
    if not os.path.isfile(LEADS_DB):
        return None
    con = sqlite3.connect(f"file:{LEADS_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def fetch(limit: int) -> list[dict]:
    con = connect()
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT id, created_at, company, name, contact, keywords, method, message "
            "FROM leads ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def print_table(items: list[dict]) -> None:
    if not items:
        print("아직 접수된 상담 신청이 없습니다.")
        return
    for item in items:
        when = datetime.fromtimestamp(item["created_at"], KST).strftime("%Y-%m-%d %H:%M")
        print(f"#{item['id']}  {when}  {item['company']} / {item['name']} / {item['contact']}")
        extra = [v for v in (item["keywords"], item["method"], item["message"]) if v]
        if extra:
            print(f"    {' · '.join(extra)}")
    print(f"\n총 {len(items)}건 표시 (최신순)")


def main() -> int:
    parser = argparse.ArgumentParser(description="BidRadar 상담 신청 조회")
    parser.add_argument("--limit", type=int, default=20, help="표시할 건수 (기본 20)")
    parser.add_argument("--csv", action="store_true", help="CSV로 출력")
    args = parser.parse_args()

    items = fetch(max(1, min(500, args.limit)))
    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(COLUMNS)
        for item in items:
            writer.writerow([item[c] for c in COLUMNS])
        return 0
    print_table(items)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
