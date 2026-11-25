#!/usr/bin/env python3
"""BidRadar demo data.

Generates a fictional but realistic procurement feed so the dashboard can be
shown without a public-data API key. Every row is fake; agency names are public
institutions, but notice numbers, amounts and titles are generated.

Run directly to reseed:  python3 seed.py --force
"""
from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "bids.db")
KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS bids (
    id             TEXT PRIMARY KEY,
    notice_no      TEXT NOT NULL,
    title          TEXT NOT NULL,
    org            TEXT NOT NULL,
    region         TEXT NOT NULL,
    category       TEXT NOT NULL,
    amount         INTEGER NOT NULL,
    posted_at      INTEGER NOT NULL,
    close_at       INTEGER NOT NULL,
    bid_type       TEXT NOT NULL,
    description    TEXT,
    qualifications TEXT,
    attachments    TEXT,
    contact_dept   TEXT,
    contact_name   TEXT,
    contact_phone  TEXT,
    url            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bids_close ON bids(close_at);
CREATE INDEX IF NOT EXISTS idx_bids_posted ON bids(posted_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

AGENCIES = {
    "서울": ["서울특별시", "서울시설공단", "서울교통공사", "SH서울주택도시공사", "서울대학교병원"],
    "경기": ["경기도청", "경기도교육청", "경기주택도시공사", "분당서울대학교병원", "경기연구원"],
    "인천": ["인천광역시", "인천국제공항공사", "인천교통공사", "인하대학교병원"],
    "부산": ["부산광역시", "부산교통공사", "부산대학교병원", "부산도시공사"],
    "대구": ["대구광역시", "대구시설공단", "대구도시개발공사", "경북대학교병원"],
    "광주": ["광주광역시", "광주교통공사", "광주도시공사"],
    "대전": ["대전광역시", "한국철도공사", "대전도시공사"],
    "울산": ["울산광역시", "울산시설공단", "울산도시공사"],
    "세종": ["세종특별자치시", "행정중심복합도시건설청", "세종시설관리공단"],
    "강원": ["강원특별자치도", "강원랜드", "강원도개발공사"],
    "충북": ["충청북도", "충북대학교병원", "충북개발공사"],
    "충남": ["충청남도", "충남대학교병원", "충남개발공사"],
    "전북": ["전북특별자치도", "전북대학교병원", "전북개발공사"],
    "전남": ["전라남도", "전남대학교병원", "전남개발공사"],
    "경북": ["경상북도", "경북대학교병원", "경북개발공사"],
    "경남": ["경상남도", "경상대학교병원", "경남개발공사"],
    "제주": ["제주특별자치도", "제주국제자유도시개발센터", "제주에너지공사"],
    "전국": ["한국도로공사", "LH한국토지주택공사", "국가철도공단", "조달청", "한국전력공사",
             "건강보험심사평가원", "한국가스공사", "한국수자원공사"],
}

REGION_WEIGHTS = {
    "서울": 15, "경기": 21, "인천": 6, "부산": 7, "대구": 5, "광주": 4, "대전": 5,
    "울산": 3, "세종": 3, "강원": 4, "충북": 4, "충남": 5, "전북": 4, "전남": 4,
    "경북": 5, "경남": 6, "제주": 3, "전국": 12,
}

TITLES = {
    "소프트웨어/SI": [
        "{org} 지능형 업무지원 시스템 구축",
        "{org} 데이터 통합 플랫폼 고도화",
        "{org} 클라우드 전환 및 보안 컨설팅",
        "{org} AI 기반 민원 분석 서비스 구축",
        "{org} 통합 관제 대시보드 구축",
        "{org} 노후 정보시스템 재구축",
        "{org} 빅데이터 분석 환경 구축",
        "{org} 모바일 통합 앱 개발",
    ],
    "건설/토목": [
        "{org} 노후 교량 보수공사",
        "{org} 하수관로 정비공사",
        "{org} 청사 리모델링 공사",
        "{org} 도로 포장 및 배수시설 정비",
        "{org} 근린공원 조성 사업",
        "{org} 재해위험지구 정비공사",
        "{org} 보행환경 개선사업",
        "{org} 상수도 노후관 교체공사",
    ],
    "시설/유지관리": [
        "{org} 시설물 유지관리 용역",
        "{org} 전기설비 안전점검 용역",
        "{org} 청소 및 방역 용역",
        "{org} 승강기 유지보수 용역",
        "{org} 조경 관리 용역",
        "{org} 냉난방 설비 유지보수",
        "{org} 소방시설 종합점검 용역",
    ],
    "물품/구매": [
        "{org} 의료장비 구매",
        "{org} 사무용 PC 및 모니터 구매",
        "{org} 소방 장비 구매",
        "{org} 실험실 장비 구매",
        "{org} 전기자동차 충전기 구매",
        "{org} CCTV 장비 구매",
        "{org} 도서관 자동화 장비 구매",
    ],
    "용역/컨설팅": [
        "{org} 중장기 발전계획 수립 용역",
        "{org} 정보화 전략계획(ISP) 수립",
        "{org} 조직 진단 및 인사제도 개선",
        "{org} 안전보건 관리체계 구축",
        "{org} ESG 경영체계 수립 용역",
        "{org} 고객만족도 조사 용역",
        "{org} 업무프로세스 개선(BPR) 용역",
    ],
    "전기/통신": [
        "{org} 지능형 CCTV 설치공사",
        "{org} 통신망 고도화 사업",
        "{org} 태양광 발전설비 설치",
        "{org} 전기설비 증설공사",
        "{org} 스마트 조명 교체사업",
        "{org} 무선통신 인프라 구축",
    ],
    "의료/바이오": [
        "{org} 의료정보시스템 고도화",
        "{org} 감염병 대응 장비 구축",
        "{org} 원격진료 플랫폼 구축",
        "{org} 의료영상 저장시스템(PACS) 교체",
        "{org} 임상시험 데이터 관리시스템 구축",
    ],
}

AMOUNTS = {
    "소프트웨어/SI": (120_000_000, 3_200_000_000),
    "건설/토목": (90_000_000, 8_500_000_000),
    "시설/유지관리": (28_000_000, 780_000_000),
    "물품/구매": (18_000_000, 1_400_000_000),
    "용역/컨설팅": (45_000_000, 1_100_000_000),
    "전기/통신": (70_000_000, 2_200_000_000),
    "의료/바이오": (95_000_000, 2_600_000_000),
}

CATEGORY_WEIGHTS = {
    "소프트웨어/SI": 22, "건설/토목": 20, "시설/유지관리": 15, "물품/구매": 14,
    "용역/컨설팅": 13, "전기/통신": 9, "의료/바이오": 7,
}

QUALIFICATIONS = {
    "소프트웨어/SI": ["소프트웨어사업자 신고업체", "정보통신공사업 등록업체", "직접생산증명서 보유"],
    "건설/토목": ["건설업 면허 보유(토목·건축)", "전문건설업 등록업체", "시공능력평가 확인서"],
    "시설/유지관리": ["시설관리용역업 등록업체", "전기안전관리 대행업체", "소방시설관리업 등록"],
    "물품/구매": ["중소기업자간 경쟁제품 직접생산 확인", "조달청 우수제품 인증", "제조사 공급확인서"],
    "용역/컨설팅": ["경영지도사 또는 기술사 보유", "최근 3년 유사용역 실적 1건 이상", "중소기업 확인서"],
    "전기/통신": ["전기공사업 등록업체", "정보통신공사업 등록업체", "기술인력 보유 확인서"],
    "의료/바이오": ["의료기기 제조·수입업 허가", "ISO 13485 인증", "최근 3년 납품 실적"],
}

DESCRIPTIONS = {
    "소프트웨어/SI": "기존 업무 시스템을 통합하고 데이터 기반 의사결정을 지원하는 플랫폼을 구축합니다. 요구사항 정의, 설계, 개발, 이관 및 1년 유지보수를 포함합니다.",
    "건설/토목": "노후 시설물의 안전성 확보와 기능 개선을 위한 공사입니다. 착공 전 안전계획 제출, 준공 후 2년 하자보수를 포함합니다.",
    "시설/유지관리": "시설물의 안정적 운영을 위한 정기 점검 및 유지관리 용역입니다. 상주 인력 운영과 월간 점검 보고서 제출이 포함됩니다.",
    "물품/구매": "현장 운영에 필요한 장비를 구매·설치·시운전합니다. 납품 후 1년 무상 유지보수와 사용자 교육을 포함합니다.",
    "용역/컨설팅": "조직 및 사업 환경을 분석해 중장기 실행 계획을 수립합니다. 중간보고 3회, 최종보고 1회와 실행 로드맵을 제출합니다.",
    "전기/통신": "노후 전기·통신 설비를 교체하고 원격 모니터링 체계를 구축합니다. 시공 후 성능시험과 운영자 교육을 포함합니다.",
    "의료/바이오": "의료 현장의 정보 연계와 환자 안전 강화를 위한 시스템을 구축합니다. 개인정보 보호 요건과 의료정보 표준을 준수해야 합니다.",
}

ATTACHMENTS = [
    "입찰공고문.pdf", "과업지시서.hwp", "제안요청서.pdf", "규격서.xlsx",
    "계약특수조건.hwp", "산출내역서.xlsx", "물량내역서.xlsx",
]

SURNAMES = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권"]
GIVEN = ["민수", "지훈", "서연", "현우", "수진", "도윤", "예린", "준호", "하늘", "유진", "태호", "소영"]

AREA_CODES = {
    "서울": "02", "경기": "031", "인천": "032", "부산": "051", "대구": "053",
    "광주": "062", "대전": "042", "울산": "052", "세종": "044", "강원": "033",
    "충북": "043", "충남": "041", "전북": "063", "전남": "061", "경북": "054",
    "경남": "055", "제주": "064", "전국": "02",
}

DEPARTMENTS = ["계약팀", "총무팀", "구매팀", "시설관리팀", "정보화팀", "재무팀", "사업관리팀"]


def _round_amount(value: int) -> int:
    """Round to a believable bid unit (백만 원)."""
    return max(1_000_000, int(round(value / 1_000_000)) * 1_000_000)


def _align_close(ts: int, rng: random.Random) -> int:
    """Put deadlines on working-hour boundaries in KST."""
    local = datetime.fromtimestamp(ts, KST)
    hour = rng.choice([10, 11, 14, 15, 17, 18])
    return int(local.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp())


def seed(force: bool = False) -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")

    existing = con.execute("SELECT value FROM meta WHERE key='seeded_at'").fetchone()
    fresh = existing and time.time() - int(existing[0]) < 2 * 86400
    if existing and fresh and not force:
        con.close()
        return

    rng = random.Random(20261111)
    now = int(time.time())
    rows = []

    regions = list(REGION_WEIGHTS)
    region_weights = [REGION_WEIGHTS[r] for r in regions]
    categories = list(CATEGORY_WEIGHTS)
    category_weights = [CATEGORY_WEIGHTS[c] for c in categories]

    for i in range(190):
        category = rng.choices(categories, weights=category_weights, k=1)[0]
        region = rng.choices(regions, weights=region_weights, k=1)[0]
        org = rng.choice(AGENCIES[region])
        title = rng.choice(TITLES[category]).format(org=org)

        roll = rng.random()
        if roll < 0.07:
            close_at = now - rng.randint(4 * 3600, 2 * 86400)
        elif roll < 0.34:
            close_at = now + rng.randint(2 * 3600, 3 * 86400)
        elif roll < 0.69:
            close_at = now + rng.randint(3 * 86400, 7 * 86400)
        else:
            close_at = now + rng.randint(7 * 86400, 21 * 86400)
        close_at = _align_close(close_at, rng)
        if roll < 0.07 and close_at > now:
            close_at -= 86400
        if roll >= 0.07 and close_at < now:
            close_at += 86400

        posted_at = close_at - rng.randint(4, 15) * 86400 - rng.randint(0, 8) * 3600
        if posted_at > now:
            posted_at = now - rng.randint(18, 72) * 3600

        low, high = AMOUNTS[category]
        amount = _round_amount(int(10 ** rng.uniform(math.log10(low), math.log10(high))))

        bid_type = rng.choices(
            ["입찰공고", "재공고", "긴급공고"], weights=[74, 16, 10], k=1
        )[0]
        notice_no = f"R26BK{rng.randint(100000, 999999)}-{rng.randint(1, 3)}"
        contact = rng.choice(SURNAMES) + rng.choice(GIVEN)
        phone = f"{AREA_CODES[region]}-{rng.randint(200, 999)}-{rng.randint(1000, 9999)}"
        attaches = rng.sample(ATTACHMENTS, k=rng.randint(2, 5))

        rows.append((
            f"bid-{i:03d}",
            notice_no,
            title,
            org,
            region,
            category,
            amount,
            posted_at,
            close_at,
            bid_type,
            DESCRIPTIONS[category],
            json.dumps(rng.sample(QUALIFICATIONS[category], k=2), ensure_ascii=False),
            json.dumps(attaches, ensure_ascii=False),
            rng.choice(DEPARTMENTS),
            contact,
            phone,
        ))

    con.execute("DELETE FROM bids")
    # Give the demo a believable "today" burst and a quieter yesterday.
    # posted_at is index 7, close_at is index 8.
    for j, row in enumerate(rows):
        item = list(row)
        if j < 32:
            item[7] = now - rng.randint(1, 20) * 3600
        elif j < 46:
            item[7] = now - rng.randint(26, 46) * 3600
        else:
            item[7] = now - rng.randint(5, 12) * 86400
        item[7] = min(item[7], item[8] - 3600, now - 60)
        rows[j] = tuple(item)

    con.executemany(
        "INSERT INTO bids(id,notice_no,title,org,region,category,amount,posted_at,"
        "close_at,bid_type,description,qualifications,attachments,contact_dept,"
        "contact_name,contact_phone) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.execute(
        "INSERT INTO meta(key,value) VALUES('seeded_at',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(now),),
    )
    con.commit()
    con.close()
    print(f"seeded {len(rows)} bids -> {DB_PATH}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    seed(force=args.force)
