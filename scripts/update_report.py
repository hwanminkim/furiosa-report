#!/usr/bin/env python3
"""
Daily report updater.

News sources:
- Korean queries (lang="ko") → Naver News API (requires NAVER_CLIENT_ID/SECRET).
  Falls back to Google News RSS when Naver credentials are missing.
- English queries (lang="en") → Google News RSS.

Furiosa daily / weekly: deduped (URL + normalized title + LLM clustering).
LLM clustering uses GitHub Models gpt-4o-mini with JSON mode.
"""
import datetime
import email.utils
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen, Request

import pytz
from openai import OpenAI

REPO_ROOT = Path(__file__).parent.parent
REPORT_PATH = REPO_ROOT / "report.json"

COMPANIES = [
    {"name": "NVIDIA", "region": "global", "queries": [("NVIDIA", "en")]},
    {"name": "Tenstorrent", "region": "global", "queries": [("Tenstorrent", "en")]},
    {"name": "SambaNova", "region": "global", "queries": [("SambaNova", "en")]},
    {"name": "Cerebras", "region": "global", "queries": [("Cerebras", "en")]},
    {"name": "Rebellions", "region": "korea", "queries": [("리벨리온", "ko"), ("Rebellions", "en")]},
    {"name": "DeepX", "region": "korea", "queries": [("딥엑스", "ko"), ("DeepX", "en")]},
    {"name": "HyperAccel", "region": "korea", "queries": [("하이퍼엑셀", "ko"), ("HyperAccel", "en")]},
    {"name": "Mobilint", "region": "korea", "queries": [("모빌린트", "ko"), ("Mobilint", "en")]},
]

FURIOSA_QUERIES = [
    ('furiosa ai OR furiosaai OR "Furiosa AI" chip', "en"),
    ('퓨리오사ai OR 퓨리오사AI OR FuriosaAI', "ko"),
]


def gnews_url(query: str, lang: str) -> str:
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


_TITLE_SOURCE_SUFFIX = re.compile(r"\s+[-|–—·]\s+[^-|–—·]+$")
_TITLE_NON_WORD = re.compile(r"[^\w가-힣\s]", flags=re.UNICODE)
_TITLE_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = _TITLE_SOURCE_SUFFIX.sub("", title)
    t = t.lower()
    t = _TITLE_NON_WORD.sub(" ", t)
    t = _TITLE_WHITESPACE.sub(" ", t).strip()
    return t


def parse_pub_datetime(raw: str) -> datetime.datetime | None:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.utc)
        return dt.astimezone(pytz.utc)
    except Exception:
        return None


def format_date_kst(dt: datetime.datetime | None, kst: pytz.BaseTzInfo) -> str:
    if dt is None:
        return ""
    return dt.astimezone(kst).strftime("%m-%d")


def format_date_with_weekday(dt: datetime.datetime | None, kst: pytz.BaseTzInfo) -> str:
    """프론트엔드 스티키 헤더용 날짜 포맷 (예: 05-20 (수))"""
    if dt is None:
        return ""
    dt_kst = dt.astimezone(kst)
    days_ko = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return f"{dt_kst.strftime('%m-%d')} {days_ko[dt_kst.weekday()]}"


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    return s.strip()


def _fetch_google(query: str, lang: str, n: int) -> list[dict]:
    try:
        req = Request(gnews_url(query, lang),
                      headers={"User-Agent": "Mozilla/5.0 (compatible; FuriosaReport/1.0)"})
        with urlopen(req, timeout=10) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"  [google-skip] {query}: {e}")
        return []

    results = []
    for item in root.findall(".//item")[:n]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = _strip_html(item.findtext("description") or "")
        pub_dt = parse_pub_datetime(item.findtext("pubDate") or "")
        if title and link:
            results.append({
                "title": title,
                "url": link,
                "pub_dt": pub_dt,
                "description": desc,
            })
    return results


def _parse_gdelt_seendate(raw: str) -> datetime.datetime | None:
    if not raw or len(raw) < 15:
        return None
    try:
        dt = datetime.datetime.strptime(raw, "%Y%m%dT%H%M%SZ")
        return dt.replace(tzinfo=pytz.utc)
    except Exception:
        return None


