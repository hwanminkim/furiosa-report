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
    # 글로벌 회사: 회사명 + (반도체 도메인 키워드 OR 그룹). 무관 기사 (주식/시장/회계 등) 배제.
    {"name": "NVIDIA", "region": "global", "query": "NVIDIA (chip OR AI OR inference OR NPU OR accelerator OR GPU)", "lang": "en"},
    {"name": "Tenstorrent", "region": "global", "query": "Tenstorrent (chip OR AI OR inference OR NPU OR accelerator OR GPU)", "lang": "en"},
    {"name": "Groq", "region": "global", "query": "Groq (chip OR AI OR inference OR NPU OR accelerator OR GPU)", "lang": "en"},
    {"name": "SambaNova", "region": "global", "query": "SambaNova (chip OR AI OR inference OR NPU OR accelerator OR GPU)", "lang": "en"},
    {"name": "Cerebras", "region": "global", "query": "Cerebras (chip OR AI OR inference OR NPU OR accelerator OR GPU)", "lang": "en"},
    # 한국 회사: Naver는 띄어쓴 키워드를 AND로 처리 → 회사명만 단순하게 (OR로 한/영 표기 합침)
    {"name": "Rebellions", "region": "korea", "query": "리벨리온", "lang": "ko"},
    {"name": "DeepX", "region": "korea", "query": "딥엑스 OR DeepX", "lang": "ko"},
    {"name": "HyperAccel", "region": "korea", "query": "하이퍼엑셀 OR HyperAccel", "lang": "ko"},
    {"name": "Mobilint", "region": "korea", "query": "모빌린트 OR Mobilint", "lang": "ko"},
]

FURIOSA_QUERIES = [
    ('furiosa ai OR furiosaai OR "Furiosa AI" chip', "en"),
    ('퓨리오사 ai OR 퓨리오사ai OR 퓨리오사AI OR FuriosaAI', "ko"),
]


def gnews_url(query: str, lang: str) -> str:
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


_TITLE_SOURCE_SUFFIX = re.compile(r"\s+[-|–—·]\s+[^-|–—·]+$")
_TITLE_NON_WORD = re.compile(r"[^\w가-힣\s]", flags=re.UNICODE)
_TITLE_WHITESPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """
    Normalize a news title so that minor variations collapse to the same key.
    - strip trailing source name (" - TechCrunch", " | The Korea Herald", " · Reuters")
    - lowercase, drop punctuation, collapse whitespace
    """
    if not title:
        return ""
    t = _TITLE_SOURCE_SUFFIX.sub("", title)
    t = t.lower()
    t = _TITLE_NON_WORD.sub(" ", t)
    t = _TITLE_WHITESPACE.sub(" ", t).strip()
    return t


def parse_pub_datetime(raw: str) -> datetime.datetime | None:
    """Parse RSS pubDate into a timezone-aware UTC datetime, or None on failure."""
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


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """Naver returns titles like 'FuriosaAI &quot;<b>RNGD</b>&quot; ...'. Clean it."""
    if not s:
        return ""
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    return s.strip()


def _fetch_google(query: str, lang: str, n: int) -> list[dict]:
    """Google News RSS fetcher."""
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
    """GDELT seendate 형식: YYYYMMDDTHHMMSSZ → UTC datetime."""
    if not raw or len(raw) < 15:
        return None
    try:
        dt = datetime.datetime.strptime(raw, "%Y%m%dT%H%M%SZ")
        return dt.replace(tzinfo=pytz.utc)
    except Exception:
        return None


def _fetch_gdelt(query: str, n: int) -> list[dict]:
    """
    GDELT 2.0 DOC API fetcher (English news).
    Endpoint: https://api.gdeltproject.org/api/v2/doc/doc
    No API key required.
    Sort by latest date (DateDesc), timespan 30d, English only.

    Rate limit 대응: HTTP 429 받으면 2초/5초 대기 후 재시도 (최대 2회).
    매 호출 후 2초 sleep (다음 호출 전 간격 확보).
    """
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
    retry_delays = [2, 5]  # 429 시 대기 시간 (초)
    for attempt in range(len(retry_delays) + 1):
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break  # 성공
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

    # 호출 후 항상 sleep — 다음 GDELT 호출과 간격 확보 (성공/실패 무관).
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
                "description": "",  # GDELT doesn't provide article snippet in ArtList mode
            })
    return results


