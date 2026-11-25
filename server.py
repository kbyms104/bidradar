#!/usr/bin/env python3
"""BidRadar — 대시보드 + 랜딩 서버.

Serves the dashboard and landing page plus a tiny JSON API over SQLite.
Standard library only.

데이터 소스는 두 가지입니다.
    BIDRADAR_SOURCE=demo  seed.py가 만든 가상 공고 (기본, 2일마다 자동 재생성)
    BIDRADAR_SOURCE=live  collector.py가 저장한 나라장터 실데이터 (data/live.db)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import math
import mimetypes
import re
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import accounts
import alerts as alert_digest
import config
import quota
import traffic
import notify
import seed

config.load_env()

BASE = config.BASE
STATIC = os.path.join(BASE, "static")
SOURCE = config.source()
DB_PATH = (os.environ.get("BIDRADAR_DB")
           or (config.LIVE_DB if SOURCE == "live" else config.DEMO_DB))
LEADS_DB = config.LEADS_DB
HOST = os.environ.get("BIDRADAR_HOST", "127.0.0.1")
PORT = int(os.environ.get("BIDRADAR_PORT", "8790"))
KST = timezone(timedelta(hours=9))
LEAD_WINDOW = 600
LEAD_LIMIT = 5
STATIC_EXT = (".html", ".js", ".css", ".svg", ".webmanifest",
              ".png", ".jpg", ".jpeg", ".webp", ".ico")

_cache_lock = threading.Lock()
_cache: tuple[float, bytes] | None = None
_seed_checked = 0.0
_lead_lock = threading.Lock()
_lead_hits: dict[str, list[float]] = {}

CATEGORIES = [
    "소프트웨어/SI", "건설/토목", "시설/유지관리", "물품/구매",
    "용역/컨설팅", "전기/통신", "의료/바이오",
]
REGIONS = list(seed.AGENCIES.keys())
SORTS = {"close": "close_at ASC", "posted": "posted_at DESC", "amount": "amount DESC"}
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def rows(con: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def ensure_data() -> None:
    """데모 모드에서만 가상 데이터를 생성/갱신합니다.

    live 모드에서는 collector.py가 채운 data/live.db를 그대로 사용합니다.
    """
    global _seed_checked
    if SOURCE != "demo":
        return
    now = time.time()
    if now - _seed_checked < 3600:
        return
    seed.seed()
    _seed_checked = now


def ensure_schema() -> None:
    """기존 DB에 새 컬럼(url)을 추가합니다. (읽기 전용 서버와 별도 연결)"""
    if not os.path.exists(DB_PATH):
        return
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        con.executescript(seed.SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(bids)")}
        if "url" not in cols:
            con.execute("ALTER TABLE bids ADD COLUMN url TEXT NOT NULL DEFAULT ''")
        con.commit()
    finally:
        con.close()


def ensure_live_db() -> None:
    """live 모드에서 DB가 아직 없으면 빈 스키마만 만들어 둡니다."""
    if SOURCE != "live" or os.path.exists(DB_PATH):
        return
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        con.executescript(seed.SCHEMA)
        con.commit()
    finally:
        con.close()


def last_sync() -> int:
    """마지막 수집 시각. live 모드에서는 meta 테이블 값을 사용합니다."""
    if SOURCE == "demo":
        return int(time.time()) - 95
    try:
        con = db()
        row = con.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        con.close()
        if row:
            return int(row[0])
    except (sqlite3.Error, ValueError):
        pass
    try:
        con = db()
        row = con.execute("SELECT MAX(posted_at) FROM bids").fetchone()
        con.close()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def bid_brief(row: dict) -> dict:
    return {
        "id": row["id"],
        "notice_no": row["notice_no"],
        "title": row["title"],
        "org": row["org"],
        "region": row["region"],
        "category": row["category"],
        "amount": row["amount"],
        "posted_at": row["posted_at"],
        "close_at": row["close_at"],
        "bid_type": row["bid_type"],
    }


def summary(scope: tuple[str, list] | None = None, account_id: int | None = None) -> dict:
    now = int(time.time())
    day = 86400
    con = db()
    sw, sa = scope if scope else ("1=1", [])

    new24 = con.execute(
        f"SELECT COUNT(*) FROM bids WHERE posted_at>=? AND {sw}", [now - day] + sa
    ).fetchone()[0]
    prev24 = con.execute(
        f"SELECT COUNT(*) FROM bids WHERE posted_at>=? AND posted_at<? AND {sw}",
        [now - 2 * day, now - day] + sa,
    ).fetchone()[0]
    closing3 = con.execute(
        f"SELECT COUNT(*) FROM bids WHERE close_at>=? AND close_at<=? AND {sw}",
        [now, now + 3 * day] + sa,
    ).fetchone()[0]
    week_close = con.execute(
        f"SELECT COUNT(*) FROM bids WHERE close_at>=? AND close_at<=? AND {sw}",
        [now, now + 7 * day] + sa,
    ).fetchone()[0]
    open_amount = con.execute(
        f"SELECT COALESCE(SUM(amount),0) FROM bids WHERE close_at>=? AND {sw}",
        [now] + sa,
    ).fetchone()[0]

    urgent = rows(
        con,
        f"SELECT * FROM bids WHERE close_at>=? AND {sw} ORDER BY close_at ASC LIMIT 5",
        [now] + sa,
    )

    categories = rows(
        con,
        f"SELECT category AS name, COUNT(*) AS n, COALESCE(SUM(amount),0) AS amount "
        f"FROM bids WHERE close_at>=? AND {sw} GROUP BY category ORDER BY n DESC",
        [now] + sa,
    )
    regions = rows(
        con,
        f"SELECT region AS name, COUNT(*) AS n FROM bids WHERE close_at>=? AND {sw} "
        "GROUP BY region ORDER BY n DESC LIMIT 7",
        [now] + sa,
    )

    today = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    weekly = []
    for i in range(7):
        start = int((today + timedelta(days=i)).timestamp())
        end = start + day
        row = con.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS amount "
            f"FROM bids WHERE close_at>=? AND close_at<? AND {sw}",
            [start, end] + sa,
        ).fetchone()
        weekly.append({
            "label": WEEKDAYS[(today + timedelta(days=i)).weekday()],
            "date": (today + timedelta(days=i)).strftime("%m.%d"),
            "n": row["n"],
            "amount": row["amount"],
            "today": i == 0,
        })
    con.close()

    # 알림 패널은 실제 발송 엔진(alerts.py)과 같은 조건/문구로 만듭니다.
    try:
        if account_id:
            alerts = alert_digest.preview_for(account_id, limit=3)
        else:
            alerts = alert_digest.preview(limit=3)
    except Exception:
        alerts = []
    if not alerts:
        alerts = [{
            "tone": "summary",
            "title": f"오늘 마감 임박 {closing3}건",
            "body": f"이번 주 마감 {week_close}건 · 신규 {new24}건을 확인하세요.",
            "time": "08:30",
        }]

    bid_alerts = os.environ.get("BIDRADAR_BID_ALERTS") == "1"
    if account_id:
        owner = accounts.get_account(account_id)
        bid_alerts = bool(
            bid_alerts and owner and notify.is_webhook(owner["webhook"] or "")
            and accounts.has_success(account_id)
        )

    return {
        "now": now,
        "kpis": {
            "new24": new24,
            "new_delta": new24 - prev24,
            "closing3": closing3,
            "week_close": week_close,
            "open_amount": open_amount,
        },
        "urgent": [bid_brief(r) for r in urgent],
        "categories": categories,
        "regions": regions,
        "weekly": weekly,
        "alerts": alerts,
        "last_sync": last_sync(),
        "source": SOURCE,
        "telegram": notify.telegram_ready(),
        "notify": notify.channel(),
        "bid_alerts": bid_alerts,
        "scoped": scope is not None,
    }


def list_bids(params: dict, scope: tuple[str, list] | None = None) -> dict:
    now = int(time.time())
    q = (params.get("q") or [""])[0].strip()[:60]
    region = (params.get("region") or [""])[0].strip()
    category = (params.get("category") or [""])[0].strip()
    try:
        min_amount = max(0, int((params.get("min_amount") or ["0"])[0]))
        days = int((params.get("days") or ["0"])[0])
        offset = max(0, int((params.get("offset") or ["0"])[0]))
        limit = max(1, min(60, int((params.get("limit") or ["15"])[0])))
    except ValueError as exc:
        raise ValueError("숫자 파라미터 형식이 올바르지 않습니다") from exc
    sort = (params.get("sort") or ["close"])[0]
    order = SORTS.get(sort, SORTS["close"])

    where = ["close_at>=?", "amount>=?"]
    args: list[object] = [now, min_amount]
    if scope:
        where.append(scope[0])
        args += scope[1]
    if q:
        like = f"%{q}%"
        where.append("(title LIKE ? OR org LIKE ? OR notice_no LIKE ? OR category LIKE ?)")
        args += [like, like, like, like]
    if region and region in REGIONS:
        where.append("region=?")
        args.append(region)
    if category and category in CATEGORIES:
        where.append("category=?")
        args.append(category)
    if days > 0:
        where.append("close_at<=?")
        args.append(now + days * 86400)

    clause = " AND ".join(where)
    con = db()
    total = con.execute(f"SELECT COUNT(*) FROM bids WHERE {clause}", args).fetchone()[0]
    items = rows(
        con,
        f"SELECT * FROM bids WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        args + [limit, offset],
    )
    con.close()

    return {
        "now": now,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [bid_brief(r) for r in items],
        "filters": {
            "q": q, "region": region, "category": category,
            "min_amount": min_amount, "days": days, "sort": sort,
        },
    }


def bid_detail(bid_id: str) -> dict | None:
    con = db()
    found = con.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    con.close()
    if not found:
        return None
    row = dict(found)
    for key in ("qualifications", "attachments"):
        try:
            row[key] = json.loads(row[key] or "[]")
        except json.JSONDecodeError:
            row[key] = []
    return row


LEAD_DDL = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    company TEXT NOT NULL,
    name TEXT NOT NULL,
    contact TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
)
"""