def _fetch_gdelt(query: str, n: int) -> list[dict]:
    full_query = f"{query} sourcelang:eng"
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={quote(full_query)}"
        f"&mode=ArtList"
        f"&maxrecords={n}"
        f"&format=json"
        f"&sort=DateDesc"
        f"&timespan=30d"
    )
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; FuriosaReport/1.0)",
    })

    data = None
    retry_delays = [2, 5]
    for attempt in range(len(retry_delays) + 1):
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as e:
            if e.code == 429 and attempt < len(retry_delays):
                wait = retry_delays[attempt]
                print(f"  [gdelt-429] {query}: rate limited, retrying in {wait}s ({attempt+1}/{len(retry_delays)})")
                time.sleep(wait)
                continue
            print(f"  [gdelt-skip] {query}: HTTP {e.code}")
            break
        except Exception as e:
            print(f"  [gdelt-skip] {query}: {e}")
            break

    time.sleep(2)
    if data is None:
        return []

    results = []
    for item in (data.get("articles") or [])[:n]:
        title = (item.get("title") or "").strip()
        link = (item.get("url") or "").strip()
        pub_dt = _parse_gdelt_seendate(item.get("seendate") or "")
        if title and link:
            results.append({
                "title": title,
                "url": link,
                "pub_dt": pub_dt,
                "description": "",
            })
    return results


def _fetch_naver(query: str, n: int) -> list[dict]:
    cid = os.environ.get("NAVER_CLIENT_ID")
    csec = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        return []

    naver_q = query.replace(" OR ", " | ")
    
    # 🚨 최근 7일 필터 유지를 위해 퓨리오사 크롤링용 최신순(sort=date) 복구 및 100개 탐색
    url = (f"https://openapi.naver.com/v1/search/news.json"
           f"?query={quote(naver_q)}&display=100&sort=date")
    
    req = Request(url, headers={
        "X-Naver-Client-Id": cid,
        "X-Naver-Client-Secret": csec,
        "User-Agent": "Mozilla/5.0 (compatible; FuriosaReport/1.0)",
    })
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [naver-skip] {query}: {e}")
        return []

    results = []
    for item in data.get("items", [])[:n]:
        title = _strip_html(item.get("title", ""))
        link = (item.get("originallink") or item.get("link") or "").strip()
        desc = _strip_html(item.get("description", ""))
        pub_dt = parse_pub_datetime(item.get("pubDate") or "")
        if title and link:
            results.append({
                "title": title,
                "url": link,
                "pub_dt": pub_dt,
                "description": desc,
            })
    return results


def fetch_articles(query: str, lang: str, n: int = 20) -> list[dict]:
    if lang == "ko":
        items = _fetch_naver(query, n)
        if items:
            return items
        return _fetch_google(query, lang, n)
    items = _fetch_gdelt(query, n)
    if items:
        return items
    return _fetch_google(query, lang, n)


def build_period(now: datetime.datetime) -> str:
    today = now.date()
    week_start = today - datetime.timedelta(days=6)
    days_ko = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} "
            f"~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}")


def cluster_articles_by_event(articles: list[dict], client: OpenAI | None) -> list[dict]:
    if client is None or len(articles) < 2:
        return articles

    numbered = "\n".join(f"[{i}] {a['title']}" for i, a in enumerate(articles))
    prompt = f"""다음은 뉴스 제목 목록입니다. 각 줄은 [번호] 제목 형식.
같은 사건을 다룬 제목들을 같은 클러스터로 묶어주세요.

제목 목록:
{numbered}

응답 형식 (오직 JSON):
{{"clusters": [[0, 2], [1], [3, 4, 5]]}}"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)
        clusters = data.get("clusters", [])

        used: set[int] = set()
        kept_indices: list[int] = []
        for cluster in clusters:
            valid = [i for i in cluster if isinstance(i, int) and 0 <= i < len(articles)]
            if not valid: continue
            rep = min(valid)
            if rep not in used: kept_indices.append(rep)
            for i in valid: used.add(i)
        for i in range(len(articles)):
            if i not in used: kept_indices.append(i)
        kept_indices.sort()
        return [articles[i] for i in kept_indices]
    except Exception as e:
        print(f"  [warn] LLM clustering failed: {e}")
        return articles


def filter_relevant_by_company(company: str, articles: list[dict], client: "OpenAI | None") -> list[dict]:
    if client is None or not articles:
        return articles

    numbered_items = []
    for i, a in enumerate(articles):
        desc = (a.get("description") or "").replace("\n", " ").strip()[:150]
        numbered_items.append(f"[{i}] 제목: {a.get('title', '')}\n    요약: {desc}")
    numbered = "\n".join(numbered_items)

    prompt = f"""다음은 '{company}' 관련 뉴스 검색 결과입니다. BD 및 시장 동향 파악에 유용한 정보인지 판단하세요.