def _fetch_naver(query: str, n: int) -> list[dict]:
    """
    Naver News API fetcher. Requires NAVER_CLIENT_ID / NAVER_CLIENT_SECRET env vars.
    Returns [] if creds are missing — caller should fall back to Google.

    Naver query syntax differs slightly from Google ("OR" → "|"), so we translate.
    Sort: "sim" (relevance) — closest to a popularity proxy. Use "date" if newest-first.
    """
    cid = os.environ.get("NAVER_CLIENT_ID")
    csec = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        return []

    naver_q = query.replace(" OR ", " | ")
    # sort=date: 최신순 (BD 용도에는 최신성이 우선)
    # sort=sim: 정확도순 (관련성이 우선이면 이쪽)
    url = (f"https://openapi.naver.com/v1/search/news.json"
           f"?query={quote(naver_q)}&display={min(max(n, 10), 100)}&sort=date")
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
        # originallink: 원 매체 URL (선호) / link: 네이버 뉴스 URL
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
    """
    Dispatch to Naver (Korean) or GDELT (English, with Google fallback).
    Korean queries fall back to Google if Naver creds are missing.
    """
    if lang == "ko":
        items = _fetch_naver(query, n)
        if items:
            return items
        # fallback
        if not os.environ.get("NAVER_CLIENT_ID"):
            print(f"  [info] NAVER creds missing, using Google for: {query}")
        return _fetch_google(query, lang, n)
    # English: GDELT (date-sorted, latest news) → Google fallback if empty
    items = _fetch_gdelt(query, n)
    if items:
        return items
    print(f"  [info] GDELT empty for: {query}, falling back to Google")
    return _fetch_google(query, lang, n)


def build_period(now: datetime.datetime) -> str:
    today = now.date()
    week_start = today - datetime.timedelta(days=6)  # 오늘 포함 최근 7일
    days_ko = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} "
            f"~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}")


def cluster_articles_by_event(articles: list[dict], client: OpenAI | None) -> list[dict]:
    """
    Group articles that report the same news event and return one representative
    per group (preserving the original Google News order).
    Falls back to the original list on any failure (network, parse, model error).
    """
    if client is None or len(articles) < 2:
        return articles

    numbered = "\n".join(f"[{i}] {a['title']}" for i, a in enumerate(articles))
    prompt = f"""다음은 Furiosa AI 관련 뉴스 제목 목록입니다. 각 줄은 [번호] 제목 형식.

같은 사건(같은 발표, 같은 인사이동, 같은 정책 등)을 다룬 제목들을 같은 클러스터로 묶어주세요.
표현이 달라도 핵심 사실이 같으면 같은 클러스터입니다.
다른 사건이면 각각 별도 클러스터입니다.

제목 목록:
{numbered}

응답 형식 (오직 JSON):
{{"clusters": [[0, 2], [1], [3, 4, 5]]}}

규칙:
- 0부터 {len(articles)-1}까지 모든 인덱스가 정확히 한 번씩만 포함되어야 합니다.
- 확신이 없으면 별도 클러스터로 두세요 (과도한 병합 금지)."""

    try:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            print(f"  [warn] response_format unsupported, retrying without it: {e}")
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
                temperature=0.1,
            )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)
        clusters = data.get("clusters", [])

        used: set[int] = set()
        kept_indices: list[int] = []
        for cluster in clusters:
            valid = [i for i in cluster
                     if isinstance(i, int) and 0 <= i < len(articles)]
            if not valid:
                continue
            rep = min(valid)  # preserve Google News order
            if rep not in used:
                kept_indices.append(rep)
            for i in valid:
                used.add(i)
        # 클러스터에 포함되지 않은 항목은 별도 클러스터로 살림
        for i in range(len(articles)):
            if i not in used:
                kept_indices.append(i)
        kept_indices.sort()
        deduped = [articles[i] for i in kept_indices]
        print(f"  LLM clustering: {len(articles)} → {len(deduped)} articles "
              f"({len(articles) - len(deduped)} merged)")
        return deduped
    except Exception as e:
        print(f"  [warn] LLM clustering failed, falling back to raw dedup: {e}")
        return articles


