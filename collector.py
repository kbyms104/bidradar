#!/usr/bin/env python3
"""나라장터 입찰공고 수집기.

공공데이터포털의 '나라장터 입찰공고정보서비스'를 호출해 `data/live.db`에
저장합니다. 서버는 `BIDRADAR_SOURCE=live`일 때 이 DB를 읽습니다.

준비물
    1. https://www.data.go.kr/data/15129394/openapi.do 에서
       '조달청_나라장터 입찰공고정보서비스' 활용신청 (자동승인) 후
       **일반 인증키(Encoding)** 복사
    2. ~/bidradar/.env 에 `DATA_GO_KR_KEY=...` 저장

사용
    python3 collector.py                      # 최근 3일, 유형 전체
    python3 collector.py --days 6 --limit 3000  # 최초 백필
    python3 collector.py --minutes 30 --limit 200  # 증분 수집 (cron)
    python3 collector.py --dump               # 원본 응답 필드 확인 (키 필요)
    python3 collector.py --fixture sample.json    # 키 없이 파싱 테스트
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import config
import quota
import seed

KST = timezone(timedelta(hours=9))
BASE_URL = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService02"

# 업무구분별 오퍼레이션과 기본 카테고리
# 2026-11 공공데이터포털 문서 기준: BidPublicInfoService02 + 오퍼레이션명 ...02
OPS = {
    "servc": ("getBidPblancListInfoServc02", "용역/컨설팅"),
    "thng": ("getBidPblancListInfoThng02", "물품/구매"),
    "cnstwk": ("getBidPblancListInfoCnstwk02", "건설/토목"),
    "frgcpt": ("getBidPblancListInfoFrgcpt02", "물품/구매"),
    "etc": ("getBidPblancListInfoEtc02", "용역/컨설팅"),
}

REGIONS = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]

SW_HINTS = (
    "시스템", "소프트웨어", "플랫폼", "데이터", "정보화", "인공지능", "AI",
    "클라우드", "앱", "홈페이지", "네트워크", "전산", "정보시스템", "DB",
)
ELEC_HINTS = ("전기", "통신", "CCTV", "조명", "정보통신", "계측", "제어")
MED_HINTS = ("의료", "의약", "바이오", "병원", "진단", "임상")

CATEGORIES = [
    "소프트웨어/SI", "건설/토목", "시설/유지관리", "물품/구매",
    "용역/컨설팅", "전기/통신", "의료/바이오",
]

# 실제 응답 필드 (공공데이터포털 OpenAPI 문서 15129394 기준)
FIELDS = {
    "notice_no": ("bidNtceNo",),
    "notice_ord": ("bidNtceOrd",),
    "title": ("bidNtceNm",),
    "org": ("ntceInsttNm", "dminsttNm"),
    "demand": ("dminsttNm",),
    "amount": ("presmptPrce", "bdgtAmt", "asignBdgtAmt"),
    "posted": ("bidNtceDt", "rgstDt"),
    "close": ("bidClseDt", "opengDt"),
    "type": ("ntceKindNm",),
    "dept": ("ntceInsttOfclDeptNm", "dminsttOfclDeptNm"),
    "name": ("ntceInsttOfclNm",),
    "phone": ("ntceInsttOfclTelNo",),
    "url": ("bidNtceDtlUrl", "bidNtceUrl"),
    "site_region": ("cnstrtsiteRgnNm",),
}


def pick(item: dict, field: str, default: str = "") -> str:
    for key in FIELDS[field]:
        value = item.get(key)
        if value not in (None, "", "null"):
            return str(value).strip()
    return default


def to_ts(value: str) -> int:
    """'2026-11-11 18:00:00', '202611111800' 등을 KST epoch로 변환."""
    text = str(value or "").strip()
    if not text:
        return 0
    digits = re.sub(r"\D", "", text)
    formats = {14: "%Y%m%d%H%M%S", 12: "%Y%m%d%H%M", 8: "%Y%m%d"}
    fmt = formats.get(len(digits))
    if fmt:
        try:
            return int(datetime.strptime(digits, fmt).replace(tzinfo=KST).timestamp())
        except ValueError:
            return 0
    return 0


def to_amount(value: str) -> int:
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else 0


def guess_region(*texts: str) -> str:
    joined = " ".join(t for t in texts if t)
    for region in REGIONS:
        if region in joined:
            return region
    return "전국"


def guess_category(title: str, base: str) -> str:
    hit = title.replace(" ", "")
    if base == "소프트웨어/SI" or (base == "용역/컨설팅" and any(h in hit for h in SW_HINTS)):
        return "소프트웨어/SI"
    if base == "건설/토목" and any(h in hit for h in ELEC_HINTS):
        return "전기/통신"
    if base == "물품/구매" and any(h in hit for h in MED_HINTS):
        return "의료/바이오"
    return base if base in CATEGORIES else "용역/컨설팅"


def guess_type(title: str, raw: str) -> str:
    joined = f"{raw} {title}"
    if "긴급" in joined:
        return "긴급공고"
    if "재공고" in joined or "재입찰" in joined:
        return "재공고"
    return "입찰공고"


def normalize(item: dict, base_category: str) -> dict | None:
    notice_no = pick(item, "notice_no")
    title = pick(item, "title")
    close_at = to_ts(pick(item, "close"))
    if not notice_no or not title or not close_at:
        return None

    ord_no = pick(item, "notice_ord")
    if ord_no and ord_no not in ("00", "000", "0", "1"):
        notice_no = f"{notice_no}-{ord_no}"

    posted_at = to_ts(pick(item, "posted")) or close_at - 7 * 86400
    org = pick(item, "org") or pick(item, "demand") or "-"
    demand = pick(item, "demand")
    site_region = pick(item, "site_region")
    region = guess_region(org, demand, site_region)
    category = guess_category(title, base_category)

    description = "나라장터에 게시된 공고입니다. 세부 내용과 제출 서류는 원문에서 확인하세요."
    if demand and demand != org:
        description += f" 수요기관: {demand}."

    return {
        "id": f"live-{notice_no}",
        "notice_no": notice_no,
        "title": title,
        "org": org,
        "region": region,
        "category": category,
        "amount": to_amount(pick(item, "amount")),
        "posted_at": min(posted_at, close_at - 60),
        "close_at": close_at,
        "bid_type": guess_type(title, pick(item, "type")),
        "description": description,
        "qualifications": "[]",
        "attachments": json.dumps([], ensure_ascii=False),
        "contact_dept": pick(item, "dept") or "-",
        "contact_name": pick(item, "name") or "-",
        "contact_phone": pick(item, "phone") or "-",
        "url": pick(item, "url"),
    }


def parse_payload(raw: bytes) -> tuple[dict, list[dict]]:
    """JSON 또는 XML 응답을 (헤더, 아이템목록)으로 변환."""
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        payload = json.loads(text)
        root = payload.get("response") or payload.get("OpenAPI_ServiceResponse") or {}
        header = root.get("header") or root.get("cmmMsgHeader") or {}
        body = root.get("body") or {}
        items = body.get("items") or {}
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        return header, items or []

    root = ET.fromstring(text)
    header = {child.tag: (child.text or "") for child in (root.find(".//header") or [])}
    items = []
    for node in root.iter("item"):
        items.append({child.tag: (child.text or "") for child in node})
    return header, items


def request_page(op: str, key: str, bgn: str, end: str, page: int, rows: int) -> tuple[dict, list[dict]]:
    params = urllib.parse.urlencode({
        "pageNo": page,
        "numOfRows": rows,
        "inqryDiv": 1,
        "inqryBgnDt": bgn,
        "inqryEndDt": end,
        "type": "json",
    })
    # 인증키는 포털에서 'Encoding' 값을 그대로 붙여넣는 경우가 많아 이중 인코딩을 피합니다.
    key_part = key if "%" in key else urllib.parse.quote(key, safe="")
    url = f"{BASE_URL}/{op}?serviceKey={key_part}&{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "BidRadar/0.2"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return parse_payload(res.read())


def store(rows: list[dict], db_path: str, source_label: str) -> tuple[int, int]:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path, timeout=15)
    try:
        con.executescript(seed.SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(bids)")}
        if "url" not in cols:
            con.execute("ALTER TABLE bids ADD COLUMN url TEXT NOT NULL DEFAULT ''")
        con.execute("PRAGMA journal_mode=WAL")
        inserted = 0
        for row in rows:
            cur = con.execute(
                "INSERT INTO bids(id,notice_no,title,org,region,category,amount,posted_at,"
                "close_at,bid_type,description,qualifications,attachments,contact_dept,"
                "contact_name,contact_phone,url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, org=excluded.org, "
                "amount=excluded.amount, posted_at=excluded.posted_at, close_at=excluded.close_at, "
                "bid_type=excluded.bid_type, description=excluded.description, "
                "contact_dept=excluded.contact_dept, contact_name=excluded.contact_name, "
                "contact_phone=excluded.contact_phone, url=excluded.url",
                (
                    row["id"], row["notice_no"], row["title"], row["org"], row["region"],
                    row["category"], row["amount"], row["posted_at"], row["close_at"],
                    row["bid_type"], row["description"], row["qualifications"],
                    row["attachments"], row["contact_dept"], row["contact_name"],
                    row["contact_phone"], row["url"],
                ),
            )
            inserted += cur.rowcount
        # 마감이 한참 지난 공고는 정리 (최근 7일은 남겨서 리포트에 활용)
        con.execute("DELETE FROM bids WHERE close_at < ?", (int(time.time()) - 7 * 86400,))
        con.execute(
            "INSERT INTO meta(key,value) VALUES('last_sync',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(int(time.time())),),
        )
        con.execute(
            "INSERT INTO meta(key,value) VALUES('last_source',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (source_label,),
        )
        con.commit()
        return inserted, con.execute("SELECT COUNT(*) FROM bids").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="나라장터 입찰공고 수집")
    parser.add_argument("--days", type=int, default=3, help="최근 며칠 (기본 3, 최대 6)")
    parser.add_argument("--minutes", type=int, default=0,
                        help="최근 몇 분 (지정 시 --days보다 우선. 증분 수집용)")
    parser.add_argument("--limit", type=int, default=500, help="유형별 최대 수집 건수")
    parser.add_argument("--types", default=None,
                        help="servc,thng,cnstwk,frgcpt,etc 조합 (기본: servc,thng,cnstwk,etc)")
    parser.add_argument("--dump", action="store_true", help="첫 응답 원본을 data/raw_bid_sample.json에 저장")
    parser.add_argument("--fixture", help="API 대신 저장된 JSON/XML 파일로 파싱 테스트")
    parser.add_argument("--dry-run", action="store_true", help="DB에 저장하지 않고 결과만 출력")
    args = parser.parse_args()

    config.load_env()
    key = config.data_go_kr_key()
    db_path = os.environ.get("BIDRADAR_LIVE_DB") or config.LIVE_DB

    type_arg = args.types or ("servc" if args.fixture else "servc,thng,cnstwk,etc")
    types = [t.strip() for t in type_arg.split(",") if t.strip() in OPS]
    if not types:
        print("수집 유형이 올바르지 않습니다:", type_arg, file=sys.stderr)
        return 2

    if not args.fixture and not key:
        print(
            "DATA_GO_KR_KEY가 없습니다.\n"
            "1) https://www.data.go.kr/data/15129394/openapi.do 접속\n"
            "   ('조달청_나라장터 입찰공고정보서비스' → 활용신청, 보통 즉시 승인)\n"
            "2) ~/bidradar/.env 에 DATA_GO_KR_KEY=<일반 인증키(Encoding)> 저장\n"
            "3) python3 collector.py 실행",
            file=sys.stderr,
        )
        return 2

    now = datetime.now(KST)
    if args.minutes > 0:
        minutes = max(1, min(args.minutes, 6 * 24 * 60))
        bgn = (now - timedelta(minutes=minutes)).strftime("%Y%m%d%H%M")
    else:
        # 2026-11-10 포털 공지: 조회기간 최대 7일. 정확히 7일은 거부되어 6일로 제한.
        days = max(1, min(args.days, 6))
        if args.days > 6:
            print("조회기간은 최대 6일로 조정합니다. (포털 제한 7일, 경계값 거부)", file=sys.stderr)
        bgn = (now - timedelta(days=days)).strftime("%Y%m%d%H%M")
    end = now.strftime("%Y%m%d%H%M")

    all_rows: list[dict] = []
    dumped = False
    budget_out = False
    # 일일 호출 예산 보호: 사용률이 높으면 유형별 1페이지만 받습니다.
    pages_cap = 1 if args.limit <= 100 else 2
    pages_cap = min(pages_cap, quota.pages_per_type(2))
    for name in types:
        if budget_out:
            break
        op, base_category = OPS[name]
        collected = 0
        page = 1
        while collected < args.limit and page <= pages_cap:
            if not args.fixture and not quota.take():
                print(
                    f"[{name}] 일일 API 예산 소진 — 수집 중단 "
                    f"(사용 {quota.used():,}/{quota.budget():,}회, 한도 {quota.limit():,}회)",
                    file=sys.stderr,
                )
                budget_out = True
                break
            try:
                if args.fixture:
                    with open(args.fixture, "rb") as fh:
                        header, items = parse_payload(fh.read())
                else:
                    header, items = request_page(op, key, bgn, end, page, 100)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:300]
                print(f"[{name}] HTTP {exc.code}: {body}", file=sys.stderr)
                if exc.code == 429:
                    quota.note_quota_error(f"HTTP 429: {body}")
                    budget_out = True
                break
            except (urllib.error.URLError, TimeoutError, ET.ParseError, json.JSONDecodeError) as exc:
                if not args.fixture:
                    quota.release()  # 포털에 도달하지 못한 호출은 돌려줍니다.
                print(f"[{name}] 요청 실패: {exc}", file=sys.stderr)
                break

            code = str(header.get("resultCode") or header.get("returnReasonCode") or "00")
            if code not in ("00", "0", ""):
                message = header.get("resultMsg") or header.get("returnAuthMsg") or "알 수 없는 오류"
                print(f"[{name}] API 오류 {code}: {message}", file=sys.stderr)
                if quota.looks_like_quota_error(code, message):
                    quota.note_quota_error(f"{code}: {message}")
                    budget_out = True
                break

            if args.dump and items and not dumped:
                os.makedirs(config.DATA_DIR, exist_ok=True)
                with open(os.path.join(config.DATA_DIR, "raw_bid_sample.json"), "w", encoding="utf-8") as fh:
                    json.dump(items[0], fh, ensure_ascii=False, indent=2)
                dumped = True
                print("원본 샘플 저장: data/raw_bid_sample.json")

            if not items:
                break

            for item in items:
                row = normalize(item, base_category)
                if row:
                    all_rows.append(row)
            collected += len(items)
            if args.fixture:
                break
            page += 1
            time.sleep(0.15)
        print(f"[{name}] {collected}건 수신")

    # 중복 제거 (같은 공고번호가 여러 유형에 걸칠 수 있음)
    unique = {row["id"]: row for row in all_rows}
    rows = list(unique.values())
    print(f"정규화 완료: {len(rows)}건 (중복 {len(all_rows) - len(rows)}건 제거)")

    if args.dry_run:
        for row in rows[:5]:
            print(json.dumps({k: row[k] for k in ("notice_no", "title", "org", "category", "amount")},
                             ensure_ascii=False))
        return 0

    if not rows:
        print("저장할 공고가 없습니다. (기간/인증키를 확인하세요)")
        return 1

    inserted, total = store(rows, db_path, "data.go.kr")
    print(f"저장 완료: {db_path} (신규/갱신 {inserted}건, 전체 {total}건)")
    if not args.fixture:
        st = quota.status()
        print(f"API 호출: {st['calls']:,}/{st['budget']:,}회 ({st['percent']:.0f}%) · 남음 {st['remaining']:,}회")
    print("서버 반영: .env 에 BIDRADAR_SOURCE=live 설정 후 `pm2 restart bidradar`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
