#!/usr/bin/env python3
"""고객 계정·로그인·알림 조건 저장소.

BidRadar를 파일럿이 아니라 **고객이 직접 쓰는 서비스**로 돌리기 위한 계층입니다.
표준 라이브러리만 사용합니다(SQLite + hashlib.scrypt + secrets).

저장 위치: `data/app.db` (환경변수 `BIDRADAR_APP_DB`로 변경 가능)

테이블
    accounts     고객 계정 (이메일, 비밀번호 해시, 회사명, 디스코드 웹훅)
    sessions     로그인 세션 (쿠키에는 원문 토큰, DB에는 SHA-256 해시)
    conditions   알림 조건 세트 (여러 개 가능)
    sent         발송 이력 (중복 발송 방지)
    alert_log    발송 결과 기록 (고객 화면 "알림 이력")
    invites      초대 코드
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    pw_salt       TEXT NOT NULL,
    pw_hash       TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    phone         TEXT NOT NULL DEFAULT '',
    webhook       TEXT NOT NULL DEFAULT '',
    plan          TEXT NOT NULL DEFAULT 'pilot',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ua         TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS conditions (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL,
    name             TEXT NOT NULL,
    keywords         TEXT NOT NULL DEFAULT '',
    exclude_keywords TEXT NOT NULL DEFAULT '',
    regions          TEXT NOT NULL DEFAULT '',
    categories       TEXT NOT NULL DEFAULT '',
    min_amount       INTEGER NOT NULL DEFAULT 0,
    stages           TEXT NOT NULL DEFAULT '3,1',
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sent (
    account_id   INTEGER NOT NULL,
    condition_id INTEGER NOT NULL,
    bid_id       TEXT NOT NULL,
    stage        INTEGER NOT NULL,
    sent_at      INTEGER NOT NULL,
    PRIMARY KEY (account_id, condition_id, bid_id, stage)
);

CREATE TABLE IF NOT EXISTS alert_log (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    condition_id INTEGER NOT NULL,
    condition_name TEXT NOT NULL DEFAULT '',
    stage        INTEGER NOT NULL,
    bid_count    INTEGER NOT NULL,
    ok           INTEGER NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    sent_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    used_by    INTEGER,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cond_account ON conditions(account_id);
CREATE INDEX IF NOT EXISTS idx_log_account ON alert_log(account_id, sent_at DESC);
"""

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


# ------------------------------------------------------------------ 기본
def connect() -> sqlite3.Connection:
    path = config.app_db()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def normalize_email(value: object) -> str:
    return str(value or "").strip().lower()[:120]


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email or ""))


def split_list(value: object, limit: int = 12) -> list[str]:
    """쉼표/줄바꿈으로 들어온 목록을 정리합니다."""
    if isinstance(value, list):
        raw = [str(v) for v in value]
    else:
        raw = re.split(r"[,\n]", str(value or ""))
    out = []
    for item in raw:
        item = " ".join(item.split()).strip()
        if item and item not in out:
            out.append(item[:40])
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ 비밀번호
def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("utf-8"),
        n=2 ** 14, r=8, p=1, dklen=32,
    ).hex()


def create_account(email: str, password: str, company: str = "", name: str = "",
                   phone: str = "", plan: str = "pilot") -> int:
    email = normalize_email(email)
    if not valid_email(email):
        raise ValueError("이메일 형식이 올바르지 않습니다.")
    if len(password or "") < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다.")
    salt = secrets.token_hex(16)
    con = connect()
    try:
        cur = con.execute(
            "INSERT INTO accounts (email, pw_salt, pw_hash, company, name, phone, plan, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (email, salt, _hash(password, salt),
             str(company or "")[:80], str(name or "")[:40],
             str(phone or "")[:40], str(plan or "pilot")[:20], int(time.time())),
        )
        con.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise ValueError("이미 가입된 이메일입니다.") from exc
    finally:
        con.close()


def authenticate(email: str, password: str) -> sqlite3.Row | None:
    email = normalize_email(email)
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM accounts WHERE email=? AND status='active'", (email,)
        ).fetchone()
        if not row:
            return None
        digest = _hash(password or "", row["pw_salt"])
        if not hmac.compare_digest(digest, row["pw_hash"]):
            return None
        con.execute("UPDATE accounts SET last_login_at=? WHERE id=?", (int(time.time()), row["id"]))
        con.commit()
        return con.execute("SELECT * FROM accounts WHERE id=?", (row["id"],)).fetchone()
    finally:
        con.close()