def filter_relevant_by_company(company: str, articles: list[dict], client: "OpenAI | None") -> list[dict]:
    """
    한 회사의 기사 목록을 받아 그 회사가 실제로 핵심 주제인 기사만 남긴다.
    회사명이 본문에 잠깐 언급된 무관 기사 제거.

    실패 시(LLM 호출 실패 등) 원본 그대로 반환 (안전).
    """
    if client is None or not articles:
        return articles

    numbered = "\n".join(f"[{i}] {a.get('title', '')}" for i, a in enumerate(articles))
    prompt = f"""다음은 '{company}' 회사 관련 뉴스 검색 결과입니다.
각 제목을 보고 그 회사가 실제로 기사의 **핵심 주제**인지 판단하세요.
회사명이 본문에 잠깐 언급되거나 비교 대상으로만 나오는 무관 기사는 제외합니다.

제목 목록:
{numbered}

응답 형식 (오직 JSON):
{{"keep": [0, 2, 5]}}

규칙:
- '{company}'가 기사의 주요 대상/주체이면 keep.
- 회사 발표, 제품, 인사, 투자, 실적, 기술, 인터뷰 등이면 keep.
- 회사명이 다른 회사 비교 글에서 잠깐 언급되거나(예: "X vs {company}" 형식의 비교 기사가 다른 회사 중심), 시장 일반 분석에서 나열만 된 거면 제외.
- 확신이 없으면 keep (보수적으로)."""

    try:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
        except Exception:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                temperature=0.1,
            )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)
        keep = data.get("keep", [])
        valid = [i for i in keep if isinstance(i, int) and 0 <= i < len(articles)]
        if not valid:
            # LLM이 0개 keep이라고 답해도, 안전망으로 원본 유지
            print(f"  [info] {company}: relevance filter returned empty, keeping all")
            return articles
        filtered = [articles[i] for i in valid]
        print(f"  Relevance filter for {company}: {len(articles)} → {len(filtered)}")
        return filtered
    except Exception as e:
        print(f"  [warn] Relevance filter failed for {company}, keeping all: {e}")
        return articles


def to_output(a: dict, kst: pytz.BaseTzInfo, include_brief: bool = False,
              include_summary_only: bool = False) -> dict:
    """Strip non-JSON-serializable fields and format date for display.
    include_brief=True 면 summary / bd_perspective 도 함께 포함 (회사 기사용).
    include_summary_only=True 면 summary 만 포함 (Furiosa 기사용)."""
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
    """
    경쟁사 기사 목록에 대해 1회 batch LLM 호출로
    {(company, url): {"summary": ..., "bd_perspective": ...}} 딕셔너리 반환.
    실패 시 빈 dict (호출자에서 빈 값으로 fallback).
    """
    if client is None or not articles_with_company:
        return {}

    items_in = []
    for i, (company, a) in enumerate(articles_with_company):
        items_in.append({
            "id": i,
            "company": company,
            "title": a.get("title", ""),
            "snippet": (a.get("description") or "")[:240],
        })

    prompt = f"""You are a BD (business development) analyst at Furiosa AI, a Korean AI inference chip startup.
Furiosa's competitors include NVIDIA, Groq, Cerebras, SambaNova, Tenstorrent (global)
and Rebellions, DeepX, HyperAccel, Mobilint (Korea).

For each competitor news article below, produce two Korean fields:
- "summary": 3문장, 사실 위주로 핵심 내용을 충분히 풀어쓰기. 총 약 300자 (250~350자). 무엇이/언제/어떻게/왜 중요한지 포함.
- "bd_perspective": 1문장, Furiosa BD 관점에서 이 뉴스가 갖는 의미(기회/위협/시장 시그널), 최대 100자.

진부한 일반론 금지. 구체적인 함의 위주.

Input articles:
{json.dumps(items_in, ensure_ascii=False)}

Return JSON ONLY:
{{"items": [{{"id": 0, "summary": "...", "bd_perspective": "..."}}, ...]}}

Rules:
- 모든 id가 정확히 한 번씩 포함되어야 함.
- 한국어만.
- 정보가 부족하면 추측하지 말고 "추가 정보 필요" 같이 명시."""

    try:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=6000,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            print(f"  [warn] response_format unsupported for briefs, retrying: {e}")
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=6000,
                temperature=0.3,
            )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)

        out: dict = {}
        for entry in data.get("items", []):
            idx = entry.get("id")
            if not isinstance(idx, int) or not (0 <= idx < len(articles_with_company)):
                continue
            company, a = articles_with_company[idx]
            out[(company, a["url"])] = {
                "summary": (entry.get("summary") or "").strip(),
                "bd_perspective": (entry.get("bd_perspective") or "").strip(),
            }
        print(f"  Briefs generated for {len(out)}/{len(articles_with_company)} articles")
        return out
    except Exception as e:
        print(f"  [warn] brief generation failed, falling back to empty: {e}")
        return {}


