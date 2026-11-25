#!/usr/bin/env python3
"""고객별 마감 임박(D-3 / D-1) 디스코드 알림.

`alerts.json`에 등록한 고객 조건(키워드·지역·업종·금액)에 맞는 공고를
찾아 마감 D-3 / D-1에 디스코드 웹훅으로 보냅니다. 같은 공고를 같은
단계로 두 번 보내지 않도록 `data/alerts.db`에 발송 이력을 남깁니다.

사용
    python3 alerts.py --preview                 # 발송 대상만 확인 (기본)
    python3 alerts.py --send --all              # 전체 고객 실제 발송 (cron)
    python3 alerts.py --send --customer sample  # 한 고객만
    python3 alerts.py --send --stage 3          # D-3 단계만
    python3 alerts.py --send --customer sample --test   # 웹훅 연결 테스트
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import accounts
import config
import notify

BASE = config.BASE
CONFIG_PATH = os.path.join(BASE, "alerts.json")
ALERTS_DB = os.path.join(config.DATA_DIR, "alerts.db")
KST = timezone(timedelta(hours=9))
DAY = 86400
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

# (하한, 상한] — D-3은 1~3일 남은 공고, D-1은 1일 이내 공고.
WINDOWS = {3: (1, 3), 1: (0, 1)}
STAGES = (3, 1)


# ------------------------------------------------------------------ 공통

def bids_db() -> str:
    """서버와 같은 규칙으로 실데이터/데모 DB를 고릅니다."""
    return (os.environ.get("BIDRADAR_DB")
            or (config.LIVE_DB if config.source() == "live" else config.DEMO_DB))


def connect_read() -> sqlite3.Connection:
    path = bids_db()
    if not os.path.isfile(path):
        raise SystemExit(f"공고 DB가 없습니다: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def connect_alerts() -> sqlite3.Connection:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    con = sqlite3.connect(ALERTS_DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS sent (
            customer TEXT NOT NULL,
            bid_id   TEXT NOT NULL,
            stage    INTEGER NOT NULL,
            sent_at  INTEGER NOT NULL,
            PRIMARY KEY (customer, bid_id, stage)
        )
    """)
    con.commit()
    return con


def fmt_amount(value) -> str:
    n = int(value or 0)
    if n <= 0:
        return "금액 미정"
    if n >= 1_0000_0000_0000:
        return f"{n / 1_0000_0000_0000:.2f}".rstrip("0").rstrip(".") + "조"
    if n >= 1_0000_0000:
        eok = n / 1_0000_0000
        return (f"{eok:.0f}" if eok >= 10 else f"{eok:.1f}".rstrip("0").rstrip(".")) + "억"
    if n >= 1_0000:
        return f"{round(n / 1_0000):,}만"
    return f"{n:,}"


