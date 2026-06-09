#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
youtube_pension_db.py
=====================
증권사 유튜브 채널의 '연금' 관련 영상을 전수 수집 → 분류(카테고리화) → DB화하는 파이프라인.

대상 채널 (기본값):
  - 미래에셋 스마트머니  : UCZS9wEZ4itPbBZk_sqccXfw  (@smartmoney0)
  - 한국투자 뱅키스       : UCU6f21g_qaJk6rkX-IF6X2g  (@bankiszon)

동작 방식:
  1) 각 채널의 '업로드 재생목록(uploads playlist)'을 통해 전체 영상 ID를 페이지네이션으로 수집
     (search.list 대신 playlistItems.list 사용 → 누락 없이 전수, 쿼터도 저렴)
  2) videos.list 로 50개씩 배치 조회 → 제목/설명/길이/조회수/좋아요/댓글/태그/썸네일 등 메타 추출
  3) 제목+설명+태그 텍스트로 '연금 관련 여부' 판별 + 카테고리/타깃/포맷 자동 태깅
  4) SQLite + CSV + Excel(xlsx) 로 동시 출력

필요:
  - YouTube Data API v3 키 (Google Cloud Console에서 무료 발급)
  - pip install google-api-python-client pandas openpyxl

실행:
  export YOUTUBE_API_KEY="발급받은_키"
  python youtube_pension_db.py                  # 연금 관련 영상만 DB화 (기본)
  python youtube_pension_db.py --all            # 전체 영상 수집 + is_pension 플래그만 표기
  python youtube_pension_db.py --since 2023-01-01   # 업로드일 필터
"""

import os
import re
import sys
import argparse
import sqlite3
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("requests 가 없습니다.  pip install requests")

YT_API_BASE = "https://www.googleapis.com/youtube/v3"

def _yt_get(endpoint: str, key: str, **params) -> dict:
    resp = requests.get(
        f"{YT_API_BASE}/{endpoint}",
        params={"key": key, **params},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

# pandas/openpyxl 은 Excel/CSV 출력에만 사용 (없으면 SQLite만 생성)
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


# ────────────────────────────────────────────────────────────────────
# 1. 설정: 대상 채널
# ────────────────────────────────────────────────────────────────────
CHANNELS = {
    "UCZS9wEZ4itPbBZk_sqccXfw": "미래에셋 스마트머니",
    "UCU6f21g_qaJk6rkX-IF6X2g": "한국투자 뱅키스",
}


# ────────────────────────────────────────────────────────────────────
# 2. 연금 콘텐츠 분류 체계 (Taxonomy)
#    - PENSION_FLAG_KEYWORDS : 이 중 하나라도 걸리면 '연금 관련' 영상으로 판정
#    - CATEGORY_KEYWORDS     : 대분류별 키워드. 점수가 가장 높은 게 primary,
#                              매칭된 나머지는 secondary 로 기록
#    ※ 키워드는 자유롭게 추가/수정하세요. 분류 품질은 여기에 달려 있습니다.
# ────────────────────────────────────────────────────────────────────
PENSION_FLAG_KEYWORDS = [
    "연금", "퇴직연금", "개인연금", "연금저축", "연저펀", "IRP", "DC형", "DB형",
    "노후", "은퇴", "연금ETF", "TDF", "연금계좌", "연금수령", "세액공제",
    "과세이연", "ISA",  # ISA는 연금 맥락에서 자주 등장 (오탐 시 제거)
]

CATEGORY_KEYWORDS = {
    "01_연금제도_기초": [
        "연금이란", "3층연금", "3층 연금", "국민연금", "연금제도", "연금 종류",
        "연금 구조", "공적연금", "사적연금", "연금 기초", "연금 개념",
    ],
    "02_연금저축": [
        "연금저축", "연금저축펀드", "연금저축계좌", "연저펀", "연금저축보험",
    ],
    "03_퇴직연금_IRP": [
        "IRP", "퇴직연금", "DC형", "DB형", "확정기여", "확정급여",
        "퇴직금", "퇴직급여", "개인형퇴직연금",
    ],
    "04_연금투자_운용": [
        "연금ETF", "연금 ETF", "TDF", "연금 포트폴리오", "연금 운용",
        "연금 투자", "리밸런싱", "자산배분", "디폴트옵션", "사전지정운용",
    ],
    "05_세제_절세": [
        "세액공제", "과세이연", "연말정산", "절세", "분리과세", "저율과세",
        "연금소득세", "세금", "13월의 월급",
    ],
    "06_수령_인출전략": [
        "연금수령", "연금 수령", "연금개시", "인출", "수령방식", "수령 순서",
        "연금 인출", "월 배당", "현금흐름", "연금화",
    ],
    "07_노후설계_은퇴준비": [
        "노후", "은퇴", "노후자금", "은퇴설계", "노후준비", "은퇴준비",
        "노후 대비", "은퇴자금", "노후 생활비", "파이어족", "FIRE",
    ],
    "08_제도_정책변화": [
        "세법개정", "납입한도", "한도 확대", "한도 증액", "제도 변경",
        "정책", "개정", "법 개정",
    ],
    "09_상품_서비스_이벤트": [
        "이벤트", "계좌개설", "계좌 개설", "연금이전", "연금 이전", "옮기기",
        "혜택", "수수료", "앱", "프로모션", "오픈",
    ],
    "10_글로벌_해외투자연금": [
        "해외주식", "미국주식", "글로벌", "S&P", "나스닥", "환헤지", "환노출",
        "해외 ETF", "미국 ETF",
    ],
}

# 타깃 시청자 추정 키워드
AUDIENCE_KEYWORDS = {
    "사회초년생/2030": ["사회초년생", "2030", "20대", "30대", "직장인 첫", "신입", "초보", "처음"],
    "4050_중장년": ["4050", "40대", "50대", "중년", "중장년"],
    "은퇴예정/은퇴자": ["은퇴 예정", "은퇴자", "5060", "60대", "정년", "퇴직 앞둔"],
    "자영업/프리랜서": ["자영업", "프리랜서", "개인사업자", "사업자"],
}

# 콘텐츠 포맷 추정 키워드
FORMAT_KEYWORDS = {
    "강의/설명": ["총정리", "완벽정리", "한방에", "기초", "강의", "설명", "알아보기", "방법"],
    "대담/인터뷰": ["인터뷰", "대담", "회장", "전문가", "초대", "특집"],
    "Q&A/질문": ["Q&A", "질문", "궁금", "FAQ", "답변"],
    "뉴스/이슈": ["이슈", "뉴스", "속보", "발표", "개정"],
    "비교/추천": ["비교", "추천", "베스트", "TOP", "vs", "어디"],
}


# ────────────────────────────────────────────────────────────────────
# 3. 유틸: ISO8601 duration 파싱, 텍스트 분류
# ────────────────────────────────────────────────────────────────────
_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

def parse_duration(iso: str) -> int:
    """'PT1H2M30S' → 초(int)."""
    if not iso:
        return 0
    m = _DUR_RE.fullmatch(iso)
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s

def hms(seconds: int) -> str:
    """초 → 'H:MM:SS' 또는 'M:SS'."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _count_hits(text: str, keywords) -> int:
    t = text.lower()
    return sum(1 for kw in keywords if kw.lower() in t)