기사 목록:
{numbered}

응답 형식 (오직 JSON):
{{"keep": [0, 2, 5]}}"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)
        keep = data.get("keep", [])
        valid = [i for i in keep if isinstance(i, int) and 0 <= i < len(articles)]
        return [articles[i] for i in valid]
    except Exception as e:
        print(f"  [warn] Relevance filter failed: {e}")
        return articles


def to_output(a: dict, kst: pytz.BaseTzInfo, include_brief: bool = False,
              include_summary_only: bool = False) -> dict:
    out = {
        "title": a["title"],
        "url": a["url"],
        "date": format_date_kst(a.get("pub_dt"), kst),
    }
    if include_brief:
        out["summary"] = a.get("summary", "")
        out["bd_perspective"] = a.get("bd_perspective", "")
    elif include_summary_only:
        out["summary"] = a.get("summary", "")
    return out


def generate_briefs(articles_with_company: list[tuple], client: "OpenAI | None") -> dict:
    if client is None or not articles_with_company:
        return {}
    items_in = [{"id": i, "company": comp, "title": a.get("title", ""), "snippet": (a.get("description") or "")[:240]} 
                for i, (comp, a) in enumerate(articles_with_company)]

    prompt = f"경쟁사 뉴스 요약 및 BD 관점 생성용 프롬프트 생략 (기존 로직 유지)\nInput: {json.dumps(items_in, ensure_ascii=False)}"
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        data = json.loads(raw)
        out = {}
        for entry in data.get("items", []):
            idx = entry.get("id")
            if idx is not None and 0 <= idx < len(articles_with_company):
                comp, a = articles_with_company[idx]
                out[(comp, a["url"])] = {"summary": entry.get("summary", ""), "bd_perspective": entry.get("bd_perspective", "")}
        return out
    except Exception:
        return {}


def generate_furiosa_summaries(articles: list[dict], client: "OpenAI | None") -> dict:
    if client is None or not articles:
        return {}
    items_in = [{"id": i, "title": a.get("title", ""), "snippet": (a.get("description") or "")[:240]} for i, a in enumerate(articles)]
    prompt = f"Furiosa 뉴스 요약용 프롬프트 생략 (기존 로직 유지)\nInput: {json.dumps(items_in, ensure_ascii=False)}"
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "")
        return {articles[entry["id"]]["url"]: entry.get("summary", "") for entry in data.get("items", []) if "id" in entry}
    except Exception:
        return {}