def generate_furiosa_summaries(articles: list[dict], client: "OpenAI | None") -> dict:
    """
    Furiosa 자체 뉴스에 대해 1회 batch LLM 호출로 요약만 생성.
    BD 시점은 안 만든다 (Furiosa 본인 뉴스이므로 무의미, 토큰 절약).
    반환: {url: summary} 딕셔너리. 실패 시 빈 dict.
    """
    if client is None or not articles:
        return {}

    items_in = []
    for i, a in enumerate(articles):
        items_in.append({
            "id": i,
            "title": a.get("title", ""),
            "snippet": (a.get("description") or "")[:240],
        })

    prompt = f"""다음은 Furiosa AI 관련 뉴스 기사 목록입니다.

각 기사에 대해 한국어 요약을 만들어 주세요:
- "summary": 3문장, 사실 위주로 핵심 내용을 충분히 풀어쓰기. 총 약 300자 (250~350자). 무엇이/언제/어떻게/왜 중요한지 포함.

진부한 일반론 금지. 구체적인 함의 위주.

Input articles:
{json.dumps(items_in, ensure_ascii=False)}

Return JSON ONLY:
{{"items": [{{"id": 0, "summary": "..."}}, ...]}}

Rules:
- 모든 id가 정확히 한 번씩 포함되어야 함.
- 한국어만.
- 정보가 부족하면 추측하지 말고 "추가 정보 필요" 같이 명시."""

    try:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=6000,
                temperature=0.3,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            print(f"  [warn] response_format unsupported for furiosa summaries, retrying: {e}")
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=6000,
                temperature=0.3,
            )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)

        out: dict = {}
        for entry in data.get("items", []):
            idx = entry.get("id")
            if not isinstance(idx, int) or not (0 <= idx < len(articles)):
                continue
            a = articles[idx]
            out[a["url"]] = (entry.get("summary") or "").strip()
        print(f"  Furiosa summaries generated for {len(out)}/{len(articles)} articles")
        return out
    except Exception as e:
        print(f"  [warn] furiosa summary generation failed, falling back to empty: {e}")
        return {}