def is_pension(text: str) -> bool:
    return _count_hits(text, PENSION_FLAG_KEYWORDS) > 0

def categorize(text: str):
    """대분류 점수 산정 → (primary, secondary_list)."""
    scores = {cat: _count_hits(text, kws) for cat, kws in CATEGORY_KEYWORDS.items()}
    matched = {c: s for c, s in scores.items() if s > 0}
    if not matched:
        return "00_미분류", []
    ordered = sorted(matched, key=lambda c: matched[c], reverse=True)
    return ordered[0], ordered[1:]

def first_match(text: str, mapping, default):
    for label, kws in mapping.items():
        if _count_hits(text, kws) > 0:
            return label
    return default


# ────────────────────────────────────────────────────────────────────
# 4. YouTube Data API 수집 (requests 사용)
# ────────────────────────────────────────────────────────────────────
def get_uploads_playlist(key: str, channel_id: str) -> str:
    data = _yt_get("channels", key, part="contentDetails", id=channel_id)
    items = data.get("items", [])
    if not items:
        raise RuntimeError(f"채널을 찾을 수 없음: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

def iter_video_ids(key: str, playlist_id: str):
    token = None
    while True:
        params = dict(part="contentDetails", playlistId=playlist_id, maxResults=50)
        if token:
            params["pageToken"] = token
        data = _yt_get("playlistItems", key, **params)
        for it in data.get("items", []):
            yield it["contentDetails"]["videoId"]
        token = data.get("nextPageToken")
        if not token:
            break

def fetch_details(key: str, video_ids: list) -> list:
    out = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        data = _yt_get(
            "videos", key,
            part="snippet,contentDetails,statistics",
            id=",".join(chunk),
        )
        out.extend(data.get("items", []))
    return out


# ────────────────────────────────────────────────────────────────────
# 5. 메인
# ────────────────────────────────────────────────────────────────────
def build_rows(key: str, since_dt, keep_all: bool) -> list:
    rows = []
    for cid, cname in CHANNELS.items():
        print(f"[수집] {cname} ({cid}) …")
        uploads = get_uploads_playlist(key, cid)
        vids = list(iter_video_ids(key, uploads))
        print(f"   업로드 영상 {len(vids)}개 발견, 상세 조회 중…")
        for v in fetch_details(key, vids):
            sn, cd, st = v["snippet"], v["contentDetails"], v.get("statistics", {})
            published = sn["publishedAt"]
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if since_dt and pub_dt < since_dt:
                continue

            title = sn.get("title", "")
            desc = sn.get("description", "")
            tags = sn.get("tags", []) or []
            blob = " ".join([title, desc, " ".join(tags)])

            pension = is_pension(blob)
            if not pension and not keep_all:
                continue

            dur = parse_duration(cd.get("duration", ""))
            primary, secondary = categorize(blob)

            rows.append({
                "video_id": v["id"],
                "channel_id": cid,
                "channel_name": cname,
                "title": title,
                "description": desc[:2000],          # 주요 내용 (앞 2000자)
                "published_at": published,
                "duration_seconds": dur,
                "duration_hms": hms(dur),
                "is_short": 1 if dur <= 60 else 0,    # 숏츠 추정(≤60초)
                "view_count": int(st.get("viewCount", 0)),
                "like_count": int(st.get("likeCount", 0)),
                "comment_count": int(st.get("commentCount", 0)),
                "tags": ", ".join(tags),
                "is_pension": 1 if pension else 0,
                "category_primary": primary,
                "category_secondary": ", ".join(secondary),
                "target_audience": first_match(blob, AUDIENCE_KEYWORDS, "일반"),
                "format_type": first_match(blob, FORMAT_KEYWORDS, "기타"),
                "thumbnail_url": sn.get("thumbnails", {}).get("high", {}).get("url", ""),
                "video_url": f"https://www.youtube.com/watch?v={v['id']}",
                "collected_at": datetime.now(timezone.utc).isoformat(),
            })
    return rows


SCHEMA = """
CREATE TABLE IF NOT EXISTS pension_videos (
    video_id           TEXT PRIMARY KEY,
    channel_id         TEXT,
    channel_name       TEXT,
    title              TEXT,
    description        TEXT,
    published_at       TEXT,
    duration_seconds   INTEGER,
    duration_hms       TEXT,
    is_short           INTEGER,
    view_count         INTEGER,
    like_count         INTEGER,
    comment_count      INTEGER,
    tags               TEXT,
    is_pension         INTEGER,
    category_primary   TEXT,
    category_secondary TEXT,
    target_audience    TEXT,
    format_type        TEXT,
    thumbnail_url      TEXT,
    video_url          TEXT,
    collected_at       TEXT
);
"""

def save_sqlite(rows, path="pension_videos.db"):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    cols = list(rows[0].keys())
    ph = ",".join("?" * len(cols))
    con.executemany(
        f"INSERT OR REPLACE INTO pension_videos ({','.join(cols)}) VALUES ({ph})",
        [tuple(r[c] for c in cols) for r in rows],
    )
    con.commit()
    con.close()
    print(f"[저장] SQLite → {path}  ({len(rows)} rows)")

def save_tabular(rows):
    if not HAS_PANDAS:
        print("[건너뜀] pandas 미설치 → CSV/Excel 생략 (pip install pandas openpyxl)")
        return
    df = pd.DataFrame(rows)
    df.to_csv("pension_videos.csv", index=False, encoding="utf-8-sig")
    print(f"[저장] CSV → pension_videos.csv")
    try:
        with pd.ExcelWriter("pension_videos.xlsx", engine="openpyxl") as xl:
            df.to_excel(xl, sheet_name="videos", index=False)
            # 채널 × 카테고리 피벗 요약 시트
            pension_df = df[df["is_pension"] == 1]
            if not pension_df.empty:
                pivot = pension_df.pivot_table(
                    index="category_primary", columns="channel_name",
                    values="video_id", aggfunc="count", fill_value=0,
                )
                pivot.to_excel(xl, sheet_name="요약_카테고리별")
        print(f"[저장] Excel → pension_videos.xlsx (videos + 요약 시트)")
    except Exception as e:
        print(f"[경고] Excel 저장 실패: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="연금 외 영상도 모두 수집(is_pension 플래그만 표기)")
    ap.add_argument("--since", help="이 날짜 이후 업로드만 (YYYY-MM-DD)")
    args = ap.parse_args()

    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        sys.exit("환경변수 YOUTUBE_API_KEY 가 필요합니다.")

    since_dt = None
    if args.since:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    rows = build_rows(key, since_dt, args.all)
    if not rows:
        sys.exit("수집된 영상이 없습니다.")
    print(f"\n총 {len(rows)}개 행 수집 "
          f"(연금 관련 {sum(r['is_pension'] for r in rows)}개)")
    save_sqlite(rows)
    save_tabular(rows)
    print("\n완료. SQLite/CSV/Excel 확인하세요.")


if __name__ == "__main__":
    main()
