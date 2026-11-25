#!/usr/bin/env python3
"""BidRadar 공통 설정.

`.env` 파일이 있으면 읽어 환경변수로 올립니다. 이미 설정된 환경변수는
덮어쓰지 않으므로 pm2/systemd 환경변수가 우선합니다.
"""
from __future__ import annotations

import os

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")
DATA_DIR = os.path.join(BASE, "data")

DEMO_DB = os.path.join(DATA_DIR, "bids.db")
LIVE_DB = os.path.join(DATA_DIR, "live.db")
LEADS_DB = os.path.join(DATA_DIR, "leads.db")
APP_DB = os.path.join(DATA_DIR, "app.db")


def load_env(path: str = ENV_PATH) -> None:
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def source() -> str:
    """demo(가상 데이터) 또는 live(나라장터 실데이터)."""
    value = (os.environ.get("BIDRADAR_SOURCE") or "demo").strip().lower()
    return value if value in ("demo", "live") else "demo"


def data_go_kr_key() -> str:
    return (os.environ.get("DATA_GO_KR_KEY") or os.environ.get("BIDRADAR_API_KEY") or "").strip()


def app_db() -> str:
    """고객 계정·조건·발송 이력 DB 경로."""
    return os.environ.get("BIDRADAR_APP_DB") or APP_DB


def session_days() -> int:
    try:
        return max(1, min(365, int(os.environ.get("BIDRADAR_SESSION_DAYS") or 30)))
    except ValueError:
        return 30


def signup_mode() -> str:
    """open(누구나 가입) 또는 invite(초대코드 필요). 기본은 invite."""
    value = (os.environ.get("BIDRADAR_SIGNUP") or "invite").strip().lower()
    return value if value in ("open", "invite") else "invite"


def contact_email() -> str:
    """약관·방침에 공개할 문의 이메일. 비어 있으면 상담 폼으로 안내합니다."""
    return (os.environ.get("BIDRADAR_CONTACT_EMAIL") or "").strip()


def admin_password() -> str:
    return (os.environ.get("BIDRADAR_ADMIN_PASSWORD") or "").strip()