def change_password(account_id: int, current: str, new: str) -> None:
    """현재 비밀번호를 확인한 뒤 새 비밀번호로 바꿉니다."""
    con = connect()
    try:
        row = con.execute("SELECT pw_salt, pw_hash FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise ValueError("계정을 찾을 수 없습니다.")
        if not hmac.compare_digest(_hash(current or "", row["pw_salt"]), row["pw_hash"]):
            raise ValueError("현재 비밀번호가 맞지 않습니다.")
    finally:
        con.close()
    set_password(account_id, new)


def set_password(account_id: int, password: str) -> None:
    if len(password or "") < 8:
        raise ValueError("비밀번호는 8자 이상이어야 합니다.")
    salt = secrets.token_hex(16)
    con = connect()
    try:
        con.execute("UPDATE accounts SET pw_salt=?, pw_hash=? WHERE id=?",
                    (salt, _hash(password, salt), account_id))
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------ 세션
def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def create_session(account_id: int, ua: str = "", ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    con = connect()
    try:
        con.execute(
            "INSERT INTO sessions (token, account_id, created_at, expires_at, ua, ip) "
            "VALUES (?,?,?,?,?,?)",
            (_token_hash(token), account_id, now,
             now + config.session_days() * 86400, str(ua)[:200], str(ip)[:64]),
        )
        con.execute("DELETE FROM sessions WHERE expires_at<?", (now,))
        con.commit()
    finally:
        con.close()
    return token


def account_by_token(token: str) -> sqlite3.Row | None:
    if not token:
        return None
    con = connect()
    try:
        return con.execute(
            "SELECT a.* FROM sessions s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.token=? AND s.expires_at>? AND a.status='active'",
            (_token_hash(token), int(time.time())),
        ).fetchone()
    finally:
        con.close()


def delete_session(token: str) -> None:
    if not token:
        return
    con = connect()
    try:
        con.execute("DELETE FROM sessions WHERE token=?", (_token_hash(token),))
        con.commit()
    finally:
        con.close()


def delete_account_sessions(account_id: int) -> None:
    con = connect()
    try:
        con.execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------ 계정
def get_account(account_id: int) -> sqlite3.Row | None:
    con = connect()
    try:
        return con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    finally:
        con.close()


def list_accounts() -> list[sqlite3.Row]:
    con = connect()
    try:
        return con.execute(
            "SELECT a.*, "
            "(SELECT COUNT(*) FROM conditions c WHERE c.account_id=a.id) AS n_conditions, "
            "(SELECT COUNT(*) FROM sent s WHERE s.account_id=a.id) AS n_sent "
            "FROM accounts a ORDER BY a.id DESC"
        ).fetchall()
    finally:
        con.close()


def update_account(account_id: int, **fields) -> None:
    allowed = ("company", "name", "phone", "webhook", "plan", "status")
    sets, args = [], []
    for key in allowed:
        if key in fields:
            value = fields[key]
            if key == "status" and value not in ("active", "paused"):
                continue
            sets.append(f"{key}=?")
            args.append(str(value or "")[:300])
    if not sets:
        return
    con = connect()
    try:
        con.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", args + [account_id])
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------ 조건
def _cond_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "name": row["name"],
        "keywords": split_list(row["keywords"]),
        "exclude_keywords": split_list(row["exclude_keywords"]),
        "regions": split_list(row["regions"]),
        "categories": split_list(row["categories"]),
        "min_amount": int(row["min_amount"] or 0),
        "stages": [int(s) for s in (row["stages"] or "").split(",") if s.strip().isdigit()] or [3, 1],
        "enabled": bool(row["enabled"]),
    }


def conditions_for(account_id: int, enabled_only: bool = False) -> list[dict]:
    con = connect()
    try:
        sql = "SELECT * FROM conditions WHERE account_id=?"
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY id"
        return [_cond_row(r) for r in con.execute(sql, (account_id,))]
    finally:
        con.close()


def save_condition(account_id: int, payload: dict) -> int:
    name = " ".join(str(payload.get("name") or "").split())[:60] or "내 조건"
    keywords = ",".join(split_list(payload.get("keywords")))
    exclude = ",".join(split_list(payload.get("exclude_keywords")))
    regions = ",".join(split_list(payload.get("regions"), 20))
    categories = ",".join(split_list(payload.get("categories"), 20))
    try:
        min_amount = max(0, int(payload.get("min_amount") or 0))
    except (TypeError, ValueError):
        min_amount = 0
    stages = [s for s in (3, 1) if str(s) in [str(v) for v in (payload.get("stages") or [])]]
    stages_s = ",".join(str(s) for s in (stages or [3, 1]))
    enabled = 0 if payload.get("enabled") in (False, 0, "0", "false") else 1
    if not (keywords or regions or categories or min_amount):
        raise ValueError("키워드·지역·분야·최소 금액 중 하나는 입력해야 합니다.")

    con = connect()
    try:
        cid = payload.get("id")
        if cid:
            owned = con.execute(
                "SELECT id FROM conditions WHERE id=? AND account_id=?", (cid, account_id)
            ).fetchone()
            if not owned:
                raise ValueError("조건을 찾을 수 없습니다.")
            con.execute(
                "UPDATE conditions SET name=?, keywords=?, exclude_keywords=?, regions=?, "
                "categories=?, min_amount=?, stages=?, enabled=? WHERE id=? AND account_id=?",
                (name, keywords, exclude, regions, categories, min_amount, stages_s,
                 enabled, cid, account_id),
            )
            con.commit()
            return int(cid)
        cur = con.execute(
            "INSERT INTO conditions (account_id, name, keywords, exclude_keywords, regions, "
            "categories, min_amount, stages, enabled, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (account_id, name, keywords, exclude, regions, categories, min_amount,
             stages_s, enabled, int(time.time())),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def delete_condition(account_id: int, condition_id: int) -> None:
    con = connect()
    try:
        con.execute("DELETE FROM conditions WHERE id=? AND account_id=?", (condition_id, account_id))
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------ 발송
def already_sent(account_id: int, condition_id: int, stage: int) -> set[str]:
    con = connect()
    try:
        return {
            r["bid_id"] for r in con.execute(
                "SELECT bid_id FROM sent WHERE account_id=? AND condition_id=? AND stage=?",
                (account_id, condition_id, stage),
            )
        }
    finally:
        con.close()


def mark_sent(account_id: int, condition_id: int, stage: int, bid_ids: list[str]) -> None:
    if not bid_ids:
        return
    now = int(time.time())
    con = connect()
    try:
        con.executemany(
            "INSERT OR IGNORE INTO sent (account_id, condition_id, bid_id, stage, sent_at) "
            "VALUES (?,?,?,?,?)",
            [(account_id, condition_id, bid_id, stage, now) for bid_id in bid_ids],
        )
        con.commit()
    finally:
        con.close()


def log_alert(account_id: int, condition_id: int, condition_name: str, stage: int,
              bid_count: int, ok: bool, detail: str) -> None:
    con = connect()
    try:
        con.execute(
            "INSERT INTO alert_log (account_id, condition_id, condition_name, stage, bid_count, "
            "ok, detail, sent_at) VALUES (?,?,?,?,?,?,?,?)",
            (account_id, condition_id, condition_name[:60], stage, bid_count,
             1 if ok else 0, str(detail)[:200], int(time.time())),
        )
        con.commit()
    finally:
        con.close()


def has_success(account_id: int) -> bool:
    """실제로 한 번이라도 발송(테스트 포함)에 성공했는지."""
    con = connect()
    try:
        row = con.execute(
            "SELECT 1 FROM alert_log WHERE account_id=? AND ok=1 LIMIT 1", (account_id,)
        ).fetchone()
        return bool(row)
    finally:
        con.close()


def alert_history(account_id: int, limit: int = 50) -> list[sqlite3.Row]:
    con = connect()
    try:
        return con.execute(
            "SELECT * FROM alert_log WHERE account_id=? ORDER BY id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
    finally:
        con.close()


# ------------------------------------------------------------------ 초대
def create_invite(note: str = "") -> str:
    code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8].upper()
    con = connect()
    try:
        con.execute("INSERT INTO invites (code, note, created_at) VALUES (?,?,?)",
                    (code, str(note)[:80], int(time.time())))
        con.commit()
    finally:
        con.close()
    return code


def invite_valid(code: str) -> bool:
    code = (code or "").strip().upper()
    if not code:
        return False
    con = connect()
    try:
        row = con.execute("SELECT used_by FROM invites WHERE code=?", (code,)).fetchone()
        return bool(row and row["used_by"] is None)
    finally:
        con.close()


def consume_invite(code: str, account_id: int) -> None:
    code = (code or "").strip().upper()
    con = connect()
    try:
        con.execute("UPDATE invites SET used_by=? WHERE code=? AND used_by IS NULL",
                    (account_id, code))
        con.commit()
    finally:
        con.close()


def list_invites() -> list[sqlite3.Row]:
    con = connect()
    try:
        return con.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()
    finally:
        con.close()
