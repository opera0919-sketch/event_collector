#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
youtube_pension_db.py
=====================
3개 증권사 유튜브 채널의 연금 관련 영상 수집 → xlsx 출력 파이프라인.

대상 채널:
  - 삼성증권               : @samsungsecurities
  - 투자와연금             : @investpension
  - 한국인의연금_한국투자증권: UCxG-WFQqKbV49KIs5S9T0wA

실행:
  export YOUTUBE_API_KEY="발급받은_키"
  python youtube_pension_db.py                  # 연금 관련 영상만 (기본)
  python youtube_pension_db.py --all            # 전체 영상 수집
  python youtube_pension_db.py --since 2023-01-01
"""

import os
import re
import sys
import sqlite3
import argparse
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

YT_API_BASE = "https://www.googleapis.com/youtube/v3"


# ────────────────────────────────────────────────────────────────────
# 1. 대상 채널 설정
#    handle → 실행 시 channel_id 로 자동 해석
#    id     → 직접 사용
# ────────────────────────────────────────────────────────────────────
CHANNELS = [
    {"handle": "@samsungsecurities", "name": "삼성증권"},
    {"handle": "@investpension",     "name": "투자와연금"},
    {"id":     "UCxG-WFQqKbV49KIs5S9T0wA", "name": "한국인의연금_한국투자증권"},
]


# ────────────────────────────────────────────────────────────────────
# 2. 연금 관련 키워드
# ────────────────────────────────────────────────────────────────────
PENSION_KEYWORDS = [
    "연금", "퇴직연금", "개인연금", "연금저축", "연저펀", "IRP", "DC형", "DB형",
    "노후", "은퇴", "연금ETF", "TDF", "연금계좌", "연금수령", "세액공제",
    "과세이연", "ISA",
]

THEME_PRIORITY = [
    ("중도인출/중도해지", [
        "중도인출", "중도해지", "해지환급금", "기타소득세", "조기인출", "페널티",
    ]),
    ("연금인출", [
        "연금인출", "인출 전략", "인출 방법", "인출 순서", "부분 인출",
    ]),
    ("연금수령", [
        "연금수령", "수령 방법", "수령 시기", "연금화", "종신연금", "확정연금",
        "현금흐름", "월 배당", "55세", "연금 개시",
    ]),
    ("연금운용", [
        "연금ETF", "TDF", "포트폴리오", "연금 운용", "리밸런싱", "자산배분",
        "디폴트옵션", "수익률",
    ]),
    ("연금납입", [
        "납입", "적립", "납입한도", "추가납입", "300만원", "900만원", "1800만원",
    ]),
    ("연금세제", [
        "세액공제", "과세이연", "절세", "분리과세", "연금소득세", "연말정산",
        "ISA", "비과세",
    ]),
    ("연금제도", [
        "연금이란", "3층연금", "국민연금", "연금 종류", "IRP란", "연금저축이란",
        "퇴직연금이란",
    ]),
    ("기타 (글로벌/해외주식)", [
        "해외주식", "미국주식", "나스닥", "S&P", "해외 ETF",
    ]),
    ("기타 (경제/시장전망)", [
        "시장 전망", "경제 전망", "금리", "인플레이션", "환율",
    ]),
    ("기타 (부동산)", [
        "부동산", "아파트", "청약", "리츠",
    ]),
    ("기타 (노후라이프)", [
        "인생2막", "노후 생활", "은퇴 생활", "상속", "건강보험료",
    ]),
    ("기타 (퇴직절차/급여)", [
        "퇴직금", "퇴직급여", "희망퇴직",
    ]),
    ("기타 (이벤트/홍보)", [
        "이벤트", "프로모션", "계좌개설",
    ]),
]


# ────────────────────────────────────────────────────────────────────
# 3. 유틸
# ────────────────────────────────────────────────────────────────────
_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

def parse_duration(iso: str) -> int:
    if not iso:
        return 0
    m = _DUR_RE.fullmatch(iso)
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s

def hms(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _has_keyword(text: str, keywords: list) -> bool:
    t = text.lower()
    return any(kw.lower() in t for kw in keywords)

def is_pension(text: str) -> bool:
    return _has_keyword(text, PENSION_KEYWORDS)

def get_theme(text: str) -> str:
    for label, kws in THEME_PRIORITY:
        if _has_keyword(text, kws):
            return label
    return "기타"

def summarize_description(text: str, max_len: int = 300) -> str:
    """URL 제거 후 첫 문단을 max_len 자 이내로 반환."""
    text = re.sub(r"https?://\S+", "", text).strip()
    text = re.sub(r"\s{2,}", " ", text.replace("\n", " ")).strip()
    first = text.split("  ")[0] if "  " in text else text
    # 문단 구분 시도 (마침표/줄바꿈 기준)
    for sep in ["\n\n", ". ", "。"]:
        if sep in first:
            first = first.split(sep)[0]
            break
    return first[:max_len].strip()


# ────────────────────────────────────────────────────────────────────
# 4. YouTube Data API
# ────────────────────────────────────────────────────────────────────
def _yt_get(endpoint: str, key: str, **params) -> dict:
    resp = requests.get(
        f"{YT_API_BASE}/{endpoint}",
        params={"key": key, **params},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()

def resolve_handle(key: str, handle: str) -> str:
    """@handle → channel_id"""
    h = handle.lstrip("@")
    data = _yt_get("channels", key, part="id", forHandle=h)
    items = data.get("items", [])
    if not items:
        raise RuntimeError(f"채널 핸들을 찾을 수 없음: {handle}")
    return items[0]["id"]

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
        data = _yt_get("videos", key,
                       part="snippet,contentDetails,statistics",
                       id=",".join(chunk))
        out.extend(data.get("items", []))
    return out


# ────────────────────────────────────────────────────────────────────
# 5. 수집 메인
# ────────────────────────────────────────────────────────────────────
def build_rows(key: str, since_dt, keep_all: bool) -> list:
    rows = []

    # handle → id 해석
    channels = []
    for ch in CHANNELS:
        if "handle" in ch:
            print(f"[핸들 해석] {ch['handle']} …", end=" ", flush=True)
            cid = resolve_handle(key, ch["handle"])
            print(f"→ {cid}")
            channels.append({"id": cid, "name": ch["name"]})
        else:
            channels.append(ch)

    for ch in channels:
        cid, cname = ch["id"], ch["name"]
        print(f"[수집] {cname} ({cid}) …")
        uploads = get_uploads_playlist(key, cid)
        vids = list(iter_video_ids(key, uploads))
        print(f"   업로드 영상 {len(vids)}개 발견, 상세 조회 중…")

        for v in fetch_details(key, vids):
            sn = v["snippet"]
            cd = v["contentDetails"]
            st = v.get("statistics", {})

            published = sn["publishedAt"]
            pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if since_dt and pub_dt < since_dt:
                continue

            title = sn.get("title", "")
            desc  = sn.get("description", "")
            tags  = sn.get("tags", []) or []
            blob  = " ".join([title, desc, " ".join(tags)])

            pension = is_pension(blob)
            if not pension and not keep_all:
                continue

            dur = parse_duration(cd.get("duration", ""))

            rows.append({
                "video_id":         v["id"],
                "channel_name":     cname,
                "title":            title,
                "published_at":     published[:10],          # YYYY-MM-DD
                "duration":         hms(dur),
                "is_short":         "숏츠" if dur <= 60 else "",
                "view_count":       int(st.get("viewCount", 0)),
                "like_count":       int(st.get("likeCount", 0)),
                "comment_count":    int(st.get("commentCount", 0)),
                "is_pension":       1 if pension else 0,
                "theme":            get_theme(blob) if pension else "",
                "tags":             ", ".join(tags),
                "description":      summarize_description(desc),
                "video_url":        f"https://www.youtube.com/watch?v={v['id']}",
                "thumbnail_url":    sn.get("thumbnails", {}).get("high", {}).get("url", ""),
            })

    return rows


# ────────────────────────────────────────────────────────────────────
# 6. 저장
# ────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS pension_videos (
    video_id       TEXT PRIMARY KEY,
    channel_name   TEXT,
    title          TEXT,
    published_at   TEXT,
    duration       TEXT,
    is_short       TEXT,
    view_count     INTEGER,
    like_count     INTEGER,
    comment_count  INTEGER,
    is_pension     INTEGER,
    theme          TEXT,
    tags           TEXT,
    description    TEXT,
    video_url      TEXT,
    thumbnail_url  TEXT
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

def save_excel(rows):
    if not HAS_PANDAS:
        print("[건너뜀] pandas 미설치 → pip install pandas openpyxl")
        return
    df = pd.DataFrame(rows)

    # 컬럼 순서 및 한글 헤더
    col_map = {
        "channel_name":  "채널명",
        "title":         "제목",
        "published_at":  "업로드일",
        "duration":      "길이",
        "is_short":      "숏츠여부",
        "view_count":    "조회수",
        "like_count":    "좋아요",
        "comment_count": "댓글수",
        "is_pension":    "연금관련",
        "theme":         "테마",
        "tags":          "태그",
        "description":   "설명요약",
        "video_url":     "영상URL",
        "thumbnail_url": "썸네일URL",
        "video_id":      "video_id",
    }
    df = df[[c for c in col_map if c in df.columns]]
    df = df.rename(columns=col_map)

    csv_path = "pension_videos.csv"
    xlsx_path = "pension_videos.xlsx"

    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"[저장] CSV → {csv_path}")

    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as xl:
            df.to_excel(xl, sheet_name="videos", index=False)

            # 채널별 요약 시트
            summary = (
                df.groupby("채널명")
                  .agg(
                      총영상수=("video_id", "count"),
                      연금관련수=("연금관련", "sum"),
                      평균조회수=("조회수", "mean"),
                  )
                  .round(0)
                  .astype({"평균조회수": int})
            )
            summary.to_excel(xl, sheet_name="채널별요약")

        print(f"[저장] Excel → {xlsx_path}  (videos + 채널별요약 시트)")
    except Exception as e:
        print(f"[경고] Excel 저장 실패: {e}")


# ────────────────────────────────────────────────────────────────────
# 7. 엔트리포인트
# ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="연금 외 영상도 수집 (is_pension 플래그 표기)")
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

    pension_count = sum(r["is_pension"] for r in rows)
    print(f"\n총 {len(rows)}개 수집 (연금 관련 {pension_count}개)")

    save_sqlite(rows)
    save_excel(rows)
    print("\n완료.")


if __name__ == "__main__":
    main()