def lead_rate_ok(ip: str) -> bool:
    now = time.time()
    with _lead_lock:
        hits = [t for t in _lead_hits.get(ip, []) if now - t < LEAD_WINDOW]
        if len(hits) >= LEAD_LIMIT:
            _lead_hits[ip] = hits
            return False
        hits.append(now)
        _lead_hits[ip] = hits
        return True


def clean_field(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def save_lead(payload: dict, ip: str, user_agent: str) -> int:
    os.makedirs(os.path.dirname(LEADS_DB), exist_ok=True)
    con = sqlite3.connect(LEADS_DB, timeout=10)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(LEAD_DDL)
        cur = con.execute(
            "INSERT INTO leads (created_at, company, name, contact, keywords, method, message, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                clean_field(payload.get("company"), 80),
                clean_field(payload.get("name"), 40),
                clean_field(payload.get("contact"), 120),
                clean_field(payload.get("keywords"), 200),
                clean_field(payload.get("method"), 40),
                clean_field(payload.get("message"), 1000),
                ip[:64],
                user_agent[:300],
            ),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


CLIENT_LOG = os.path.join(BASE, "logs", "client.log")
_client_hits: dict[str, list[float]] = {}
_client_lock = threading.Lock()


def mask_ip(ip: str) -> str:
    """로그에는 마지막 옥텟을 가려서 남깁니다."""
    parts = (ip or "").split(".")
    if len(parts) == 4:
        return ".".join(parts[:3] + ["x"])
    return ip[:64]


def client_rate_ok(ip: str) -> bool:
    now = time.time()
    with _client_lock:
        if len(_client_hits) > 5000:
            _client_hits.clear()
        hits = [t for t in _client_hits.get(ip, []) if now - t < 60]
        if len(hits) >= 30:
            _client_hits[ip] = hits
            return False
        hits.append(now)
        _client_hits[ip] = hits
        return True


def write_client_log(ip: str, payload: dict) -> None:
    """브라우저에서 올라온 오류/진단을 JSONL로 남깁니다."""
    os.makedirs(os.path.dirname(CLIENT_LOG), exist_ok=True)
    entry = {
        "ts": int(time.time()),
        "ip": mask_ip(ip),
        "kind": clean_field(payload.get("kind"), 32),
        "path": clean_field(payload.get("path"), 120),
        "message": clean_field(payload.get("message"), 400),
        "src": clean_field(payload.get("src"), 200),
        "stack": clean_field(payload.get("stack"), 1500),
        "href": clean_field(payload.get("href"), 200),
        "ref": clean_field(payload.get("ref"), 200),
        "ua": clean_field(payload.get("ua"), 300),
    }
    extra = payload.get("extra")
    if isinstance(extra, dict):
        def _s(value, limit: int) -> str:
            if value is None:
                return ""
            if isinstance(value, bool):
                return "true" if value else "false"
            return " ".join(str(value).split())[:limit]

        entry["extra"] = {_s(k, 40): _s(v, 400) for k, v in list(extra.items())[:15]}
        # 브라우저 핸들러는 부가 정보로 보내므로 대표 필드로 끌어올립니다.
        for key, limit in (("message", 400), ("src", 200), ("stack", 1500), ("href", 200)):
            if not entry[key] and entry["extra"].get(key):
                entry[key] = entry["extra"][key]
    try:
        if os.path.getsize(CLIENT_LOG) > 5 * 1024 * 1024:
            os.replace(CLIENT_LOG, CLIENT_LOG + ".1")
    except OSError:
        pass
    with open(CLIENT_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def tail_client_log(limit: int = 20) -> list[dict]:
    if not os.path.isfile(CLIENT_LOG):
        return []
    with open(CLIENT_LOG, encoding="utf-8") as fh:
        lines = fh.readlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build_summary_cache() -> bytes:
    return json.dumps(summary(), ensure_ascii=False, separators=(",", ":")).encode()


def cached_summary() -> bytes:
    global _cache
    with _cache_lock:
        if _cache and time.time() - _cache[0] < 15:
            return _cache[1]
        ensure_data()
        body = build_summary_cache()
        _cache = (time.time(), body)
        return body


# --------------------------------------------------------------- 고객 계정
COOKIE_NAME = "br_session"
_login_hits: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def parse_cookies(value: str) -> dict[str, str]:
    out = {}
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def session_token(handler) -> str:
    return parse_cookies(handler.headers.get("Cookie") or "").get(COOKIE_NAME, "")


def current_account(handler) -> sqlite3.Row | None:
    try:
        return accounts.account_by_token(session_token(handler))
    except sqlite3.Error:
        return None


def cookie_header(token: str | None, secure: bool) -> str:
    if token is None:
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    days = config.session_days()
    parts = [f"{COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={days * 86400}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def ip_hint(handler) -> str:
    return handler.headers.get("X-Real-IP") or handler.client_address[0]


def is_https(handler) -> bool:
    proto = (handler.headers.get("X-Forwarded-Proto") or "").lower()
    return proto == "https" or "duckdns.org" in (handler.headers.get("Host") or "")


def login_rate_ok(ip: str) -> bool:
    now = time.time()
    with _login_lock:
        hits = [t for t in _login_hits.get(ip, []) if now - t < 300]
        if len(hits) >= 20:
            _login_hits[ip] = hits
            return False
        hits.append(now)
        _login_hits[ip] = hits
        return True


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def condition_sql(cond: dict) -> tuple[str, list]:
    """알림 조건 하나를 SQL WHERE 조각으로 바꿉니다."""
    parts: list[str] = []
    args: list[object] = []
    if cond.get("keywords"):
        ors = []
        for kw in cond["keywords"]:
            ors.append("(title LIKE ? ESCAPE '\\' OR org LIKE ? ESCAPE '\\')")
            args += [_like(kw), _like(kw)]
        parts.append("(" + " OR ".join(ors) + ")")
    for kw in cond.get("exclude_keywords") or []:
        parts.append("(title NOT LIKE ? ESCAPE '\\' AND org NOT LIKE ? ESCAPE '\\')")
        args += [_like(kw), _like(kw)]
    if cond.get("regions"):
        names = list(dict.fromkeys(list(cond["regions"]) + ["전국"]))
        parts.append("region IN (" + ",".join("?" * len(names)) + ")")
        args += names
    if cond.get("categories"):
        cats = list(cond["categories"])
        parts.append("category IN (" + ",".join("?" * len(cats)) + ")")
        args += cats
    if int(cond.get("min_amount") or 0) > 0:
        parts.append("amount >= ?")
        args.append(int(cond["min_amount"]))
    return (" AND ".join(parts) or "1=1"), args


def scope_for(account_id: int) -> tuple[str, list] | None:
    """그 고객의 모든 조건을 합친 범위. 조건이 없으면 None(전체 공고)."""
    conds = accounts.conditions_for(account_id, enabled_only=True)
    if not conds:
        return None
    sqls, args = [], []
    for cond in conds:
        sql, add = condition_sql(cond)
        sqls.append(f"({sql})")
        args += add
    return "(" + " OR ".join(sqls) + ")", args


# --------------------------------------------------------------- 서버 렌더링
# 데모는 원래 브라우저 JS가 API를 불러와 목록을 그립니다. 그런데 JS가 실행되지
# 않는 환경(스크립트 차단·인앱 브라우저·구형 웹뷰)에서는 회색 스켈레톤만 남아
# "데이터가 없다"로 보입니다. 그래서 첫 화면(15건·KPI·알림)을 서버에서 직접
# 그려 넣습니다. JS가 정상 동작하면 app.js가 곧바로 같은 내용으로 덮어씁니다.

DISCORD_LOGO = (
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M19.7 5.4A17.3 17.3 0 0 0 '
    '15.4 4l-.2.4a16 16 0 0 1 3.8 1.9 15.6 15.6 0 0 0-13.9 0A16 16 0 0 1 8.9 4.4L8.6 4a17.3 17.3 0 '
    '0 0-4.3 1.4C1.5 9.6.8 13.7 1.1 17.7a17.4 17.4 0 0 0 5.2 2.6l1.1-1.8c-.6-.2-1.2-.5-1.7-.9l.4-.3a'
    '12.4 12.4 0 0 0 11.8 0l.4.3c-.5.4-1.1.7-1.7.9l1.1 1.8a17.4 17.4 0 0 0 5.2-2.6c.4-4.6-.8-8.7-'
    '3.2-12.3ZM8.7 15.2c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Zm6.6 0c-1 0-1.8-.9-'
    '1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Z" fill="currentColor"/></svg>'
)


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _won(value) -> str:
    n = int(value or 0)
    if n <= 0:
        return "미정"
    if n >= 1_0000_0000_0000:
        return f"{n / 1_0000_0000_0000:.2f}".rstrip("0").rstrip(".") + "조"
    if n >= 1_0000_0000:
        eok = n / 1_0000_0000
        return (f"{eok:.0f}" if eok >= 10 else f"{eok:.1f}".rstrip("0").rstrip(".")) + "억"
    if n >= 1_0000:
        return f"{round(n / 1_0000):,}만"
    return f"{n:,}"


def _dday(close_at: int, now: float) -> tuple[str, str, str]:
    if close_at <= now:
        return "마감", "종료", ""
    days = math.ceil((close_at - now) / 86400)
    if days <= 3:
        return f"D-{days}", "마감 임박", "urgent"
    if days <= 7:
        return f"D-{days}", "이번 주", "soon"
    return f"D-{days}", "접수 중", ""


def _ago(ts: int, now: float) -> str:
    diff = max(0, int(now - ts))
    if diff < 3600:
        return f"{max(1, diff // 60)}분 전"
    if diff < 86400:
        return f"{diff // 3600}시간 전"
    return f"{diff // 86400}일 전"


def _bid_card(item: dict, now: float) -> str:
    label, sub, cls = _dday(int(item["close_at"]), now)
    close = datetime.fromtimestamp(int(item["close_at"]), KST)
    kind = item.get("bid_type") or ""
    kind_cls = "type-urgent" if kind == "긴급공고" else "type-re" if kind == "재공고" else "type-normal"
    tags = []
    if now - int(item["posted_at"]) < 86400:
        tags.append('<span class="tag">신규</span>')
    if int(item["amount"] or 0) >= 1e9:
        tags.append('<span class="tag">대형사업</span>')
    if cls == "urgent":
        tags.append('<span class="tag">마감임박</span>')
    tag_html = f'<div class="bid-tags">{"".join(tags)}</div>' if tags else ""
    return (
        f'<article class="bid" data-id="{_esc(item["id"])}" tabindex="0" role="button" '
        f'aria-label="{_esc(item["title"])} 상세 보기">'
        f'<div class="dday {cls}"><b>{label}</b><span>{sub}</span></div>'
        f'<div class="bid-body"><div class="bid-badges">'
        f'<span class="badge {kind_cls}">{_esc(kind)}</span>'
        f'<span class="badge">{_esc(item["region"])}</span>'
        f'<span class="badge">{_esc(item["category"])}</span></div>'
        f'<h3>{_esc(item["title"])}</h3>'
        f'<div class="bid-meta"><b>{_esc(item["org"])}</b><span class="sep">·</span>'
        f'{_esc(item["notice_no"])}<span class="sep">·</span>'
        f'{_ago(int(item["posted_at"]), now)} 등록</div>{tag_html}</div>'
        f'<div class="bid-right"><div class="bid-amount">{_won(item["amount"])}</div>'
        f'<div class="bid-close">{close:%m.%d %H:%M} 마감</div></div>'
        f'</article>'
    )


def _alert_msgs(alerts: list[dict]) -> str:
    if not alerts:
        return '<div class="tg-empty">표시할 알림이 없습니다.</div>'
    return "".join(
        f'<div class="tg-msg {_esc(a.get("tone", "normal"))}">'
        f'<div class="tg-msg-title">{_esc(a.get("title"))}</div>'
        f'<div class="tg-msg-body">{_esc(a.get("body"))}</div>'
        f'<div class="tg-msg-time">{_esc(a.get("time"))}</div></div>'
        for a in alerts
    )


def _json_body(raw: bytes) -> dict | None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _form_or_json(raw: bytes, content_type: str) -> dict | None:
    """JSON 또는 HTML form(urlencoded) 본문을 dict로 바꿉니다."""
    if "application/json" in content_type:
        return _json_body(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    payload = {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}
    return payload or None


def _public_account(account: sqlite3.Row) -> dict:
    return {
        "id": account["id"],
        "email": account["email"],
        "company": account["company"],
        "name": account["name"],
        "phone": account["phone"],
        "webhook": account["webhook"],
        "plan": account["plan"],
        "status": account["status"],
        "created_at": account["created_at"],
    }


def render_legal(name: str) -> bytes:
    """약관·방침 HTML의 {{CONTACT}} 자리를 문의 이메일(또는 상담 폼)로 바꿉니다."""
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        doc = fh.read()
    email = config.contact_email()
    if email:
        contact = f'<a href="mailto:{_esc(email)}">{_esc(email)}</a>'
    else:
        contact = '<a href="/#contact">사이트 내 상담 신청 폼</a>'
    return doc.replace("{{CONTACT}}", contact).encode("utf-8")


def render_admin_html() -> bytes:
    accounts_rows = accounts.list_accounts()
    invites = accounts.list_invites()

    def esc(v):
        return html.escape(str(v if v is not None else ""))

    rows = "".join(
        f"<tr><td>{a['id']}</td><td>{esc(a['email'])}</td><td>{esc(a['company'])}</td>"
        f"<td>{esc(a['plan'])}</td><td>{'사용' if a['webhook'] else '미설정'}</td>"
        f"<td>{a['n_conditions']}</td><td>{a['n_sent']}</td>"
        f"<td>{esc(datetime.fromtimestamp(a['created_at'], KST).strftime('%m-%d %H:%M'))}</td></tr>"
        for a in accounts_rows
    ) or '<tr><td colspan="8">계정 없음</td></tr>'

    invite_rows = "".join(
        f"<tr><td><code>{esc(i['code'])}</code></td><td>{esc(i['note'])}</td>"
        f"<td>{'사용됨' if i['used_by'] else '사용 가능'}</td></tr>"
        for i in invites
    ) or '<tr><td colspan="3">초대 코드 없음</td></tr>'

    q = quota.status()
    q_pct = min(100, q["percent"])
    q_color = "#dc2626" if q_pct >= 90 else ("#d97706" if q_pct >= 70 else "#16a34a")
    q_state = (
        f"차단됨 — {esc(q['blocked'])}"
        if q["blocked"]
        else ("절전: 유형별 1페이지" if q["throttled"] else "정상")
    )
    quota_html = (
        f'<section><h2>공공데이터 API 사용량 ({esc(q["date"])} KST)</h2>'
        f'<div style="display:flex;align-items:center;gap:12px;font-size:13px">'
        f'<div style="flex:1;height:10px;background:#f0f1f4;border-radius:6px;overflow:hidden">'
        f'<div style="width:{q_pct}%;height:100%;background:{q_color}"></div></div>'
        f'<b>{q["calls"]:,} / {q["budget"]:,}회 ({q["percent"]:.0f}%)</b></div>'
        f'<p class="sub" style="margin:8px 0 0">포털 한도 {q["limit"]:,}회 · 예비 {q["reserve"]:,}회 · '
        f'남음 {q["remaining"]:,}회 · {q_state}</p></section>'
    )

    try:
        t = traffic.scan(60)
    except Exception:
        t = {"requests": 0, "mb": 0, "unique_ips": 0, "errors": 0,
             "top_ip": [], "self_skipped": 0}
    t_top = ", ".join(f"{esc(ip)}({n:,})" for ip, n in t["top_ip"][:2]) or "—"
    traffic_html = (
        f'<section><h2>지난 1시간 트래픽</h2>'
        f'<p class="sub" style="margin:0 0 6px">요청 {t["requests"]:,}건 · 전송 {t["mb"]:,}MB · '
        f'고유 IP {t["unique_ips"]} · 4xx {t["errors"]:,}'
        + (f' · 자체 점검 {t["self_skipped"]:,}건 제외' if t["self_skipped"] else "")
        + f'</p><p class="sub" style="margin:0">상위 IP: {t_top}</p></section>'
    )

    doc = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BidRadar 관리자</title>
<style>
body{{font:14px/1.6 system-ui,-apple-system,sans-serif;margin:0;background:#f6f7fb;color:#111827}}
main{{max-width:1000px;margin:0 auto;padding:28px 18px 60px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#6b7280;font-size:13px;margin-bottom:22px}}
section{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:18px;margin-bottom:18px}}
h2{{font-size:15px;margin:0 0 12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #f0f1f4;white-space:nowrap}}
th{{color:#6b7280;font-weight:600;font-size:12px}}
code{{background:#f3f4f6;padding:2px 6px;border-radius:5px}}
form{{display:flex;gap:8px;margin-top:12px}}
input{{flex:1;padding:9px 12px;border:1px solid #d1d5db;border-radius:8px}}
button{{padding:9px 16px;border:0;border-radius:8px;background:#335cff;color:#fff;font-weight:700;cursor:pointer}}
</style></head><body><main>
<h1>BidRadar 관리자</h1>
<p class="sub">고객 계정 · 초대 코드 · 발송 현황</p>
{quota_html}
{traffic_html}
<section><h2>고객 계정 ({len(accounts_rows)})</h2>
<table><tr><th>ID</th><th>이메일</th><th>회사</th><th>플랜</th><th>웹훅</th><th>조건</th><th>발송</th><th>가입</th></tr>
{rows}</table></section>
<section><h2>초대 코드</h2>
<table><tr><th>코드</th><th>메모</th><th>상태</th></tr>{invite_rows}</table>
<form method="post" action="/admin/invite">
<input name="note" placeholder="메모 (예: 파일럿 A사)" maxlength="60">
<button type="submit">코드 발급</button></form>
</section>
</main></body></html>"""
    return doc.encode("utf-8")


def _customer_shell(doc: str, account: sqlite3.Row) -> str:
    """데모 화면을 고객 콘솔로 바꿉니다(내비게이션·계정 표기·부트스트랩)."""
    label = (account["company"] or account["email"] or "내 계정").strip()[:24]
    nav = (
        '<nav class="mainnav" aria-label="주요 메뉴">'
        '<a href="/app" class="active">대시보드</a>'
        '<a href="/app/settings" id="navAlerts">알림 설정</a>'
        '<a href="#weekly">주간 리포트</a>'
        '<a href="/app/settings#history">알림 이력</a>'
        '<a href="/#pricing" id="navPricing">요금제</a>'
        '</nav>'
    )
    doc = re.sub(r'<nav class="mainnav".*?</nav>', nav, doc, count=1, flags=re.S)
    doc = re.sub(
        r'<span class="demo-badge"[^>]*>[^<]*</span>',
        f'<span class="demo-badge" title="{_esc(account["email"])}">{_esc(label)}</span>',
        doc, count=1,
    )
    doc = doc.replace('<a class="brand" href="/demo"', '<a class="brand" href="/app"')

    initial = (account["name"] or account["company"] or account["email"] or "?").strip()[:1]
    doc = re.sub(
        r'<div class="avatar" title="[^"]*">[^<]*</div>',
        f'<div class="avatar" title="{_esc(account["name"] or account["company"] or account["email"])}">'
        f'{_esc(initial)}</div>',
        doc, count=1,
    )
    doc = re.sub(r'<title>[^<]*</title>', '<title>BidRadar — 고객 콘솔</title>', doc, count=1)
    doc = doc.replace(
        '우리 회사에 맞는 입찰 공고만 모아 마감 전에 알려주는 B2B 모니터링 대시보드 데모.',
        '내 조건에 맞는 나라장터 입찰 공고를 마감 전에 디스코드로 알려드립니다.',
    )
    doc = doc.replace(
        '데모에서는 브라우저에만 저장됩니다. 실제 서비스에서는 계정별로 서버에 저장됩니다.',
        '조건은 알림 설정 화면에서 서버에 저장됩니다.',
    )

    start = doc.find('<section class="card demo-card">')
    if start != -1:
        end = doc.find('</section>', start)
        if end != -1:
            doc = doc[:start] + doc[end + len('</section>'):]

    doc = doc.replace(
        '<span>BidRadar — 입찰 공고 모니터링 데모</span>',
        '<span>BidRadar — 고객 콘솔</span>',
    )

    if not accounts.conditions_for(account["id"], enabled_only=True):
        banner = (
            '<div style="max-width:1320px;margin:18px auto 0;padding:0 22px">'
            '<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;'
            'justify-content:space-between;padding:14px 16px;border:1px solid #c7d2fe;'
            'border-radius:12px;background:#eef2ff;color:#1e293b;font-size:13.5px">'
            '<span><b>아직 알림 조건이 없습니다.</b> 지금은 전체 공고가 보입니다. '
            '키워드·지역을 저장하면 내 공고만 남습니다.</span>'
            '<a href="/app/settings" style="flex:none;padding:8px 14px;border-radius:8px;'
            'background:#3157e8;color:#fff;font-weight:700;text-decoration:none">알림 설정 →</a>'
            '</div></div>'
        )
        doc = doc.replace('<main class="shell">', banner + '<main class="shell">', 1)

    bootstrap = (
        '<script>window.BIDRADAR_API="/api/app";'
        'window.BIDRADAR_USER=' + json.dumps(
            {"email": account["email"], "company": account["company"],
             "plan": account["plan"]}, ensure_ascii=False
        ).replace("<", "\\u003c") + ';</script>\n'
    )
    doc = doc.replace('<script src="/app.js?v=20261113f" defer></script>',
                      bootstrap + '<script src="/app.js?v=20261113f" defer></script>')
    return doc


def render_demo_html(account: sqlite3.Row | None = None) -> bytes:
    """JS 없이도 첫 화면이 보이도록 HTML에 실제 데이터를 미리 심습니다.

    account가 있으면 그 고객의 조건 범위로만 조회하고, 로그인 콘솔용
    내비게이션·계정 표기를 주입합니다.
    """
    scope = scope_for(account["id"]) if account else None
    data = summary(scope, account["id"] if account else None)
    listing = list_bids({"limit": ["15"], "sort": ["close"]}, scope)
    now = data["now"]
    k = data["kpis"]

    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
        doc = fh.read()

    # 공고 목록 (스켈레톤 → 실제 카드)
    skeleton = ('<div class="bid-list" id="bidList">'
                '<div class="skeleton-row"></div><div class="skeleton-row"></div>'
                '<div class="skeleton-row"></div></div>')
    cards = "".join(_bid_card(item, now) for item in listing["items"])
    empty = '<div class="empty-state"><b>조건에 맞는 공고가 없습니다</b><span>잠시 후 다시 시도해 주세요.</span></div>'
    skeleton_block = (
        '<div class="bid-list" id="bidList">\n'
        '        <div class="skeleton-row"></div>\n'
        '        <div class="skeleton-row"></div>\n'
        '        <div class="skeleton-row"></div>\n'
        '      </div>'
    )
    doc = doc.replace(
        skeleton_block,
        f'<div class="bid-list" id="bidList">{cards or empty}</div>',
    )

    # KPI
    doc = doc.replace('id="kpiNew">—<', f'id="kpiNew">{k["new24"]:,}건<')
    delta = k["new_delta"]
    doc = doc.replace('id="kpiDelta">—<', f'id="kpiDelta">{"+" if delta >= 0 else ""}{delta:,}건<')
    doc = doc.replace('id="kpiClosing">—<', f'id="kpiClosing">{k["closing3"]:,}건<')
    doc = doc.replace('id="kpiWeek">—<', f'id="kpiWeek">{k["week_close"]:,}건<')
    week_amount = sum(int(d.get("amount") or 0) for d in data.get("weekly", []))
    doc = doc.replace('id="kpiWeekAmount">—<', f'id="kpiWeekAmount">마감 예정 {_won(week_amount)} 규모<')
    doc = doc.replace('id="kpiAmount">—<', f'id="kpiAmount">{_won(k["open_amount"])}<')

    # 동기화 시각 / 결과 수
    stamp = datetime.fromtimestamp(int(data.get("last_sync") or now), KST)
    doc = doc.replace('id="lastSync">--:--<', f'id="lastSync">{stamp:%H:%M}<')
    doc = doc.replace('id="resultCount">0<', f'id="resultCount">{listing["total"]:,}<')

    # 데이터 출처 카드
    con = db()
    try:
        total = con.execute("SELECT COUNT(*) FROM bids").fetchone()[0]
        regions = con.execute("SELECT COUNT(DISTINCT region) FROM bids").fetchone()[0]
    finally:
        con.close()
    if SOURCE == "live":
        label, cycle = "실데이터", "15분"
        note = "나라장터 입찰공고정보서비스에서 15분마다 수집한 실제 공고입니다. 금액·마감은 원문에서 최종 확인하세요."
        doc = doc.replace(
            '<span class="demo-badge" title="포트폴리오용 데모 데이터">DEMO</span>',
            '<span class="demo-badge" title="나라장터에서 수집한 실제 공고">LIVE</span>',
        )
        doc = doc.replace('BidRadar — 입찰 공고 모니터링 데모',
                          'BidRadar — 입찰 공고 모니터링 (실시간 수집)')
        doc = doc.replace('<title>BidRadar — 입찰 공고 모니터링</title>',
                          '<title>BidRadar — 실시간 입찰 공고</title>')
    else:
        label, cycle = "데모 데이터", "5분"
        note = "이 화면의 공고는 모두 데모용 가상 데이터입니다. 실제 서비스는 나라장터 OpenAPI와 연동합니다."
    doc = doc.replace('id="srcLabel">데이터 출처<', f'id="srcLabel">{label}<')
    doc = doc.replace('id="srcNote">나라장터 입찰공고정보서비스에서 수집한 공고입니다.<',
                      f'id="srcNote">{note}<')
    doc = doc.replace('id="srcBids">—<', f'id="srcBids">{total:,}<')
    doc = doc.replace('id="srcRegions">—<', f'id="srcRegions">{regions}<')
    doc = doc.replace('id="srcCycle">—<', f'id="srcCycle">{cycle}<')

    # 알림 패널
    channel = data.get("notify") or ("telegram" if data.get("telegram") else "")
    if channel == "discord":
        doc = doc.replace('id="alertTitle">텔레그램 알림<', 'id="alertTitle">디스코드 알림<')
        doc = doc.replace(
            '<span class="tg-logo" id="alertLogo" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none">'
            '<path d="m21 4-3.2 15.2c-.2 1-.8 1.2-1.6.8l-4.5-3.3-2.2 2.1c-.2.2-.5.4-.9.4l.3-4.6 8.4-7.6c.4-.3-.1-.5-.6-.2L7.3 12.4 2.9 11c-1-.3-1-1 .2-1.4L19.8 3c.8-.3 1.4.2 1.2 1Z" fill="currentColor"/></svg></span>',
            f'<span class="tg-logo" id="alertLogo" aria-hidden="true">{DISCORD_LOGO}</span>',
        )
    live = bool(data.get("bid_alerts"))
    if account is not None:
        live = (live and notify.is_webhook(account["webhook"] or "")
                and accounts.has_success(account["id"]))
    if not live:
        doc = doc.replace('<span class="live-dot"><i></i>LIVE</span>',
                          '<span class="live-dot"><i></i>PREVIEW</span>')
        doc = doc.replace('id="alertSubtitle">마감 D-3 · D-1 자동 발송<',
                          'id="alertSubtitle">공고 알림 예시 · D-3 / D-1<')
    feed_block = (
        '<div class="phone" id="telegramFeed">\n'
        '          <div class="tg-empty">알림을 불러오는 중…</div>\n'
        '        </div>'
    )
    doc = doc.replace(
        feed_block,
        f'<div class="phone" id="telegramFeed">{_alert_msgs(data.get("alerts") or [])}</div>',
    )

    if account is not None:
        doc = _customer_shell(doc, account)

    # JS가 꺼져 있어도 목록은 보인다는 안내
    doc = doc.replace('</body>',
                      '<noscript><div style="position:fixed;left:12px;right:12px;bottom:12px;z-index:99;'
                      'padding:10px 14px;border-radius:10px;background:#111827;color:#fff;'
                      'font:13px/1.5 system-ui,sans-serif">자바스크립트가 꺼져 있어 필터·상세 보기가 '
                      '동작하지 않습니다. 목록은 서버에서 표시한 최신 공고입니다.</div></noscript></body>')
    return doc.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "BidRadar/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)

        if path in ("/", "/landing", "/landing.html"):
            return self.send_file(os.path.join(STATIC, "landing.html"))
        if path in ("/terms", "/terms.html", "/privacy", "/privacy.html"):
            name = "terms.html" if path.startswith("/terms") else "privacy.html"
            return self.send_bytes(render_legal(name), "text/html; charset=utf-8",
                                   cache="no-cache")
        if path == "/login":
            if current_account(self):
                return self.redirect("/app")
            return self.send_file(os.path.join(STATIC, "login.html"))
        if path in ("/app", "/app/"):
            account = current_account(self)
            if not account:
                return self.redirect("/login")
            try:
                return self.send_bytes(render_demo_html(account), "text/html; charset=utf-8",
                                       cache="no-store")
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=500)
        if path == "/app/settings":
            if not current_account(self):
                return self.redirect("/login")
            return self.send_file(os.path.join(STATIC, "settings.html"))
        if path == "/logout":
            accounts.delete_session(session_token(self))
            return self.redirect("/login", headers={
                "Set-Cookie": cookie_header(None, is_https(self))})
        if path == "/admin":
            if not self.admin_ok():
                return self.admin_challenge()
            return self.send_bytes(render_admin_html(), "text/html; charset=utf-8", cache="no-store")
        if path.startswith("/api/app/"):
            account = current_account(self)
            if not account:
                return self.send_json({"ok": False, "error": "로그인이 필요합니다."}, status=401)
            return self.handle_app_api(path, params, account)
        if path in ("/demo", "/demo/", "/demo/index.html", "/index.html"):
            try:
                return self.send_bytes(render_demo_html(), "text/html; charset=utf-8",
                                       cache="no-store")
            except Exception:
                return self.send_file(os.path.join(STATIC, "index.html"))
        if path == "/api/summary":
            try:
                return self.send_bytes(cached_summary(), "application/json; charset=utf-8")
            except Exception as exc:
                return self.send_json({"error": str(exc)}, status=500)
        if path == "/api/bids":
            try:
                return self.send_json(list_bids(params))
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, status=400)
        if path == "/api/bid":
            bid = bid_detail((params.get("id") or [""])[0])
            if not bid:
                return self.send_json({"error": "not found"}, status=404)
            return self.send_json(bid)
        if path == "/api/health":
            try:
                con = db()
                n = con.execute("SELECT COUNT(*) FROM bids").fetchone()[0]
                con.close()
                return self.send_json({
                    "ok": True,
                    "bids": n,
                    "source": SOURCE,
                    "last_sync": last_sync(),
                    "telegram": notify.telegram_ready(),
                    "notify": notify.channel(),
                    "bid_alerts": os.environ.get("BIDRADAR_BID_ALERTS") == "1",
                })
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=500)
        if path == "/favicon.ico":
            return self.send_file(os.path.join(STATIC, "favicon.svg"))
        if path.startswith("/static/"):
            path = path[len("/static/"):]
        if path.endswith(STATIC_EXT):
            target = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
            if not target.startswith(os.path.realpath(STATIC) + os.sep):
                return self.send_error(403)
            return self.send_file(target)
        return self.send_error(404)

    def do_HEAD(self) -> None:  # noqa: N802
        """같은 라우팅을 쓰되 본문만 생략합니다(모니터링/프리뷰 대응)."""
        self.do_GET()

    POST_PATHS = (
        "/api/lead", "/api/clientlog", "/api/login", "/api/signup", "/api/logout",
        "/api/app/conditions", "/api/app/conditions/delete", "/api/app/account",
        "/api/app/test", "/api/app/password", "/admin/invite",
    )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path not in self.POST_PATHS:
            return self.send_error(404)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > 40000:
            return self.send_json({"ok": False, "error": "요청 본문이 올바르지 않습니다."}, status=400)

        raw = self.rfile.read(length)
        ip = self.headers.get("X-Real-IP") or self.client_address[0]

        # CSRF: 브라우저가 붙인 Origin이 우리 호스트와 다르면 거부합니다.
        origin = self.headers.get("Origin") or ""
        if origin:
            host = self.headers.get("Host") or ""
            if urlparse(origin).netloc != host:
                return self.send_json({"ok": False, "error": "허용되지 않은 요청입니다."}, status=403)

        if path == "/api/clientlog":
            if not client_rate_ok(ip):
                return self.send_bytes(b"", "text/plain; charset=utf-8", status=429)
            try:
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return self.send_bytes(b"", "text/plain; charset=utf-8", status=400)
            try:
                write_client_log(ip, payload)
            except OSError:
                pass
            return self.send_bytes(b"", "text/plain; charset=utf-8", status=204)

        if path == "/api/login":
            return self.handle_login(raw, ip)
        if path == "/api/signup":
            return self.handle_signup(raw, ip)
        if path == "/api/logout":
            accounts.delete_session(session_token(self))
            return self.send_json({"ok": True}, headers={
                "Set-Cookie": cookie_header(None, is_https(self))})
        if path.startswith("/api/app/"):
            account = current_account(self)
            if not account:
                return self.send_json({"ok": False, "error": "로그인이 필요합니다."}, status=401)
            return self.handle_app_post(path, raw, account)
        if path == "/admin/invite":
            if not self.admin_ok():
                return self.admin_challenge()
            payload = _form_or_json(raw, self.headers.get("Content-Type") or "")
            if payload is None:
                return self.send_json({"ok": False, "error": "요청 형식 오류"}, status=400)
            accounts.create_invite(clean_field(payload.get("note"), 60))
            return self.redirect("/admin")

        # ---------------------------------------------------------- 상담 신청
        if not lead_rate_ok(ip):
            return self.send_json(
                {"ok": False, "error": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."},
                status=429,
            )
        payload = _json_body(raw)
        if payload is None:
            return self.send_json({"ok": False, "error": "요청 본문이 올바르지 않습니다."}, status=400)

        if clean_field(payload.get("website"), 10):
            return self.send_json({"ok": True, "id": 0})

        missing = [
            label for key, label in
            (("company", "회사명"), ("name", "담당자"), ("contact", "연락처"))
            if len(clean_field(payload.get(key), 120)) < 2
        ]
        if missing:
            return self.send_json(
                {"ok": False, "error": f"{', '.join(missing)}을(를) 입력해 주세요."},
                status=400,
            )

        try:
            lead_id = save_lead(payload, ip, self.headers.get("User-Agent") or "")
        except sqlite3.Error as exc:
            return self.send_json({"ok": False, "error": f"저장 실패: {exc}"}, status=500)

        lead = {
            "company": clean_field(payload.get("company"), 80),
            "name": clean_field(payload.get("name"), 40),
            "contact": clean_field(payload.get("contact"), 120),
            "keywords": clean_field(payload.get("keywords"), 200),
            "method": clean_field(payload.get("method"), 40),
            "message": clean_field(payload.get("message"), 1000),
        }
        threading.Thread(
            target=notify.notify_lead, args=(lead_id, lead), daemon=True
        ).start()
        return self.send_json({"ok": True, "id": lead_id})

    # -------------------------------------------------------------- 인증
    def handle_login(self, raw: bytes, ip: str) -> None:
        payload = _json_body(raw)
        if payload is None:
            return self.send_json({"ok": False, "error": "요청 형식 오류"}, status=400)
        if not login_rate_ok(ip):
            return self.send_json({"ok": False, "error": "시도가 너무 많습니다. 5분 후 다시 시도해 주세요."},
                                  status=429)
        account = accounts.authenticate(payload.get("email"), payload.get("password"))
        if not account:
            return self.send_json({"ok": False, "error": "이메일 또는 비밀번호가 맞지 않습니다."},
                                  status=401)
        token = accounts.create_session(
            account["id"], self.headers.get("User-Agent") or "", ip)
        return self.send_json({"ok": True, "next": "/app"}, headers={
            "Set-Cookie": cookie_header(token, is_https(self))})

    def handle_signup(self, raw: bytes, ip: str) -> None:
        payload = _json_body(raw)
        if payload is None:
            return self.send_json({"ok": False, "error": "요청 형식 오류"}, status=400)
        if not login_rate_ok(ip):
            return self.send_json({"ok": False, "error": "시도가 너무 많습니다. 5분 후 다시 시도해 주세요."},
                                  status=429)
        invite = (payload.get("invite") or "").strip()
        if config.signup_mode() == "invite":
            if not accounts.invite_valid(invite):
                return self.send_json({"ok": False, "error": "초대 코드가 올바르지 않습니다."}, status=403)
        try:
            account_id = accounts.create_account(
                payload.get("email"), payload.get("password") or "",
                clean_field(payload.get("company"), 80),
                clean_field(payload.get("name"), 40),
                clean_field(payload.get("phone"), 40),
            )
        except ValueError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, status=400)
        if invite:
            accounts.consume_invite(invite, account_id)
        token = accounts.create_session(account_id, self.headers.get("User-Agent") or "", ip)
        return self.send_json({"ok": True, "next": "/app/settings"}, headers={
            "Set-Cookie": cookie_header(token, is_https(self))})

    # -------------------------------------------------------------- 고객 API
    def handle_app_api(self, path: str, params: dict, account: sqlite3.Row) -> None:
        if path == "/api/app/summary":
            scope = scope_for(account["id"])
            data = summary(scope, account["id"])
            data["me"] = _public_account(account)
            return self.send_json(data)
        if path == "/api/app/bids":
            try:
                return self.send_json(list_bids(params, scope_for(account["id"])))
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, status=400)
        if path == "/api/app/bid":
            bid = bid_detail((params.get("id") or [""])[0])
            if not bid:
                return self.send_json({"error": "not found"}, status=404)
            return self.send_json(bid)
        if path == "/api/app/me":
            return self.send_json({
                "ok": True,
                "me": _public_account(account),
                "conditions": accounts.conditions_for(account["id"]),
                "history": [dict(r) for r in accounts.alert_history(account["id"], 40)],
                "webhook_ok": notify.is_webhook(account["webhook"] or ""),
                "options": {"categories": CATEGORIES, "regions": REGIONS},
            })
        if path == "/api/app/health":
            return self.send_json({"ok": True})
        return self.send_error(404)

    def handle_app_post(self, path: str, raw: bytes, account: sqlite3.Row) -> None:
        payload = _json_body(raw)
        if payload is None:
            return self.send_json({"ok": False, "error": "요청 형식 오류"}, status=400)

        if path == "/api/app/conditions":
            try:
                cid = accounts.save_condition(account["id"], payload)
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=400)
            return self.send_json({"ok": True, "id": cid,
                                   "conditions": accounts.conditions_for(account["id"])})
        if path == "/api/app/conditions/delete":
            accounts.delete_condition(account["id"], int(payload.get("id") or 0))
            return self.send_json({"ok": True,
                                   "conditions": accounts.conditions_for(account["id"])})
        if path == "/api/app/account":
            webhook = (payload.get("webhook") or "").strip()
            if webhook and not notify.is_webhook(webhook):
                return self.send_json({"ok": False, "error": "디스코드 웹훅 URL 형식이 아닙니다."},
                                      status=400)
            accounts.update_account(
                account["id"],
                company=clean_field(payload.get("company"), 80),
                name=clean_field(payload.get("name"), 40),
                phone=clean_field(payload.get("phone"), 40),
                webhook=webhook,
            )
            fresh = accounts.get_account(account["id"])
            return self.send_json({"ok": True, "me": _public_account(fresh)})
        if path == "/api/app/password":
            current = payload.get("current") or ""
            new = payload.get("new") or ""
            if len(new) < 8:
                return self.send_json({"ok": False, "error": "새 비밀번호는 8자 이상이어야 합니다."},
                                      status=400)
            try:
                accounts.change_password(account["id"], current, new)
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, status=400)
            accounts.delete_account_sessions(account["id"])
            token = accounts.create_session(
                account["id"], self.headers.get("User-Agent") or "", ip_hint(self))
            return self.send_json({"ok": True}, headers={
                "Set-Cookie": cookie_header(token, is_https(self))})
        if path == "/api/app/test":
            url = (payload.get("webhook") or account["webhook"] or "").strip()
            if not notify.is_webhook(url):
                return self.send_json({"ok": False, "error": "디스코드 웹훅 URL을 먼저 저장해 주세요."},
                                      status=400)
            conds = accounts.conditions_for(account["id"], enabled_only=True)
            sample = " / ".join(
                ((", ".join(c["keywords"][:3])) or (", ".join(c["regions"][:3])) or c["name"])
                for c in conds[:2]
            ) or "아직 조건 없음"
            text = (
                "**BidRadar 알림 테스트**\n"
                f"{account['company'] or account['email']} · 조건: {sample}\n"
                "이 채널로 마감 D-3 / D-1 알림이 발송됩니다."
            )
            ok, detail = notify.send_discord_to(url, text)
            if ok:
                accounts.log_alert(account["id"], 0, "웹훅 테스트", 0, 0, True, detail)
            return self.send_json({"ok": ok, "detail": detail})
        return self.send_error(404)

    # -------------------------------------------------------------- 관리자
    def admin_ok(self) -> bool:
        password = config.admin_password()
        if not password:
            return False
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, _, given = decoded.partition(":")
        return user in ("admin", "bidradar") and hmac.compare_digest(given, password)

    def admin_challenge(self) -> None:
        if not config.admin_password():
            return self.send_error(404)
        return self.send_bytes(
            b"\uc790\uaca9\uc99d\uba85\uc774 \ud544\uc694\ud569\ub2c8\ub2e4.",
            "text/plain; charset=utf-8", status=401,
            headers={"WWW-Authenticate": 'Basic realm="BidRadar Admin", charset="UTF-8"'})

    def redirect(self, location: str, headers: dict | None = None, status: int = 302) -> None:
        extra = {"Location": location}
        extra.update(headers or {})
        self.send_bytes(b"", "text/plain; charset=utf-8", status=status, headers=extra)

    def send_file(self, target: str) -> None:
        if not os.path.isfile(target):
            return self.send_error(404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            body = fh.read()
        cache = "no-cache" if target.endswith(".html") else "public, max-age=300"
        self.send_bytes(body, ctype, cache=cache)

    def send_json(self, payload: dict, status: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_bytes(body, "application/json; charset=utf-8", status=status, headers=headers)

    def send_bytes(self, body: bytes, ctype: str, status: int = 200,
                   cache: str = "no-store", headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    # 계정·상담 DB와 새로 생기는 파일을 소유자만 읽을 수 있게 합니다.
    os.umask(0o077)
    ensure_schema()
    ensure_live_db()
    ensure_data()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"BidRadar listening on http://{HOST}:{PORT} (source={SOURCE})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