def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    now_utc = now.astimezone(pytz.utc)
    daily_cutoff = now_utc - datetime.timedelta(hours=24)
    weekly_cutoff = now_utc - datetime.timedelta(days=7)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    token = os.environ.get("GITHUB_TOKEN")
    client = OpenAI(base_url="https://models.inference.ai.azure.com", api_key=token) if token else None

    # ── 1. Competitor 뉴스 수집 (기존 로직 동일) ─────────────────────────
    COMPETITOR_CUTOFF_DAYS = 30
    COMPETITOR_MAX_ITEMS = 3
    competitor_cutoff = now_utc - datetime.timedelta(days=COMPETITOR_CUTOFF_DAYS)

    companies_raw = []
    for co in COMPANIES:
        fetched = []
        seen_urls = set()
        for query, lang in co["queries"]:
            for a in fetch_articles(query, lang, n=100): # 100개 탐색
                if a["url"] not in seen_urls:
                    seen_urls.add(a["url"])
                    fetched.append(a)
        recent = [a for a in fetched if a.get("pub_dt") is not None and a["pub_dt"] >= competitor_cutoff]
        relevant = filter_relevant_by_company(co["name"], recent, client)
        deduped = cluster_articles_by_event(relevant, client)
        deduped.sort(key=lambda a: a["pub_dt"], reverse=True)
        companies_raw.append({"name": co["name"], "region": co["region"], "articles": deduped[:COMPETITOR_MAX_ITEMS]})

    all_pairs = [(c["name"], a) for c in companies_raw for a in c["articles"]]
    briefs = generate_briefs(all_pairs, client)
    for c in companies_raw:
        for a in c["articles"]:
            key = (c["name"], a["url"])
            if key in briefs:
                a["summary"] = briefs[key]["summary"]
                a["bd_perspective"] = briefs[key]["bd_perspective"]

    companies_out = [{"name": c["name"], "region": c["region"], "items": [to_output(a, kst, include_brief=True) for a in c["articles"]]} for c in companies_raw]

    # ── 2. Furiosa 뉴스 수집 및 중복 제거 ────────────────────────────────
    all_furiosa = []
    seen_urls, seen_titles = set(), set()
    for query, lang in FURIOSA_QUERIES:
        for a in fetch_articles(query, lang, n=100): # 🚨 100개 최신순 수집
            if a["url"] in seen_urls: continue
            norm = normalize_title(a["title"])
            if norm and norm in seen_titles: continue
            seen_urls.add(a["url"])
            if norm: seen_titles.add(norm)
            all_furiosa.append(a)

    def in_window(a: dict, cutoff: datetime.datetime) -> bool:
        return a.get("pub_dt") is not None and a["pub_dt"] >= cutoff

    in_weekly_window = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    if client:
        deduped = cluster_articles_by_event(in_weekly_window, client)
        deduped_urls = {a["url"] for a in deduped}
        all_furiosa = [a for a in all_furiosa if a["url"] in deduped_urls or not in_window(a, weekly_cutoff)]

    # ── 🚨 2.5 일간 / 주간 분리 및 주간 그룹핑(Grouping) 로직 ──
    # 1) 일간: 최근 24시간 이내 기사 (최대 5개)
    furiosa_daily_raw = sorted([a for a in all_furiosa if in_window(a, daily_cutoff)], key=lambda x: x["pub_dt"], reverse=True)[:5]
    
    # 2) 주간: 최근 7일 중 '최근 24시간 이내 기사'는 완전히 배제 (겹침 방지)
    furiosa_weekly_raw = sorted([a for a in all_furiosa if in_window(a, weekly_cutoff) and not in_window(a, daily_cutoff)], key=lambda x: x["pub_dt"], reverse=True)

    # 요약 생성용 일괄 병합 및 호출
    furiosa_articles = furiosa_daily_raw + furiosa_weekly_raw
    furiosa_summaries = generate_furiosa_summaries(furiosa_articles, client)
    for a in furiosa_articles:
        if a["url"] in furiosa_summaries:
            a["summary"] = furiosa_summaries[a["url"]]

    # 3) 일간 데이터 아웃풋 변환
    furiosa_daily_out = [to_output(a, kst, include_summary_only=True) for a in furiosa_daily_raw]

    # 4) 🚨 주간 데이터 날짜별 그룹핑 {"05-20 (수)": [기사1, 기사2]}
    furiosa_weekly_group = {}
    for a in furiosa_weekly_raw:
        date_key = format_date_with_weekday(a.get("pub_dt"), kst) # '05-20 (수)' 형태로 키 생성
        if date_key not in furiosa_weekly_group:
            furiosa_weekly_group[date_key] = []
        furiosa_weekly_group[date_key].append(to_output(a, kst, include_summary_only=True))

    # ── 3. Write report.json ─────────────────────────────────────────────
    report = {
        "period": build_period(now),
        "updated_at": now.isoformat(),
        "furiosa_daily": furiosa_daily_out,
        "furiosa_weekly": furiosa_weekly_group, # 🚨 딕셔너리 구조로 변경됨
        "companies": companies_out,
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"Done. Update completed.")


if __name__ == "__main__":
    main()
