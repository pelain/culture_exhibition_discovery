#!/usr/bin/env python3
"""전시 피드 빌더: 공개 데이터 → feed.json (앱이 받아 쓰는 정적 파일 하나).

    python3 tools/build_feed.py              # 내려받은 원본은 tools/cache 에 캐시
    python3 tools/build_feed.py --refresh    # 캐시를 무시하고 최신 데이터로
    python3 tools/build_feed.py --max-pages 3   # 문화포털 목록을 앞쪽 몇 쪽만 (빠른 확인용)

표준 라이브러리만 쓴다. 결과는 feed/ 와 app/src/main/assets/ (앱 내장 스냅숏) 두 곳에 쓴다: feed.json (전시, 국보·보물), programs.json (체험·교육 프로그램).

출처
- 국보·보물 목록, 소장처, 좌표: 국가유산청 Open API (키 불필요)
- 체험·교육 프로그램: 국가유산청 행사 Open API (키 불필요)
- 전국 전시 일정: 문화포털 '한눈에 보는 문화정보' 목록 (키 불필요, HTML)
- 국립중앙박물관 특별전·테마전: 박물관 누리집 (키 불필요, HTML)
- (선택) 공공데이터포털 '한눈에보는문화정보조회서비스': tools/.env 의 DATA_GO_KR_KEY 가 있으면 함께 쓴다. get_key.md 참고.
- 사람이 고친 값: tools/curated.json (한정 공개 표시, 출품 국보·보물, 빠진 전시 추가)
- 지역 테마 코스: tools/themes.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
CACHE = TOOLS / "cache"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
KST = dt.timezone(dt.timedelta(hours=9))

KHS_LIST = "https://www.khs.go.kr/cha/SearchKindOpenapiList.do"
KHS_EVENTS = "https://www.khs.go.kr/cha/openapi/selectEventListOpenapi.do"
PORTAL = "https://www.culture.go.kr"
PORTAL_LIST = PORTAL + "/portal/cltInfo/oneCltInfo/oneEyeDetailView.do?viewType=CONTBODY"
PORTAL_VIEW = PORTAL + "/portal/cltInfo/oneCltInfo/oneCltInfoView.do?menuNo=200010&pblprfrSn="
NMK = "https://www.museum.go.kr"
NMK_LISTS = [
    NMK + "/MUSEUM/contents/M0202010000.do?menuId=current",
    NMK + "/MUSEUM/contents/M0202020000.do?menuId=upcomming",
]
DATA_GO_KR = "https://apis.data.go.kr/B553457/cultureinfo/period2"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

#: 끝난 뒤 이 날수까지는 피드에 남긴다 (날짜 검색에서 "그 전에 끝난 전시"로 보인다).
KEEP_ENDED_DAYS = 45
#: 전시 기간이 이 날수 이하면 '단기 공개'로 표시한다.
SHORT_DAYS = 31

# 역사·유물 전시로 볼 단서. 장소 이름이나 제목에 하나라도 있으면 남긴다.
HISTORY_WORDS = (
    "박물관", "기념관", "역사관", "역사문화", "유물", "사료관", "자료관", "향토", "고궁", "궁궐", "국가유산", "문화유산",
    "문화재", "왕릉", "서원", "향교", "고분", "유적", "민속", "국보", "실록", "의궤", "고미술", "서화", "청자", "백자",
    "불교", "사찰", "백제", "신라", "가야", "고구려", "고려시대", "고려청자", "조선시대", "조선왕", "선사시대", "출토",
    "독립운동",
)
# 역사와 거리가 먼 박물관·전시. 위 단서가 있어도 뺀다.
NOT_HISTORY_WORDS = (
    "과학", "자연사", "로봇", "만화", "애니메이션", "장난감", "곤충", "공룡", "천문", "우주", "철도", "자동차", "항공",
    "해양생물", "수족관", "화폐", "우표", "커피", "초콜릿", "트릭아트", "미디어아트", "사진전", "어린이박물관", "어린이 박물관",
    "현대미술", "디자인", "모집", "공모", "교육생", "수강생", "체험 프로그램", "개인전", "예비예술가", "비엔날레", "영화",
    "공연예술", "무용", "온라인", "짜장면", "건축가",
)

#: 국가유산청 목록의 관리자 이름 → 관람객이 아는 전시 장소 이름.
HOLDER_ALIAS = {"삼성문화재단": "리움미술관", "서울대학교규장각 한국학연구원": "서울대학교 규장각"}

SIDO_SHORT = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천", "광주광역시": "광주",
    "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종", "경기도": "경기", "강원특별자치도": "강원",
    "강원도": "강원", "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북", "전라북도": "전북",
    "전라남도": "전남", "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주",
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── 내려받기 / 캐시 ────────────────────────────────────────────────────────────

class Fetcher:
    """같은 주소는 한 번만 받는다. 호스트마다 요청 사이를 띄워 서버에 부담을 주지 않는다."""

    def __init__(self, refresh: bool):
        self.refresh = refresh
        self.last: dict[str, float] = {}
        CACHE.mkdir(parents=True, exist_ok=True)

    def get(self, url: str, data: dict | None = None, *, ttl_hours: float, gap: float = 0.4, headers: dict | None = None) -> str:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        key = hashlib.sha1((url + "|" + (body or b"").decode()).encode()).hexdigest()[:20]
        path = CACHE / f"{urllib.parse.urlparse(url).hostname}-{key}.txt"
        if not self.refresh and path.exists() and time.time() - path.stat().st_mtime < ttl_hours * 3600:
            return path.read_text(encoding="utf-8")
        host = urllib.parse.urlparse(url).hostname or ""
        wait = gap - (time.time() - self.last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, **(headers or {})})
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=40) as res:
                    text = res.read().decode("utf-8", errors="replace")
                self.last[host] = time.time()
                path.write_text(text, encoding="utf-8")
                return text
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = e
                time.sleep(1.5 * (attempt + 1))
        # 새로 못 받으면 오래된 캐시라도 쓴다. 하루치 실패로 피드 전체가 비는 것보다 낫다.
        if path.exists():
            log(f"  ! {host} 응답 없음, 이전 캐시 사용: {last_error}")
            return path.read_text(encoding="utf-8")
        raise RuntimeError(f"{url} 을(를) 받지 못했다: {last_error}")


def strip_tags(s: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", s, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_date(s: str) -> dt.date | None:
    m = re.search(r"(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", s or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_period(s: str) -> tuple[dt.date, dt.date] | None:
    parts = re.split(r"\s*[~∼-]\s*(?=\d{4})", s.strip(), maxsplit=1)
    start = parse_date(parts[0])
    end = parse_date(parts[1]) if len(parts) > 1 else start
    if not start or not end or end < start:
        return None
    return start, end


# ── 국가유산청: 국보·보물 목록 ──────────────────────────────────────────────────

def khs_catalog(f: Fetcher, kinds: tuple[tuple[str, str], ...] = (("11", "국보"), ("12", "보물"))) -> list[dict]:
    items: list[dict] = []
    for code, kind in kinds:
        page = 1
        while True:
            xml = f.get(f"{KHS_LIST}?ccbaKdcd={code}&pageUnit=500&pageIndex={page}", ttl_hours=24 * 7)
            root = ET.fromstring(xml.strip())
            rows = root.findall("item")
            for it in rows:
                def t(tag: str) -> str:
                    return (it.findtext(tag) or "").strip()
                if t("ccbaCncl") == "Y":  # 지정 해제
                    continue
                try:
                    lat, lon = float(t("latitude") or 0), float(t("longitude") or 0)
                except ValueError:
                    lat = lon = 0.0
                items.append({
                    # 지정 종목·번호·시도 코드를 이은 값. 앱이 도장 기록의 열쇠로 쓰므로 바뀌면 안 된다.
                    "id": f"{code}.{int(t('ccbaAsno') or 0)}.{t('ccbaCtcd')}",
                    "kind": kind, "name": t("ccbaMnm1"), "admin": t("ccbaAdmin"),
                    "sido": t("ccbaCtcdNm"), "sigungu": t("ccsiName"),
                    "lat": lat if 33 < lat < 39.5 else 0.0, "lon": lon if 124 < lon < 132 else 0.0,
                })
            total = int(root.findtext("totalCnt") or 0)
            if not rows or page * 500 >= total:
                break
            page += 1
    log("국가유산청: " + ", ".join(f"{kind} {sum(i['kind'] == kind for i in items)}건" for _, kind in kinds))
    return items


# ── 국가유산청: 체험·교육 프로그램 ──────────────────────────────────────────────

#: 프로그램을 이번 달부터 몇 달치 받을지.
PROGRAM_MONTHS = 3
#: 한 프로그램에 실을 회차 날짜의 최대 수.
PROGRAM_DATES_MAX = 12

# 프로그램 종류를 가르는 낱말. 위에서부터 먼저 맞는 것으로 정한다 (체험과 교육이 이 탭의 중심이라 먼저 본다).
PROGRAM_KINDS = (
    ("체험", ("체험", "만들기", "공예", "다도", "다례", "한복", "탁본", "활쏘기", "염색", "떡", "전통놀이", "스테이", "캠프", "투어", "탐방", "걷기")),
    ("교육", ("교육", "강연", "강좌", "강의", "인문학", "아카데미", "학교", "교실", "답사", "해설", "특강", "토크", "수업", "배움")),
    ("공연", ("공연", "음악회", "콘서트", "국악", "연주", "판소리", "마당극", "무용", "버스킹")),
    ("축제", ("축제", "문화제", "야행", "페스티벌", "한마당", "잔치", "재현")),
)


def program_kind(title: str, content: str) -> str:
    # 제목에 드러난 종류를 내용보다 먼저 본다 ('○○ 음악회'의 내용에 '체험 부스'가 있어도 공연이다).
    for text in (title, content):
        for kind, words in PROGRAM_KINDS:
            if any(w in text for w in words):
                return kind
    return "기타"


def region_of(name: str) -> str:
    """시도 이름을 국보·보물 목록과 같은 짧은 표기로 바꾼다. 그 목록이 광주와 전남을 '전남광주'로 묶으므로 여기서도 묶는다."""
    short = SIDO_SHORT.get(name, name)
    return "전남광주" if short in ("광주", "전남") or "전남광주" in name else short


def khs_programs(f: Fetcher, today: dt.date, spots: list[tuple[str, str, float, float]]) -> list[dict]:
    """국가유산 활용 프로그램(체험, 교육, 공연, 축제). 키가 필요 없다.

    출처는 회차마다 한 줄씩 준다 (같은 프로그램이 날짜만 달리해 수십 번 나온다). 제목·장소·시군구가 같으면 한 프로그램으로
    묶고 남은 회차 날짜를 모은다.

    응답에 좌표가 없다. 장소 이름에 우리가 아는 유적·박물관 이름([spots]: 이름, 시도, 위도, 경도)이 들어 있고 시도가 같으면
    그 좌표를 쓰고, 아니면 시군구의 중심 좌표를 쓰되 어림값이라고 표시한다 (앱이 거리를 '약 ○km'로 적는다).
    """
    # '안동 도산서원' → '도산서원' 처럼 앞의 지역 이름을 뗀 이름으로도 찾는다. 너무 짧은 이름은 엉뚱한 곳에 걸리므로 뺀다.
    index: dict[str, list[tuple[str, float, float]]] = {}
    for name, sido, lat, lon in spots:
        for key in {norm(name), norm(re.sub(r"^\S+\s+", "", name))}:
            if len(key) >= 4:
                index.setdefault(key, []).append((sido, lat, lon))

    groups: dict[tuple, dict] = {}
    months = [(today.year + (today.month - 1 + i) // 12, (today.month - 1 + i) % 12 + 1) for i in range(PROGRAM_MONTHS)]
    for year, month in months:
        xml = f.get(f"{KHS_EVENTS}?searchYear={year}&searchMonth={month}", ttl_hours=24)
        for it in ET.fromstring(xml.strip()).findall("item"):
            def t(tag: str) -> str:
                return re.sub(r"\s+", " ", html.unescape(it.findtext(tag) or "")).strip()
            start, end = parse_date(t("sDate")), parse_date(t("eDate"))
            title = t("subTitle")
            if not title or not start or not end or end < start or end < today:
                continue
            # 끝나는 날이 몇 해 뒤로 잘못 적힌 것이 있다. 한 회차가 한 해를 넘기지는 않는다고 보고 시작일로 맞춘다.
            if (end - start).days > 366:
                end = start
            key = (norm(title), norm(t("subDesc")), t("gugun"))
            g = groups.get(key)
            if g is None:
                url = t("subPath")
                g = groups[key] = {
                    # 앱이 참여 도장의 열쇠로 쓰므로, 받을 때마다 바뀌는 출처의 회차 번호 대신 묶는 기준(제목·장소·시군구)으로 만든다.
                    "id": "p" + hashlib.sha1("|".join(key).encode()).hexdigest()[:10], "title": title, "kind": program_kind(title, t("subContent")),
                    "desc": t("subContent")[:240], "schedule": t("subDate")[:160], "place": t("subDesc"),
                    "sido": region_of(t("sido")), "sidoFull": t("sido"), "gugun": t("gugun"),
                    "organizer": t("groupName"), "contact": t("contact"), "target": t("subDesc_2")[:80], "fee": t("subDesc_3")[:80],
                    "url": url if url.startswith("http") else "", "sessions": set(),
                }
            g["sessions"].add((start, end))

    # 시군구 중심 좌표 (어림값). 시군구 수만큼만 지오코더를 부르고 오래 캐시한다.
    centers: dict[tuple[str, str], tuple[float, float]] = {}

    def center(sido_full: str, gugun: str) -> tuple[float, float]:
        k = (sido_full, gugun)
        if k not in centers:
            # 새 통합 시도 이름은 지도 자료에 아직 없을 수 있어, 못 찾으면 시군구 이름만으로 다시 찾는다.
            centers[k] = (geocode(f, f"{gugun}, {sido_full}") if gugun else None) or (geocode(f, gugun) if gugun else None) or (0.0, 0.0)
        return centers[k]

    programs = []
    for g in groups.values():
        sessions = sorted(g.pop("sessions"))
        flat = norm(g["place"])
        hits = [(k, v) for k, v in index.items() if k in flat]
        lat = lon = 0.0
        if hits:
            # 가장 긴 이름이 가장 구체적이다. 같은 이름이 여러 시도에 있으면 프로그램과 같은 시도의 것만 쓴다.
            _, cands = max(hits, key=lambda kv: len(kv[0]))
            same = [c for c in cands if region_of(c[0]) == g["sido"]]
            if same and len({(round(c[1], 2), round(c[2], 2)) for c in same}) == 1:
                lat, lon = same[0][1], same[0][2]
        approx = not lat
        if approx:
            lat, lon = center(g.pop("sidoFull"), g["gugun"])
        g.pop("sidoFull", None)
        programs.append({
            **g, "start": sessions[0][0].isoformat(), "end": max(e for _, e in sessions).isoformat(),
            # 하루짜리 회차가 여럿이면 남은 날짜를 적는다 (기간으로 열리는 프로그램은 start~end 로 충분하다).
            "dates": [st.isoformat() for st, en in sessions if st == en][:PROGRAM_DATES_MAX] if len(sessions) > 1 else [],
            "lat": round(lat, 6), "lon": round(lon, 6), "approx": approx,
        })
    programs.sort(key=lambda p: (p["start"], p["title"]))
    kinds = ", ".join(f"{k} {sum(p['kind'] == k for p in programs)}" for k in ("체험", "교육", "공연", "축제", "기타"))
    log(f"국가유산청 프로그램: {len(programs)}건 ({kinds}), 좌표: 장소 {sum(1 for p in programs if p['lat'] and not p['approx'])}건,"
        f" 시군구 어림 {sum(1 for p in programs if p['lat'] and p['approx'])}건, 없음 {sum(1 for p in programs if not p['lat'])}건")
    return programs


# ── 문화포털: 전국 전시 목록 ────────────────────────────────────────────────────

def is_history(title: str, place: str) -> bool:
    text = f"{title} {place}".replace("문화재단", "")
    if any(w in text for w in NOT_HISTORY_WORDS):
        return False
    return any(w in text for w in HISTORY_WORDS)


def portal_exhibitions(f: Fetcher, today: dt.date, max_pages: int) -> list[dict]:
    out: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        text = f.get(
            PORTAL_LIST,
            {"pageIndex": page, "menuNo": "200010", "sGubun": "D", "searchKeyword": "", "genreCds": "06"},
            ttl_hours=12,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": PORTAL + "/portal/cltInfo/oneCltInfo/list.do?menuNo=200010"},
        )
        blocks = text.split('<li class="list">')[1:]  # 항목 안에 <li> 가 또 있어 정규식으로 닫는 태그를 찾을 수 없다
        if not blocks:
            break
        fresh = 0
        for b in blocks:
            sn = re.search(r"pblprfrSn=(\d+)", b)
            title = re.search(r'class="tit[^"]*"[^>]*>(.*?)</span>', b, flags=re.S)
            if not sn or not title:
                continue
            fields = {strip_tags(k): strip_tags(v) for k, v in re.findall(r'<dt class="term">(.*?)</dt>\s*<dd class="desc">(.*?)</dd>', b, flags=re.S)}
            tags = [strip_tags(x) for x in re.findall(r'<li class="tag[^"]*">(.*?)</li>', b, flags=re.S)]
            img = re.search(r'<img src="([^"]+)"', b)
            if sn.group(1) not in out:
                fresh += 1
            out[sn.group(1)] = {
                "sn": sn.group(1), "title": strip_tags(title.group(1)), "place": fields.get("장소", ""),
                "period": fields.get("기간", ""), "tags": tags, "image": PORTAL + img.group(1) if img and img.group(1).startswith("/") else "",
            }
        if fresh == 0:  # 마지막 쪽을 넘기면 같은 목록이 되풀이된다
            break
    log(f"문화포털: 전시 {len(out)}건 수집")

    result = []
    for row in out.values():
        period = parse_period(row["period"])
        if not period or period[1] < today - dt.timedelta(days=KEEP_ENDED_DAYS):
            continue
        if not is_history(row["title"], row["place"]):
            continue
        detail = f.get(PORTAL_VIEW + row["sn"], ttl_hours=24 * 14, gap=0.6)
        def hidden(name: str) -> str:
            m = re.search(rf'id="{name}"[^>]*value="([^"]*)"', detail) or re.search(rf'value="([^"]*)"[^>]*id="{name}"', detail)
            return html.unescape(m.group(1)).strip() if m else ""
        try:
            lat, lon = float(hidden("prfplcLat") or 0), float(hidden("prfplcLot") or 0)
        except ValueError:
            lat = lon = 0.0
        body = strip_tags(detail[detail.find("section-view-cont"):]) if "section-view-cont" in detail else ""
        addr = re.search(r"주소\s+(.*?)\s+(?:문의|홈페이지|목록)", body)
        home = re.search(r"홈페이지\s+(https?://\S+)", body)
        fee = next((t for t in row["tags"] if t in ("무료", "유료", "할인")), "")
        price = re.search(r'<dt class="head">\s*가격\s*</dt>\s*<dd class="desc">(.*?)</dd>', detail, flags=re.S)
        fee_text = strip_tags(price.group(1))[:120] if price else ""
        if not fee and fee_text:
            fee = "무료" if fee_text.startswith("무료") else "유료" if re.search(r"\d\s*원", fee_text) else ""
        result.append({
            "id": "culture-" + row["sn"], "source": "문화포털", "title": row["title"], "venue": row["place"],
            "place": "", "start": period[0], "end": period[1], "fee": fee, "feeText": fee_text, "image": row["image"],
            "url": PORTAL_VIEW + row["sn"], "homepage": home.group(1) if home else "",
            "address": addr.group(1) if addr else "", "lat": lat if 33 < lat < 39.5 else 0.0, "lon": lon if 124 < lon < 132 else 0.0,
            "text": body[:3000],
        })
    log(f"문화포털: 역사·유물 전시 {len(result)}건")
    return result


# ── 국립중앙박물관: 특별전·테마전 ───────────────────────────────────────────────

def nmk_fee(text: str) -> tuple[str, str]:
    """안내 글에서 (무료/유료, 요금 설명) 을 찾는다. 정해진 항목이 없어 글의 표현으로 판단하고, 모르면 빈 값으로 둔다.

    유료 전시에도 '무료 입장 대상', '연휴 무료입장' 같은 말이 나오므로 요금표가 있으면 유료를 먼저 본다.
    """
    table = re.search(r"(?:입장료|관람료|관람요금)(.{0,160})", text, flags=re.S)
    if table:
        part = table.group(1)
        prices = []
        for label in ("성인", "어린이 및 청소년", "청소년", "어린이"):
            m = re.search(label + r"\s*(?:\([^)]*\))?\s*([\d,]+)\s*원", part)
            if m and not any(label in p for p in prices):
                prices.append(f"{label} {m.group(1)}원")
        if prices:
            return "유료", " / ".join(prices)
        if re.match(r"\s*[:：]?\s*무료", part):
            return "무료", ""
    if re.search(r"(?:티켓|입장권)[^.]{0,40}[\d,]{4,}\s*원", text):
        return "유료", ""
    if re.search(r"무료\s*입장입니다|무료로 관람|무료 관람하실", text):
        return "무료", ""
    return "", ""


def nmk_exhibitions(f: Fetcher, today: dt.date) -> list[dict]:
    result = []
    for url in NMK_LISTS:
        text = f.get(url, ttl_hours=12)
        for b in re.findall(r"<li>\s*<div class=\"img-box\">(.*?)</li>\s*(?=<li>\s*<div class=\"img-box\">|</ul>)", text, flags=re.S):
            eid = re.search(r"exhiSpThemId=(\d+)", b)
            title = re.search(r"<strong>(.*?)</strong>", b, flags=re.S)
            fields = {strip_tags(k): strip_tags(v) for k, v in re.findall(r"<li><strong>(.*?)</strong>\s*<p>(.*?)</p>", b, flags=re.S)}
            period = parse_period(fields.get("기간", ""))
            if not eid or not title or not period or period[1] < today - dt.timedelta(days=KEEP_ENDED_DAYS):
                continue
            labels = [strip_tags(x) for x in re.findall(r'<span class="label-type[^"]*">(.*?)</span>', b, flags=re.S)]
            img = re.search(r'<img src="(/afile/[^"]+)"', b)
            view = f"{NMK}/MUSEUM/contents/M0202010000.do?menuId=current&schM=view&exhiSpThemId={eid.group(1)}"
            detail = strip_tags(f.get(view, ttl_hours=24 * 7, gap=0.6))
            fee, fee_text = nmk_fee(detail)
            result.append({
                "id": "nmk-" + eid.group(1), "source": "국립중앙박물관", "title": strip_tags(title.group(1)),
                "venue": "국립중앙박물관", "place": fields.get("장소", ""), "start": period[0], "end": period[1],
                "fee": fee, "feeText": fee_text, "image": NMK + html.unescape(img.group(1)) if img else "", "url": view,
                "homepage": NMK, "address": "서울특별시 용산구 서빙고로 137", "lat": 0.0, "lon": 0.0,
                "kindLabel": next((x for x in labels if x in ("특별전", "테마전")), ""), "text": detail[:6000],
            })
    log(f"국립중앙박물관: 전시 {len(result)}건")
    return result


# ── 공공데이터포털 (선택, 키 필요) ──────────────────────────────────────────────

def load_env() -> dict[str, str]:
    env = dict(os.environ)
    path = TOOLS / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def datagokr_exhibitions(f: Fetcher, key: str, today: dt.date) -> list[dict]:
    """한국문화정보원 '한눈에보는문화정보조회서비스' (기간별). 좌표(gpsX, gpsY)가 응답에 들어 있다.

    2026-10-25 실제 키로 확인: 응답 항목 이름이 맞고, 1년 범위 2,015건 가운데 역사·유물 전시 75건이 좌표와 함께 들어온다.
    """
    result = []
    start = today - dt.timedelta(days=KEEP_ENDED_DAYS)
    end = today + dt.timedelta(days=365)
    for page in range(1, 60):
        q = urllib.parse.urlencode({
            "serviceKey": key, "PageNo": page, "numOfrows": 100, "serviceTp": "A",
            "from": start.strftime("%Y%m%d"), "to": end.strftime("%Y%m%d"),
        })
        root = ET.fromstring(f.get(f"{DATA_GO_KR}?{q}", ttl_hours=12).strip())
        rows = root.findall(".//item")
        if not rows:
            code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or ""
            if page == 1:
                log(f"공공데이터포털: 응답에 항목이 없다 (resultCode={code}). 키 승인 상태를 확인할 것.")
            break
        for it in rows:
            def t(tag: str) -> str:
                return (it.findtext(tag) or "").strip()
            s, e = parse_date(t("startDate")), parse_date(t("endDate"))
            if "전시" not in t("realmName") or not s or not e or not is_history(t("title"), t("place")):
                continue
            try:
                lat, lon = float(t("gpsY") or 0), float(t("gpsX") or 0)
            except ValueError:
                lat = lon = 0.0
            result.append({
                "id": "datagokr-" + t("seq"), "source": "공공데이터포털", "title": html.unescape(t("title")), "venue": t("place"),
                "place": "", "start": s, "end": e, "fee": "", "feeText": "", "image": t("thumbnail"), "url": "", "homepage": "",
                "address": f"{t('area')} {t('sigungu')}".strip(), "lat": lat if 33 < lat < 39.5 else 0.0,
                "lon": lon if 124 < lon < 132 else 0.0, "text": "",
            })
    log(f"공공데이터포털: 역사·유물 전시 {len(result)}건")
    return result


# ── 좌표 보완 ───────────────────────────────────────────────────────────────────

def geocode(f: Fetcher, query: str) -> tuple[float, float] | None:
    """OSM Nominatim. 결과는 오래(90일) 캐시해 같은 질의를 다시 보내지 않는다.

    이용 정책상 손으로 한 번 돌릴 때는 초당 1회까지지만, 매일 정해진 시각에 도는 작업(GitHub Actions)은 분당 4회까지다.
    그래서 GitHub Actions(환경 변수 CI 가 있다)에서는 15초 간격으로 부른다. 새 전시장이 많은 날은 그만큼 오래 걸린다.
    """
    q = urllib.parse.urlencode({"q": query, "format": "json", "limit": 1, "countrycodes": "kr", "accept-language": "ko"})
    gap = 15.0 if os.environ.get("CI") else 1.2
    try:
        rows = json.loads(f.get(f"{NOMINATIM}?{q}", ttl_hours=24 * 90, gap=gap,
                                headers={"User-Agent": "heritage-exhibition-feed/1.0 (build script)"}))
    except (RuntimeError, json.JSONDecodeError):
        return None
    if not rows:
        return None
    return float(rows[0]["lat"]), float(rows[0]["lon"])


# ── 합치기 ──────────────────────────────────────────────────────────────────────

def norm(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", s.lower())


def norm_item_name(name: str) -> str:
    """'금동미륵보살반가사유상(1962-1)' → '금동미륵보살반가사유상'. 지정 구분용 괄호와 권수 표기를 뗀다."""
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r"\s*권\s*[\d,~\s-]+$", "", name)
    return name.strip()


def tag_treasures(text: str, title: str, catalog_names: dict[str, tuple[str, str, str]]) -> tuple[list[dict], str]:
    """전시 글에서 국보·보물 단서를 찾는다. (출품 유물 목록, 뱃지 등급) 을 돌려준다.

    공개 API 에 출품 목록이 없어서 글에 적힌 이름과 '국보'·'보물' 표현으로 추정한다. 등급:
      "named"   지정 명칭이 글에 그대로 나온다
      "mention" '국보'·'보물 제○호' 같은 표현만 나온다
      ""        단서 없음
    """
    flat = norm(text + " " + title)
    found = [{"kind": kind, "name": name, "id": hid} for key, (kind, name, hid) in catalog_names.items() if key in flat]
    found.sort(key=lambda x: (x["kind"] != "국보", x["name"]))
    if found:
        return found[:12], "named"
    plain = f"{title} {text}"
    # '보물'은 비유로 흔히 쓰여서(보물찾기, 우리 동네 보물) 지정 표현일 때만 센다.
    national = re.search(r"국보(?!급)", plain)
    treasure = re.search(r"보물\s*(?:제\s*)?\d+\s*호|보물\s*[\[「<‘']|보물로 지정|보물 지정|국가지정\s*보물|보물\s*\d+\s*[건점]", plain)
    if national or treasure:
        return [], "mention"
    return [], ""


def build(args: argparse.Namespace) -> tuple[dict, dict]:
    today = dt.datetime.now(KST).date()
    f = Fetcher(args.refresh)
    env = load_env()

    catalog = khs_catalog(f)
    raw = nmk_exhibitions(f, today) + portal_exhibitions(f, today, args.max_pages)
    if env.get("DATA_GO_KR_KEY"):
        raw += datagokr_exhibitions(f, env["DATA_GO_KR_KEY"], today)
    else:
        log("공공데이터포털: DATA_GO_KR_KEY 가 없어 건너뜀 (get_key.md 참고)")

    curated = json.loads((TOOLS / "curated.json").read_text(encoding="utf-8"))
    for extra in curated.get("add", []):
        raw.append({
            "id": extra["id"], "source": "직접 입력", "title": extra["title"], "venue": extra["venue"],
            "place": extra.get("place", ""), "start": dt.date.fromisoformat(extra["start"]), "end": dt.date.fromisoformat(extra["end"]),
            "fee": extra.get("fee", ""), "feeText": extra.get("feeText", ""), "image": extra.get("image", ""), "url": extra.get("url", ""), "homepage": "",
            "address": extra.get("address", ""), "lat": extra.get("lat", 0.0), "lon": extra.get("lon", 0.0), "text": extra.get("note", ""),
        })

    # 소장처별 국보·보물. 박물관·미술관이 관리하는 것만 '소장'으로 본다 (사찰·개인 소장은 전시 장소가 아니다).
    holdings: dict[str, list[dict]] = {}
    for it in catalog:
        if re.search(r"박물관|미술관|문화재단|규장각|한국학중앙연구원|현담문고|기념관", it["admin"]):
            holdings.setdefault(norm(it["admin"]), []).append(it)

    # 지정 명칭 사전. 흔한 낱말과 겹치지 않게 충분히 긴 이름만 쓴다.
    catalog_names: dict[str, tuple[str, str, str]] = {}
    for it in catalog:
        name = norm_item_name(it["name"])
        key = norm(name)
        if len(key) >= 6 and (key not in catalog_names or it["kind"] == "국보"):
            catalog_names[key] = (it["kind"], name, it["id"])

    def locate(name: str, address: str = "") -> tuple[float, float]:
        """이름으로 좌표를 찾는다: 지오코더 → 소장 유물 좌표(여러 건이 한곳에 모여 있을 때만)."""
        key = norm(name)
        # '의령 의병박물관'처럼 지역 이름이 앞에 붙으면 지오코더가 못 찾는 일이 있어 뗀 이름으로도 찾는다.
        local = re.sub(r"^\S+\s+", "", name)
        usable = local != name and re.search(r"박물관|미술관|기념관|전시관", local)
        hit = geocode(f, name) or (geocode(f, local) if usable else None) or (geocode(f, address) if address else None)
        if hit:
            return hit
        # 국가유산청 좌표는 유물마다 기준이 달라(출토지인 경우도 있다) 서로 가까이 모인 경우에만 믿는다.
        pts = sorted((h["lat"], h["lon"]) for h in holdings.get(key, []) if h["lat"])
        if len(pts) >= 3:
            mid = pts[len(pts) // 2]
            near = sum(abs(p[0] - mid[0]) < 0.01 and abs(p[1] - mid[1]) < 0.01 for p in pts)
            if near * 2 > len(pts):
                return mid
        return 0.0, 0.0

    # 전시장 묶기: 이름이 같으면(정규화) 한 곳이다.
    venues: dict[str, dict] = {}
    by_name: dict[str, str] = {}

    def venue_for(e: dict) -> str | None:
        key = norm(e["venue"])
        if not key:
            return None
        if key in by_name:
            v = venues[by_name[key]]
            if not v["address"] and e["address"]:
                v["address"] = e["address"]
            if not v["homepage"] and e["homepage"]:
                v["homepage"] = e["homepage"]
            return v["id"]
        lat, lon = e["lat"], e["lon"]
        if not lat:
            lat, lon = locate(e["venue"], e["address"])
        if not lat:
            return None
        vid = "v" + hashlib.sha1(key.encode()).hexdigest()[:8]
        head = e["address"].split(" ")[0] if e["address"] else ""
        sido = SIDO_SHORT.get(head, head if head in SIDO_SHORT.values() else "")
        venues[vid] = {"id": vid, "name": e["venue"], "address": e["address"], "sido": sido,
                       "lat": round(lat, 6), "lon": round(lon, 6), "homepage": e["homepage"], "holdings": []}
        by_name[key] = vid
        return vid

    overrides = curated.get("override", {})
    exhibitions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for e in raw:
        o = overrides.get(e["id"], {})
        if o.get("hide"):
            continue
        # 순회전처럼 장소가 여러 박물관이면 박물관마다 따로 싣는다.
        outside = e["source"] == "국립중앙박물관" and e["place"] and not re.search(
            r"국립중앙박물관|상설전시|특별전시실|기획전시실|기증|서화실|\d층", e["place"])
        targets = [e]
        if outside:
            names = [n.strip() for n in re.split(r"[,、]", re.sub(r"\s*등\s*\d*\s*개.*$", "", e["place"])) if "박물관" in n or "미술관" in n]
            targets = [{**e, "id": f"{e['id']}-{i}", "venue": n, "place": "", "address": "", "homepage": "", "lat": 0.0, "lon": 0.0, "touring": True}
                       for i, n in enumerate(names)] or [e]
        for t in targets:
            vid = venue_for(t)
            if not vid:
                dropped += 1
                continue
            dedupe = (norm(t["title"])[:24], vid)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            treasures, grade = tag_treasures(t["text"], t["title"], catalog_names)
            if o.get("treasures"):
                treasures, grade = o["treasures"], "confirmed"
            days = (t["end"] - t["start"]).days + 1
            touring = bool(t.get("touring") or o.get("touring") or re.search(r"순회전|순회 전시|순회전시", t["title"]))
            exhibitions.append({
                "id": t["id"], "title": t["title"], "venueId": vid, "place": t["place"],
                "start": t["start"].isoformat(), "end": t["end"].isoformat(),
                "fee": o.get("fee", t["fee"]),
                # 요금 설명이 '무료' 한마디뿐이면 fee 와 겹치므로 싣지 않는다.
                "feeText": (lambda x: "" if x in ("무료", "유료") else x)(o.get("feeText", t["feeText"])),
                "image": t["image"], "url": t["url"] or t["homepage"], "source": t["source"],
                "kind": "순회전" if touring else t.get("kindLabel") or ("기획전" if days <= 366 else "상설"),
                "treasures": treasures, "treasureGrade": grade,
                "limited": bool(o.get("limited")), "short": days <= SHORT_DAYS, "touring": touring,
                "note": o.get("note", ""),
            })

    # 국보·보물을 가진 박물관은 전시가 없어도 지도에 싣는다 (상설 관람 가능).
    for key, held in holdings.items():
        if key not in by_name:
            first = held[0]
            name = HOLDER_ALIAS.get(first["admin"], first["admin"])
            if len(held) < 2 or not re.search(r"박물관|미술관|기념관", name):
                continue
            if norm(name) in by_name:  # 별칭으로 바꾼 이름이 이미 전시장으로 있다
                by_name[key] = by_name[norm(name)]
            else:
                lat, lon = locate(name)
                if not lat:
                    continue
                vid = "v" + hashlib.sha1(key.encode()).hexdigest()[:8]
                venues[vid] = {"id": vid, "name": name, "address": "", "sido": first["sido"],
                               "lat": round(lat, 6), "lon": round(lon, 6), "homepage": "", "holdings": []}
                by_name[key] = vid
        venues[by_name[key]]["holdings"] = sorted(
            ({"kind": h["kind"], "name": h["name"]} for h in held), key=lambda x: (x["kind"] != "국보", x["name"]))

    # 지역 테마: 정류지 이름으로 좌표를 찾는다 (전시장 → 국가유산청 목록 → 지오코더 순).
    themes = []
    # 유적지는 대부분 사적·국가민속문화유산이라 그 목록도 받아 이름으로 찾는다.
    sites = khs_catalog(f, (("13", "사적"), ("17", "국가민속문화유산")))
    khs_by_name = {norm(it["name"]): it for it in sites + catalog if it["lat"]}
    for th in json.loads((TOOLS / "themes.json").read_text(encoding="utf-8")):
        stops = []
        for s in th["stops"]:
            key = norm(s["name"])
            lat, lon, vid = s.get("lat", 0.0), s.get("lon", 0.0), by_name.get(key)
            if vid:
                lat, lon = venues[vid]["lat"], venues[vid]["lon"]
            elif not lat and key in khs_by_name:
                lat, lon = khs_by_name[key]["lat"], khs_by_name[key]["lon"]
            elif not lat:
                lat, lon = locate(s.get("query", s["name"]))
            if not lat:
                log(f"  ! 테마 '{th['name']}' 정류지 좌표 없음: {s['name']}")
                continue
            stops.append({"name": s["name"], "nameEn": s.get("nameEn", ""), "lat": round(lat, 6), "lon": round(lon, 6),
                          "venueId": vid or "", "note": s.get("note", "")})
        # 영어 이름(nameEn 등)은 앱을 영어로 볼 때 쓴다. 없으면 앱이 한국어 이름을 그대로 보여 준다.
        themes.append({"id": th["id"], "name": th["name"], "nameEn": th.get("nameEn", ""), "area": th["area"],
                       "areaEn": th.get("areaEn", ""), "desc": th["desc"], "descEn": th.get("descEn", ""),
                       "regions": th["regions"], "stops": stops})

    exhibitions.sort(key=lambda x: (x["end"], x["title"]))
    used = {e["venueId"] for e in exhibitions} | {v["id"] for v in venues.values() if v["holdings"]}

    # 도장 찍기용 국보·보물 전체와 그것이 있는 곳. 개인 소장(관리자 이름이 가려진 것)은 찾아갈 수 없어 뺀다.
    #  - 박물관 소장분은 그 박물관(전시장)이 '곳'이 된다.
    #  - 사찰·유적처럼 야외에 있는 것은 관리자와 위치(약 1km 격자)가 같은 것끼리 한 곳으로 묶는다.
    #  - 좌표가 없는 것은 관리자별로 묶고 지도에는 찍지 않는다 (목록에서만 체크).
    groups: dict[str, dict] = {}
    for it in catalog:
        if not it["admin"] or "＊" in it["admin"] or "*" in it["admin"]:
            continue
        key = norm(it["admin"])
        vid = by_name.get(key) if key in holdings and by_name.get(key) in used else None
        holder = HOLDER_ALIAS.get(it["admin"], it["admin"])
        if vid:
            pid = vid
        else:
            where = f"|{round(it['lat'], 2)}|{round(it['lon'], 2)}" if it["lat"] else ""
            pid = "p" + hashlib.sha1((holder + where).encode()).hexdigest()[:8]
        groups.setdefault(pid, {"holder": holder, "venueId": vid or "", "items": []})["items"].append(it)

    places, heritage = [], []
    for pid, g in groups.items():
        items = sorted(g["items"], key=lambda x: (x["kind"] != "국보", x["name"]))
        pts = sorted((x["lat"], x["lon"]) for x in items if x["lat"])
        if g["venueId"]:
            name, lat, lon = venues[pid]["name"], venues[pid]["lat"], venues[pid]["lon"]
        else:
            lat, lon = pts[len(pts) // 2] if pts else (0.0, 0.0)
            # 지자체·관청이 관리하는 야외 유산은 관리자 이름('경주시')보다 유산 이름이 그곳을 잘 가리킨다.
            agency = re.search(r"(특별시|광역시|시|군|구|도|청|관리소|사무소)$", g["holder"])
            name = items[0]["name"] if agency and pts else g["holder"]
        sido = max({x["sido"] for x in items}, key=lambda v: sum(x["sido"] == v for x in items))
        places.append({"id": pid, "name": name, "sido": sido, "lat": round(lat, 6), "lon": round(lon, 6), "venueId": g["venueId"]})
        heritage += [{"id": x["id"], "kind": x["kind"], "name": x["name"], "placeId": pid} for x in items]
    assert len({h["id"] for h in heritage}) == len(heritage), "국보·보물 id 가 겹친다"

    feed = {
        "version": 1,
        "generatedAt": dt.datetime.now(KST).replace(microsecond=0).isoformat(),
        # 박물관의 소장 국보·보물은 places/heritage 에서 venueId 로 찾는다.
        "venues": sorted(({k: v for k, v in venue.items() if k != "holdings"} for venue in venues.values() if venue["id"] in used),
                         key=lambda v: v["name"]),
        "exhibitions": exhibitions,
        "themes": themes,
        "places": sorted(places, key=lambda x: x["name"]),
        "heritage": heritage,
    }
    log(f"피드: 전시 {len(exhibitions)}건, 장소 {len(feed['venues'])}곳, 테마 {len(themes)}개"
        f" (좌표를 못 찾아 뺀 전시 {dropped}건)")
    log(f"  도장 대상: 국보 {sum(h['kind'] == '국보' for h in heritage)}건, 보물 {sum(h['kind'] == '보물' for h in heritage)}건,"
        f" 곳 {len(places)}곳 (지도에 찍히는 곳 {sum(1 for x in places if x['lat'])}곳)")
    log("  국보·보물 단서: " + ", ".join(
        f"{g or '없음'} {sum(e['treasureGrade'] == g for e in exhibitions)}" for g in ("confirmed", "named", "mention", "")))
    # 체험·교육 프로그램은 양이 많아 따로 둔다 (앱은 그 탭을 열 때만 읽는다).
    spots = [(x["name"], x["sido"], x["lat"], x["lon"]) for x in sites + catalog if x["lat"]]
    spots += [(v["name"], v["sido"], v["lat"], v["lon"]) for v in venues.values()]
    programs = {
        "version": 1,
        "generatedAt": feed["generatedAt"],
        "programs": khs_programs(f, today, spots),
    }
    return feed, programs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 모두 새로 받는다")
    ap.add_argument("--max-pages", type=int, default=80, help="문화포털 목록을 몇 쪽까지 읽을지")
    ap.add_argument("--out", default=str(ROOT / "feed"), help="feed.json 을 쓸 폴더")
    ap.add_argument("--no-asset", action="store_true", help="앱 내장 스냅숏(app/src/main/assets/feed.json)은 건드리지 않는다")
    args = ap.parse_args()

    feed, programs = build(args)
    if len(feed["exhibitions"]) < 5:
        sys.exit("전시가 5건도 안 된다. 출처 페이지 구조가 바뀌었을 수 있어 피드를 덮어쓰지 않는다.")
    outputs = [("feed.json", feed)]
    # 프로그램을 거의 못 받았으면 그 파일만 건너뛴다. 전시 피드까지 막을 일은 아니고, 호스팅에는 어제 파일이 남는다.
    if len(programs["programs"]) >= 20:
        outputs.append(("programs.json", programs))
    else:
        log(f"  ! 프로그램이 {len(programs['programs'])}건뿐이라 programs.json 은 쓰지 않는다")
    for name, data in outputs:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        targets = [Path(args.out) / name]
        if not args.no_asset:
            targets.append(ROOT / "app/src/main/assets" / name)
        for path in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            log(f"→ {path} ({len(text.encode()) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