def days_left(close_at: int, now: float) -> int:
    """남은 일수(올림). 마감까지 2.3일이면 3."""
    return max(0, int((close_at - now + DAY - 1) // DAY))


# ------------------------------------------------------------------ 조건

def load_config(path: str = CONFIG_PATH) -> list[dict]:
    if not os.path.isfile(path):
        raise SystemExit(f"설정 파일이 없습니다: {path}")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    customers = data.get("customers") if isinstance(data, dict) else data
    if not isinstance(customers, list):
        raise SystemExit("alerts.json 형식이 올바르지 않습니다. {\"customers\": [...]}")
    out = []
    for raw in customers:
        cid = (raw.get("id") or "").strip()
        if not cid:
            raise SystemExit("고객 id가 없는 항목이 있습니다.")
        out.append({
            "id": cid,
            "name": raw.get("name") or cid,
            "enabled": bool(raw.get("enabled", True)),
            "webhook": raw.get("webhook") or "",
            "keywords": [k.strip() for k in raw.get("keywords") or [] if k.strip()],
            "exclude_keywords": [k.strip() for k in raw.get("exclude_keywords") or [] if k.strip()],
            "regions": [r.strip() for r in raw.get("regions") or [] if r.strip()],
            "categories": [c.strip() for c in raw.get("categories") or [] if c.strip()],
            "min_amount": int(raw.get("min_amount") or 0),
            "stages": [int(s) for s in raw.get("stages") or STAGES if int(s) in STAGES],
        })
    return out


def customers_from_db() -> list[dict]:
    """`data/app.db`의 활성 계정 + 활성 조건을 발송 단위로 펼칩니다."""
    con = accounts.connect()
    try:
        rows = con.execute(
            "SELECT a.id AS account_id, a.email, a.company, a.webhook, "
            "c.id AS condition_id, c.name AS condition_name, c.keywords, "
            "c.exclude_keywords, c.regions, c.categories, c.min_amount, c.stages "
            "FROM accounts a JOIN conditions c ON c.account_id=a.id "
            "WHERE a.status='active' AND c.enabled=1 ORDER BY a.id, c.id"
        ).fetchall()
    finally:
        con.close()

    out = []
    for r in rows:
        out.append({
            "id": f"acc{r['account_id']}-c{r['condition_id']}",
            "account_id": int(r["account_id"]),
            "condition_id": int(r["condition_id"]),
            "name": f"{r['company'] or r['email']} · {r['condition_name']}",
            "condition_name": r["condition_name"],
            "email": r["email"],
            "enabled": True,
            "webhook": r["webhook"] or "",
            "keywords": accounts.split_list(r["keywords"]),
            "exclude_keywords": accounts.split_list(r["exclude_keywords"]),
            "regions": accounts.split_list(r["regions"], 20),
            "categories": accounts.split_list(r["categories"], 20),
            "min_amount": int(r["min_amount"] or 0),
            "stages": [int(s) for s in (r["stages"] or "").split(",") if s.strip().isdigit()] or [3, 1],
        })
    return out


def preview_for(account_id: int, limit: int = 3) -> list[dict]:
    """고객 콘솔의 알림 패널 — 그 계정 조건에 맞는 공고만 보여줍니다."""
    conds = accounts.conditions_for(account_id, enabled_only=True)
    now = time.time()
    con = connect_read()
    try:
        items = []
        for cond in conds:
            for stage in (1, 3):
                found = [r for r in window_items(con, stage, now) if matches(r, cond)]
                items.extend(dedupe(found))
    finally:
        con.close()

    # 조건이 여러 개면 같은 공고가 중복될 수 있어 한 번 더 정리합니다.
    seen, unique = set(), []
    for item in items:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)
    unique.sort(key=lambda r: r["close_at"])

    d1 = sum(1 for r in unique if (r["close_at"] - now) <= DAY)
    d3 = sum(1 for r in unique if DAY < (r["close_at"] - now) <= 3 * DAY)
    out = [{
        "tone": "summary",
        "title": f"내 조건 마감 {len(unique)}건" if conds else "알림 조건을 설정하세요",
        "body": (f"D-1 {d1}건 · D-3 {d3}건 · 원문 링크 포함" if conds
                 else "설정에서 키워드·지역을 등록하면 D-3/D-1에 보내드립니다."),
        "time": "09:00",
    }]
    for item in unique[:limit]:
        d = days_left(item["close_at"], now)
        close = datetime.fromtimestamp(item["close_at"], KST)
        out.append({
            "tone": "urgent" if d <= 1 else "normal",
            "title": f"[D-{d}] {item['title'][:34]}",
            "body": f"{item['org']} · {close:%m.%d %H:%M} 마감",
            "time": close.strftime("%H:%M"),
        })
    return out


def webhook_of(customer: dict) -> str:
    """`env:NAME` 이면 환경변수에서 읽습니다."""
    value = (customer.get("webhook") or "").strip()
    if value.startswith("env:"):
        return (os.environ.get(value[4:].strip()) or "").strip()
    return value


def matches(item: sqlite3.Row, customer: dict) -> bool:
    title = (item["title"] or "").lower()
    org = (item["org"] or "").lower()
    text = f"{title} {org}"

    if customer["keywords"] and not any(k.lower() in text for k in customer["keywords"]):
        return False
    if customer["exclude_keywords"] and any(k.lower() in text for k in customer["exclude_keywords"]):
        return False
    if customer["categories"] and item["category"] not in customer["categories"]:
        return False
    if customer["regions"]:
        region = item["region"] or ""
        if region != "전국" and region not in customer["regions"]:
            return False
    if customer["min_amount"] > 0 and int(item["amount"] or 0) < customer["min_amount"]:
        return False
    return True


def window_items(con: sqlite3.Connection, stage: int, now: float) -> list[sqlite3.Row]:
    lo_days, hi_days = WINDOWS[stage]
    lo = now + lo_days * DAY
    hi = now + hi_days * DAY
    return con.execute(
        "SELECT id, notice_no, title, org, region, category, amount, close_at, url "
        "FROM bids WHERE close_at > ? AND close_at <= ? ORDER BY close_at ASC",
        (lo, hi),
    ).fetchall()


def dedupe(items: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """같은 사업의 변경공고(차수)가 여러 건 잡히면 한 번만 보냅니다."""
    seen = set()
    out = []
    for item in items:
        key = ((item["title"] or "").strip(), (item["org"] or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def pending(con_alerts: sqlite3.Connection, customer: dict, stage: int,
            items: list[sqlite3.Row]) -> list[sqlite3.Row]:
    sent = {r["bid_id"] for r in con_alerts.execute(
        "SELECT bid_id FROM sent WHERE customer=? AND stage=?", (customer["id"], stage)
    )}
    return [item for item in items if item["id"] not in sent]


# ------------------------------------------------------------------ 메시지

def close_label(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, KST)
    return f"{dt.month}.{dt.day:02d}({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def condition_label(customer: dict) -> str:
    parts = []
    if customer["keywords"]:
        parts.append(", ".join(customer["keywords"][:5]))
    if customer["categories"]:
        parts.append(" · ".join(customer["categories"][:3]))
    if customer["regions"]:
        parts.append(", ".join(customer["regions"][:4]))
    if customer["min_amount"]:
        parts.append(f"{fmt_amount(customer['min_amount'])} 이상")
    return " / ".join(parts) if parts else "전체 공고"


def format_message(customer: dict, stage: int, items: list[sqlite3.Row],
                   now: float, limit: int, site: str) -> str:
    today = datetime.fromtimestamp(now, KST)
    head = (f"**BidRadar 마감 D-{stage} 알림** · "
            f"{today.month}/{today.day}({WEEKDAYS[today.weekday()]}) · {len(items)}건")
    lines = [head, f"조건: {condition_label(customer)}", ""]

    for item in items[:limit]:
        d = days_left(item["close_at"], now)
        url = (item["url"] or "").strip()
        lines.append(f"**[D-{d}] {item['title']}**")
        lines.append(f"{item['org']} · {fmt_amount(item['amount'])} · {close_label(item['close_at'])} 마감")
        if url:
            lines.append(f"<{url}>")
        lines.append("")

    if len(items) > limit:
        lines.append(f"외 {len(items) - limit}건은 대시보드에서 확인하세요.")
        lines.append(site)
    return "\n".join(lines).strip()


def preview(limit: int = 3, customer: dict | None = None) -> list[dict]:
    """대시보드 알림 패널용. 실제 발송 엔진과 같은 조건으로 만든 미리보기입니다."""
    now = time.time()
    if customer is None:
        try:
            enabled = [c for c in load_config() if c.get("enabled")]
            customer = enabled[0] if enabled else None
        except (SystemExit, ValueError):
            customer = None

    def collect(use_filter: bool) -> list[sqlite3.Row]:
        con = connect_read()
        try:
            items = []
            for stage in (1, 3):
                found = window_items(con, stage, now)
                if use_filter and customer:
                    found = [r for r in found if matches(r, customer)]
                items.extend(dedupe(found))
            return items
        finally:
            con.close()

    items = collect(True)
    if not items and customer:
        items = collect(False)

    items.sort(key=lambda r: r["close_at"])
    d1 = sum(1 for r in items if (r["close_at"] - now) <= DAY)
    d3 = sum(1 for r in items if DAY < (r["close_at"] - now) <= 3 * DAY)
    total = len(items)

    out = [{
        "tone": "summary",
        "title": f"3일 내 마감 {total}건",
        "body": f"D-1 {d1}건 · D-3 {d3}건 · 원문 링크 포함",
        "time": "09:00",
    }]
    for item in items[:limit]:
        d = days_left(item["close_at"], now)
        close = datetime.fromtimestamp(item["close_at"], KST)
        out.append({
            "tone": "urgent" if d <= 1 else "normal",
            "title": f"[D-{d}] {item['title'][:34]}",
            "body": f"{item['org']} · {close:%m.%d %H:%M} 마감",
            "time": close.strftime("%H:%M"),
        })
    return out


# ------------------------------------------------------------------ 실행

def run_customer(customer: dict, stages: list[int], now: float, limit: int,
                 dry_run: bool, site: str) -> int:
    con_bids = connect_read()
    account_id = customer.get("account_id")
    condition_id = customer.get("condition_id")
    con_alerts = None if account_id else connect_alerts()
    sent_count = 0
    try:
        for stage in stages:
            items = dedupe([r for r in window_items(con_bids, stage, now) if matches(r, customer)])
            if account_id:
                done = accounts.already_sent(account_id, condition_id, stage)
                items = [item for item in items if item["id"] not in done]
            else:
                items = pending(con_alerts, customer, stage, items)
            if not items:
                print(f"[{customer['id']}] D-{stage}: 발송 대상 없음")
                continue

            text = format_message(customer, stage, items, now, limit, site)
            if dry_run:
                print(f"[{customer['id']}] D-{stage}: {len(items)}건 (미리보기)")
                print(text)
                print("-" * 60)
                continue

            url = webhook_of(customer)
            if not notify.is_webhook(url):
                print(f"[{customer['id']}] D-{stage}: 웹훅이 없거나 형식이 잘못됨 → 건너뜀",
                      file=sys.stderr)
                if account_id:
                    accounts.log_alert(account_id, condition_id,
                                       customer.get("condition_name") or customer["name"],
                                       stage, len(items), False, "웹훅 미설정")
                continue
            ok, detail = notify.send_discord_to(url, text)
            print(f"[{customer['id']}] D-{stage}: {len(items)}건 → {detail}")
            if ok:
                if account_id:
                    accounts.mark_sent(account_id, condition_id, stage,
                                       [item["id"] for item in items])
                else:
                    con_alerts.executemany(
                        "INSERT OR IGNORE INTO sent (customer, bid_id, stage, sent_at) "
                        "VALUES (?,?,?,?)",
                        [(customer["id"], item["id"], stage, int(now)) for item in items],
                    )
                    con_alerts.commit()
                sent_count += len(items)
            if account_id:
                accounts.log_alert(account_id, condition_id,
                                   customer.get("condition_name") or customer["name"],
                                   stage, len(items), ok, detail)
    finally:
        con_bids.close()
        if con_alerts is not None:
            con_alerts.close()
    return sent_count


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    site = os.environ.get("BIDRADAR_SITE") or "https://bidradar-kr.duckdns.org/demo"

    ap = argparse.ArgumentParser(description="마감 D-3 / D-1 디스코드 알림")
    ap.add_argument("--config", default=CONFIG_PATH, help="고객 조건 파일 (기본 alerts.json)")
    ap.add_argument("--customer", help="특정 고객 id만 실행")
    ap.add_argument("--stage", type=int, choices=STAGES, help="특정 단계만 실행 (3 또는 1)")
    ap.add_argument("--all", action="store_true", help="활성 고객 전체 실행")
    ap.add_argument("--preview", action="store_true", help="발송 대상만 확인 (기본값)")
    ap.add_argument("--send", action="store_true", help="실제 발송")
    ap.add_argument("--test", action="store_true", help="웹훅 연결 테스트 메시지 발송")
    ap.add_argument("--limit", type=int, default=10, help="메시지당 최대 공고 수 (기본 10)")
    ap.add_argument("--source", choices=("auto", "db", "file"), default="auto",
                    help="고객 조건 출처: db(가입 고객) / file(alerts.json) / auto")
    args = ap.parse_args(argv)

    source = args.source
    if source == "auto":
        db_customers = customers_from_db()
        if db_customers:
            source = "db"
        else:
            source = "file"
            customers_file = os.path.join(config.BASE, "alerts.json")
            if os.path.isfile(args.config if args.config != CONFIG_PATH else customers_file):
                print("등록된 가입 고객이 없어 alerts.json으로 실행합니다.")

    customers = customers_from_db() if source == "db" else load_config(args.config)
    if args.customer:
        customers = [c for c in customers if c["id"] == args.customer]
        if not customers:
            print(f"고객 id를 찾을 수 없습니다: {args.customer}", file=sys.stderr)
            return 2
    elif not args.all:
        customers = [c for c in customers if c.get("enabled")]

    if args.test:
        rc = 0
        for customer in customers:
            url = webhook_of(customer)
            if not notify.is_webhook(url):
                print(f"[{customer['id']}] 웹훅 미설정", file=sys.stderr)
                rc = 1
                continue
            ok, detail = notify.send_discord_to(
                url, f"**BidRadar 알림 테스트** · {customer['name']}\n"
                     f"조건: {condition_label(customer)}\n"
                     f"이 채널로 마감 D-3 / D-1 알림이 발송됩니다.")
            print(f"[{customer['id']}] test → {detail}")
            rc = rc or (0 if ok else 1)
        return rc

    stages = [args.stage] if args.stage else list(STAGES)
    now = time.time()
    total = 0
    for customer in customers:
        if not customer["enabled"]:
            print(f"[{customer['id']}] 비활성 → 건너뜀")
            continue
        customer_stages = [s for s in stages if s in customer["stages"]]
        total += run_customer(customer, customer_stages, now, args.limit,
                              dry_run=not args.send, site=site)
    print(f"완료: {total}건 발송" if args.send else f"미리보기 완료: {total}건 발송 예정")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