def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    now_utc = now.astimezone(pytz.utc)
    daily_cutoff = now_utc - datetime.timedelta(hours=24)
    weekly_cutoff = now_utc - datetime.timedelta(days=7)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    # OpenAI client (GitHub Models gpt-4o-mini). 없으면 None → AI 단계 skip.
    token = os.environ.get("GITHUB_TOKEN")
    client: "OpenAI | None" = None
    if token:
        client = OpenAI(base_url="https://models.inference.ai.azure.com", api_key=token)
    else:
        print("  [warn] GITHUB_TOKEN not set: skipping AI clustering & briefs")

    # ── 1. Competitor news (raw) ─────────────────────────────────────────
    # 30일 필터링 후에도 최대 3개를 확보하기 위해 더 많이 받아온다.
    COMPETITOR_CUTOFF_DAYS = 30
    COMPETITOR_MAX_ITEMS = 3
    competitor_cutoff = now_utc - datetime.timedelta(days=COMPETITOR_CUTOFF_DAYS)

    companies_raw = []
    for co in COMPANIES:
        fetched = fetch_articles(co["query"], co["lang"], n=20)
        # 최근 N일 내 기사만 유지. pub_dt 없는 기사는 제외 (Furiosa 필터링과 동일 방식).
        recent = [a for a in fetched
                  if a.get("pub_dt") is not None and a["pub_dt"] >= competitor_cutoff]
        # 관련성 LLM 필터: 회사명만 잠깐 언급된 무관 기사 제거 (클러스터링/요약 토큰 절약).
        relevant = filter_relevant_by_company(co["name"], recent, client)
        # LLM 클러스터링: 같은 사건/syndicated 중복 제거 (회사별로 호출).
        # client가 None이거나 기사 1개 이하면 그대로 반환됨.
        deduped = cluster_articles_by_event(relevant, client)
        # 최신순 정렬 (각 소스의 기본 정렬을 신뢰하지 않고 명시적으로 내림차순).
        deduped.sort(key=lambda a: a["pub_dt"], reverse=True)
        articles = deduped[:COMPETITOR_MAX_ITEMS]
        companies_raw.append({
            "name": co["name"],
            "region": co["region"],
            "articles": articles,
        })
        print(f"  {co['name']}: {len(articles)} articles (fetched={len(fetched)}, in {COMPETITOR_CUTOFF_DAYS}d={len(recent)}, relevant={len(relevant)}, deduped={len(deduped)})")

    # ── 1.5 LLM 요약 + Furiosa BD 시점 생성 (1회 batch 호출) ──────────────
    all_pairs = [(c["name"], a) for c in companies_raw for a in c["articles"]]
    briefs = generate_briefs(all_pairs, client)
    for c in companies_raw:
        for a in c["articles"]:
            key = (c["name"], a["url"])
            if key in briefs:
                a["summary"] = briefs[key]["summary"]
                a["bd_perspective"] = briefs[key]["bd_perspective"]

    companies_out = [
        {
            "name": c["name"],
            "region": c["region"],
            "items": [to_output(a, kst, include_brief=True) for a in c["articles"]],
        }
        for c in companies_raw
    ]

    # ── 2. Furiosa daily / weekly (raw RSS, deduped by URL + normalized title) ──
    DAILY_LIMIT = 5
    WEEKLY_LIMIT = 5
    all_furiosa: list[dict] = []
    seen_urls = set()
    seen_titles = set()
    for query, lang in FURIOSA_QUERIES:
        for a in fetch_articles(query, lang, n=30):
            if a["url"] in seen_urls:
                continue
            norm = normalize_title(a["title"])
            if norm and norm in seen_titles:
                continue
            seen_urls.add(a["url"])
            if norm:
                seen_titles.add(norm)
            all_furiosa.append(a)

    # Google News 기본 정렬을 그대로 보존 (인기도/relevance proxy).
    # 날짜 필터만 걸고 재정렬은 안 함. pub_dt 없는 기사는 제외.
    def in_window(a: dict, cutoff: datetime.datetime) -> bool:
        return a.get("pub_dt") is not None and a["pub_dt"] >= cutoff

    # ── 2.5 LLM 기반 의미 단위 클러스터링 (같은 사건 dedup) ───────────────
    # 7일 안의 기사들만 클러스터링 대상으로 (오래된 기사 토큰 낭비 방지)
    in_weekly_window = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    if client:
        deduped = cluster_articles_by_event(in_weekly_window, client)
        deduped_urls = {a["url"] for a in deduped}
        all_furiosa = [a for a in all_furiosa if a["url"] in deduped_urls
                       or not in_window(a, weekly_cutoff)]

    # 일간/주간: pub_dt 내림차순 (최신순) 정렬 후 top N
    # pub_dt가 None인 기사는 in_window에서 이미 걸러지므로 안전.
    def sort_key(a: dict):
        return a["pub_dt"]

    # 일간: 최근 24시간 top N (최신순)
    furiosa_daily = sorted(
        [a for a in all_furiosa if in_window(a, daily_cutoff)],
        key=sort_key,
        reverse=True,
    )[:DAILY_LIMIT]
    # 주간: 최근 7일 중 일간에 이미 들어간 건 제외하고 top N (최신순)
    daily_urls = {a["url"] for a in furiosa_daily}
    furiosa_weekly = sorted(
        [a for a in all_furiosa
         if in_window(a, weekly_cutoff) and a["url"] not in daily_urls],
        key=sort_key,
        reverse=True,
    )[:WEEKLY_LIMIT]

    print(f"  Furiosa: total={len(all_furiosa)}, daily(24h)={len(furiosa_daily)}, weekly(7d)={len(furiosa_weekly)}")

    # ── 2.6 Furiosa 기사 요약 생성 (1회 batch 호출, BD 시점은 안 만듦) ────
    furiosa_articles = furiosa_daily + furiosa_weekly
    furiosa_summaries = generate_furiosa_summaries(furiosa_articles, client)
    for a in furiosa_articles:
        if a["url"] in furiosa_summaries:
            a["summary"] = furiosa_summaries[a["url"]]

    # ── 3. Write report.json ─────────────────────────────────────────────
    report = {
        "period": build_period(now),
        "updated_at": now.isoformat(),
        "furiosa_daily": [to_output(a, kst, include_summary_only=True) for a in furiosa_daily],
        "furiosa_weekly": [to_output(a, kst, include_summary_only=True) for a in furiosa_weekly],
        "companies": companies_out,
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"Done. {len(report['furiosa_daily'])} daily / {len(report['furiosa_weekly'])} weekly.")


if __name__ == "__main__":
    main()
