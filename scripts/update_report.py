#!/usr/bin/env python3
"""
Daily report updater.
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
    if not title: return ""
    t = _TITLE_SOURCE_SUFFIX.sub("", title)
    t = t.lower()
    t = _TITLE_NON_WORD.sub(" ", t)
    t = _TITLE_WHITESPACE.sub(" ", t).strip()
    return t

def parse_pub_datetime(raw: str) -> datetime.datetime | None:
    if not raw: return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is None: return None
        if dt.tzinfo is None: dt = dt.replace(tzinfo=pytz.utc)
        return dt.astimezone(pytz.utc)
    except Exception:
        return None

def format_date_kst(dt: datetime.datetime | None, kst: pytz.BaseTzInfo) -> str:
    if dt is None: return ""
    return dt.astimezone(kst).strftime("%m-%d")

# 🚨 스티키 헤더를 위한 요일 포함 포맷 함수
def format_date_with_weekday(dt: datetime.datetime | None, kst: pytz.BaseTzInfo) -> str:
    if dt is None: return ""
    dt_kst = dt.astimezone(kst)
    days_ko = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return f"{dt_kst.strftime('%m-%d')} {days_ko[dt_kst.weekday()]}"

_HTML_TAG_RE = re.compile(r"<[^>]+>")

def _strip_html(s: str) -> str:
    if not s: return ""
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    return s.strip()

def _fetch_google(query: str, lang: str, n: int) -> list[dict]:
    try:
        req = Request(gnews_url(query, lang), headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            root = ET.fromstring(resp.read())
    except Exception:
        return []
    results = []
    for item in root.findall(".//item")[:n]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = _strip_html(item.findtext("description") or "")
        pub_dt = parse_pub_datetime(item.findtext("pubDate") or "")
        if title and link:
            results.append({"title": title, "url": link, "pub_dt": pub_dt, "description": desc})
    return results

def _parse_gdelt_seendate(raw: str) -> datetime.datetime | None:
    if not raw or len(raw) < 15: return None
    try:
        dt = datetime.datetime.strptime(raw, "%Y%m%dT%H%M%SZ")
        return dt.replace(tzinfo=pytz.utc)
    except Exception:
        return None

def _fetch_gdelt(query: str, n: int) -> list[dict]:
    full_query = f"{query} sourcelang:eng"
    url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={quote(full_query)}&mode=ArtList&maxrecords={n}&format=json&sort=DateDesc&timespan=30d"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = None
    retry_delays = [2, 5]
    for attempt in range(len(retry_delays) + 1):
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except HTTPError as e:
            if e.code == 429 and attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            break
        except Exception:
            break
    time.sleep(2)
    if data is None: return []
    results = []
    for item in (data.get("articles") or [])[:n]:
        title = (item.get("title") or "").strip()
        link = (item.get("url") or "").strip()
        pub_dt = _parse_gdelt_seendate(item.get("seendate") or "")
        if title and link:
            results.append({"title": title, "url": link, "pub_dt": pub_dt, "description": ""})
    return results

def _fetch_naver(query: str, n: int) -> list[dict]:
    cid = os.environ.get("NAVER_CLIENT_ID")
    csec = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not csec: return []
    naver_q = query.replace(" OR ", " | ")
    url = f"https://openapi.naver.com/v1/search/news.json?query={quote(naver_q)}&display=100&sort=date"
    req = Request(url, headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec, "User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    results = []
    for item in data.get("items", [])[:n]:
        title = _strip_html(item.get("title", ""))
        link = (item.get("originallink") or item.get("link") or "").strip()
        desc = _strip_html(item.get("description", ""))
        pub_dt = parse_pub_datetime(item.get("pubDate") or "")
        if title and link:
            results.append({"title": title, "url": link, "pub_dt": pub_dt, "description": desc})
    return results

def fetch_articles(query: str, lang: str, n: int = 20) -> list[dict]:
    if lang == "ko":
        items = _fetch_naver(query, n)
        if items: return items
        return _fetch_google(query, lang, n)
    # 영문: GDELT가 시간/정렬 좋아서 primary, 단 description이 비어있음
    # Google News RSS로 description 보완 (제목 normalize 매칭)
    items = _fetch_gdelt(query, n)
    if not items:
        return _fetch_google(query, lang, n)
    # GDELT 결과에 description 비어있으면 Google RSS에서 같은 제목 찾아 채우기
    has_empty_desc = any(not (a.get("description") or "").strip() for a in items)
    if has_empty_desc:
        rss_items = _fetch_google(query, lang, n)
        rss_by_norm = {}
        for r in rss_items:
            norm = normalize_title(r.get("title", ""))
            if norm and r.get("description"):
                rss_by_norm[norm] = r["description"]
        for a in items:
            if (a.get("description") or "").strip():
                continue
            norm = normalize_title(a.get("title", ""))
            if norm in rss_by_norm:
                a["description"] = rss_by_norm[norm]
    return items

def build_period(now: datetime.datetime) -> str:
    today = now.date()
    week_start = today - datetime.timedelta(days=6)
    days_ko = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} ~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}"

def _extract_keywords(title: str) -> set:
    """제목에서 의미 있는 키워드 추출 (2글자 이상 한글/영문 단어, 흔한 단어 제외)."""
    if not title:
        return set()
    # 한글 2글자+ / 영문 3글자+ 단어 추출
    words = re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", title)
    # 흔한 단어/조사 제외 (의미 없는 매칭 방지)
    stopwords = {
        "있다", "한다", "위한", "위해", "통해", "통한", "관련", "대한", "대해",
        "지난", "오는", "올해", "내년", "최근", "이번", "그리고", "또는", "하는",
        "the", "and", "for", "with", "from", "into", "this", "that", "have",
    }
    return {w.lower() for w in words if w.lower() not in stopwords}


def dedup_by_keyword_overlap(articles: list[dict], min_overlap: int = 3) -> list[dict]:
    """
    키워드 오버랩으로 같은 사건 dedup. 발행일 빠른 기사를 우선 남김.
    articles는 pub_dt 오름차순 정렬되어 있다고 가정(아니면 정렬).
    min_overlap개 이상 키워드 겹치면 같은 사건으로 간주.
    """
    if len(articles) < 2:
        return articles
    # 발행일 오름차순 (오래된 게 먼저 = 첫 보도 우선)
    sorted_arts = sorted(articles, key=lambda a: a.get("pub_dt") or datetime.datetime.min.replace(tzinfo=pytz.utc))
    kept = []
    kept_keywords = []
    for a in sorted_arts:
        kw = _extract_keywords(a.get("title", ""))
        is_dup = False
        for prev_kw in kept_keywords:
            if len(kw & prev_kw) >= min_overlap:
                is_dup = True
                break
        if not is_dup:
            kept.append(a)
            kept_keywords.append(kw)
    return kept


def cluster_articles_by_event(articles: list[dict], client: OpenAI | None) -> list[dict]:
    if client is None or len(articles) < 2: return articles
    numbered = "\n".join(f"[{i}] {a['title']}" for i, a in enumerate(articles))
    prompt = f"""다음은 뉴스 제목 목록입니다. 각 줄은 [번호] 제목 형식.

같은 사건을 다룬 제목들을 같은 클러스터로 묶어주세요.

## 같은 사건 판단 기준 (중요)
1. **표현이 달라도 같은 주체 + 같은 액션이면 같은 사건**
   예: "동서발전, 외산 GPU 의존 탈피...국산 AI 생태계 조성 박차"
       "동서발전, 국산 인공지능 성장 생태계 조성 '박차'"
       → 같은 사건 (동서발전이 국산 AI 추진)
2. **다른 매체에서 같은 발표/사건을 다룬 경우** = 같은 클러스터
3. **시점이 며칠 차이나도** 같은 사건이면 묶기 (후속 보도 포함)
4. **핵심 단어 (회사명, 제품명, 액션) 3개 이상 겹치면** 같은 사건일 가능성 매우 높음

다른 사건이면 각각 별도 클러스터.

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
        used = set()
        kept_indices = []
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
    except Exception:
        return articles

def filter_relevant_by_company(company: str, articles: list[dict], client: "OpenAI | None") -> list[dict]:
    if client is None or not articles: return articles
    numbered_items = []
    for i, a in enumerate(articles):
        desc = (a.get("description") or "").replace("\n", " ").strip()[:150]
        numbered_items.append(f"[{i}] 제목: {a.get('title', '')}\n    요약: {desc}")
    numbered = "\n".join(numbered_items)
    prompt = f"""다음은 '{company}' 관련 뉴스 검색 결과입니다. BD 및 시장 동향 파악에 유용한 정보인지 판단하세요.
기사 목록:
{numbered}
## Keep 기준
1. '{company}'가 주도적으로 무언가를 한 기사
2. 업계 트렌드, 시장 분석 중 '{company}'가 의미 있게 언급된 경우
## 제외 기준
- 단순 주식 시황, 증시 마감
- 기계적 공시
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
    except Exception:
        return articles

def filter_furiosa_subject(articles: list[dict], client: "OpenAI | None") -> list[dict]:
    """
    Furiosa AI가 주체로 등장한 기사만 keep. 단순 언급/사이드 등장은 제외.
    경쟁사 기사에 Furiosa가 짧게 언급되는 케이스를 잘못 묶지 않도록 엄격하게.
    """
    if client is None or not articles: return articles
    numbered_items = []
    for i, a in enumerate(articles):
        desc = (a.get("description") or "").replace("\n", " ").strip()[:200]
        numbered_items.append(f"[{i}] 제목: {a.get('title', '')}\n    요약: {desc}")
    numbered = "\n".join(numbered_items)
    prompt = f"""다음은 'Furiosa AI(퓨리오사AI)' 키워드로 검색된 뉴스 기사 목록입니다.
**Furiosa AI가 주체(주어)로 등장한 기사만** keep 하세요.

## Keep 기준 (이 중 하나라도 명확하면 keep)
1. 제목에 'Furiosa', '퓨리오사' 등이 직접 등장하고 그것이 기사의 주된 주체
2. Furiosa AI가 발표/투자/제품/협력 등 행동의 주체로 명시
3. Furiosa AI의 칩, 사업, 인사, 전략을 핵심으로 다룬 기사

## 제외 기준 (이 중 하나라도 해당하면 제외)
1. **다른 회사가 주체이고 Furiosa AI는 본문 중 한 문단 정도로 사이드 언급된 경우**
   예: "리벨리온이 데이터센터 구축... 한편 퓨리오사AI도 데이터센터에 입주했다"
   → 주체는 리벨리온, 제외.
2. 업계 트렌드 기사에서 'NVIDIA, 리벨리온, 퓨리오사' 식으로 나열만 된 경우
3. 단순 주식 시황, 증시 마감
4. 정부/협회 정책 발표에서 여러 기업 중 하나로 언급된 경우

## 판단 원칙
**제목과 요약에서 Furiosa가 주인공인가?**가 핵심 질문.
애매하면 제외. 보수적으로 판단.

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
    except Exception:
        return articles


def to_output(a: dict, kst: pytz.BaseTzInfo, include_summary: bool = False) -> dict:
    out = {
        "title": a["title"],
        "url": a["url"],
        "date": format_date_kst(a.get("pub_dt"), kst),
    }
    if include_summary:
        out["summary"] = a.get("summary", "")
    return out

def generate_competitor_summaries(articles_with_company: list[tuple], client: "OpenAI | None") -> dict:
    """경쟁사 기사에 대해 요약만 생성. BD 시점은 생성 안 함."""
    if client is None or not articles_with_company: return {}
    items_in = []
    for i, (company, a) in enumerate(articles_with_company):
        items_in.append({
            "id": i, "company": company, "title": a.get("title", ""), "snippet": (a.get("description") or "")[:240]
        })
    prompt = f"""다음은 AI 반도체 경쟁사 뉴스 기사 목록입니다.

각 기사에 대해 한국어 요약을 만들어 주세요:
- "summary": 3문장, 사실 위주, 250~350자. 무엇/언제/어떻게를 포함.

## 절대 금지 표현 (이런 일반론 문장은 쓰지 말 것)
- "~분야의 경쟁력을 높이는 데 중요한 역할을 할 것이다"
- "~기술의 발전에 기여할 것으로 기대된다"
- "~시장에서의 경쟁력을 높이는 데 중요한 역할을 할 것이다"
- "~산업 전반의 혁신을 촉진할 것으로 기대된다"
- "기대된다", "전망된다" 같은 막연한 미래 추측

## 작성 원칙
1. 제목과 snippet에 있는 사실만 활용. 없는 정보 만들어내지 말 것.
2. 정보가 부족하면 솔직하게 "본문 정보 부족 — 원문 확인 필요"로 마무리.
3. 추측 금지. 일반론 절대 금지.

Input articles:
{json.dumps(items_in, ensure_ascii=False)}

Return JSON ONLY:
{{"items": [{{"id": 0, "summary": "..."}}, ...]}}"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)
        out: dict = {}
        for entry in data.get("items", []):
            idx = entry.get("id")
            if not isinstance(idx, int) or not (0 <= idx < len(articles_with_company)): continue
            company, a = articles_with_company[idx]
            out[(company, a["url"])] = (entry.get("summary") or "").strip()
        return out
    except Exception:
        return {}

def generate_furiosa_summaries(articles: list[dict], client: "OpenAI | None") -> dict:
    if client is None or not articles: return {}
    items_in = []
    for i, a in enumerate(articles):
        items_in.append({"id": i, "title": a.get("title", ""), "snippet": (a.get("description") or "")[:240]})
    prompt = f"""다음은 Furiosa AI 관련 뉴스 기사 목록입니다.

각 기사에 대해 한국어 요약을 만들어 주세요:
- "summary": 3문장, 사실 위주로 핵심 내용을 충분히 풀어쓰기.

## 절대 금지 표현
- "~분야의 경쟁력을 높이는 데 중요한 역할을 할 것이다"
- "~기술의 발전에 기여할 것으로 기대된다"
- "~산업 전반의 혁신을 촉진할 것으로 기대된다"
- 막연한 추측/일반론 마무리 문장

## 작성 원칙
1. 제목과 snippet에 있는 사실만 활용. 없는 정보 만들어내지 말 것.
2. 정보가 부족하면 "본문 정보 부족 — 원문 확인 필요"로 마무리.

Input articles:
{json.dumps(items_in, ensure_ascii=False)}

Return JSON ONLY:
{{"items": [{{"id": 0, "summary": "..."}}, ...]}}"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)
        out: dict = {}
        for entry in data.get("items", []):
            idx = entry.get("id")
            if not isinstance(idx, int) or not (0 <= idx < len(articles)): continue
            a = articles[idx]
            out[a["url"]] = (entry.get("summary") or "").strip()
        return out
    except Exception:
        return {}

def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    now_utc = now.astimezone(pytz.utc)
    
    # 일간: 오늘 KST 00시 기준 (예: 5/21 실행 → 5/21 00:00 KST 이후 기사만 포함)
    daily_start_kst = kst.localize(datetime.datetime.combine(
        now.date(),
        datetime.time(0, 0)
    ))
    daily_cutoff = daily_start_kst.astimezone(pytz.utc)
    # 주간: 오늘 - 6일의 KST 자정 0시 기준 (예: 5/21 실행 → 5/15 00:00 KST 이후 기사만 포함)
    weekly_start_kst = kst.localize(datetime.datetime.combine(
        now.date() - datetime.timedelta(days=6),
        datetime.time(0, 0)
    ))
    weekly_cutoff = weekly_start_kst.astimezone(pytz.utc)
    
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    token = os.environ.get("GITHUB_TOKEN")
    client = OpenAI(base_url="https://models.inference.ai.azure.com", api_key=token) if token else None

    # ── 1. Competitor 뉴스 ──
    COMPETITOR_CUTOFF_DAYS = 30
    COMPETITOR_MAX_ITEMS = 3
    competitor_cutoff = now_utc - datetime.timedelta(days=COMPETITOR_CUTOFF_DAYS)

    companies_raw = []
    for co in COMPANIES:
        fetched = []
        seen_urls = set()
        for query, lang in co["queries"]:
            for a in fetch_articles(query, lang, n=100):
                if a["url"] not in seen_urls:
                    seen_urls.add(a["url"])
                    fetched.append(a)
        recent = [a for a in fetched if a.get("pub_dt") is not None and a["pub_dt"] >= competitor_cutoff]
        relevant = filter_relevant_by_company(co["name"], recent, client)
        deduped = cluster_articles_by_event(relevant, client)
        deduped.sort(key=lambda a: a["pub_dt"], reverse=True)
        companies_raw.append({"name": co["name"], "region": co["region"], "articles": deduped[:COMPETITOR_MAX_ITEMS]})

    all_pairs = [(c["name"], a) for c in companies_raw for a in c["articles"]]
    summaries = generate_competitor_summaries(all_pairs, client)
    for c in companies_raw:
        for a in c["articles"]:
            key = (c["name"], a["url"])
            if key in summaries:
                a["summary"] = summaries[key]

    companies_out = [{"name": c["name"], "region": c["region"], "items": [to_output(a, kst, include_summary=True) for a in c["articles"]]} for c in companies_raw]

    # ── 2. Furiosa 뉴스 ──
    all_furiosa = []
    seen_urls, seen_titles = set(), set()
    for query, lang in FURIOSA_QUERIES:
        for a in fetch_articles(query, lang, n=100):
            if a["url"] in seen_urls: continue
            norm = normalize_title(a["title"])
            if norm and norm in seen_titles: continue
            seen_urls.add(a["url"])
            if norm: seen_titles.add(norm)
            all_furiosa.append(a)

    def in_window(a: dict, cutoff: datetime.datetime) -> bool:
        return a.get("pub_dt") is not None and a["pub_dt"] >= cutoff

    # Furiosa가 주체인 기사만 통과시킴 (사이드 언급된 경쟁사 기사 제외)
    in_weekly_window_raw = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    if client and in_weekly_window_raw:
        kept_subject = filter_furiosa_subject(in_weekly_window_raw, client)
        kept_subject_urls = {a["url"] for a in kept_subject}
        all_furiosa = [a for a in all_furiosa if a["url"] in kept_subject_urls or not in_window(a, weekly_cutoff)]

    in_weekly_window = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    if client:
        deduped = cluster_articles_by_event(in_weekly_window, client)
        deduped_urls = {a["url"] for a in deduped}
        all_furiosa = [a for a in all_furiosa if a["url"] in deduped_urls or not in_window(a, weekly_cutoff)]

    # 키워드 오버랩 dedup (LLM 클러스터링 보완): 표현 달라도 같은 사건 잡아냄
    in_weekly_window_2 = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    kept = dedup_by_keyword_overlap(in_weekly_window_2, min_overlap=3)
    kept_urls = {a["url"] for a in kept}
    all_furiosa = [a for a in all_furiosa if a["url"] in kept_urls or not in_window(a, weekly_cutoff)]

    # ── 2.5 일간/주간 분리 ──
    furiosa_daily_raw = sorted([a for a in all_furiosa if in_window(a, daily_cutoff)], key=lambda x: x["pub_dt"], reverse=True)[:5]
    furiosa_weekly_raw = sorted([a for a in all_furiosa if in_window(a, weekly_cutoff) and not in_window(a, daily_cutoff)], key=lambda x: x["pub_dt"], reverse=True)

    furiosa_articles = furiosa_daily_raw + furiosa_weekly_raw
    furiosa_summaries = generate_furiosa_summaries(furiosa_articles, client)
    for a in furiosa_articles:
        if a["url"] in furiosa_summaries:
            a["summary"] = furiosa_summaries[a["url"]]

    # 🚨 3. 일간 그룹핑 
    furiosa_daily_group = {}
    for a in furiosa_daily_raw:
        date_key = format_date_with_weekday(a.get("pub_dt"), kst)
        if date_key not in furiosa_daily_group:
            furiosa_daily_group[date_key] = []
        furiosa_daily_group[date_key].append(to_output(a, kst, include_summary=True))

    # 🚨 4. 주간 그룹핑 (하루 최대 3개 제한)
    furiosa_weekly_group = {}
    daily_count = {}
    for a in furiosa_weekly_raw:
        date_key = format_date_with_weekday(a.get("pub_dt"), kst)
        if daily_count.get(date_key, 0) < 3:
            if date_key not in furiosa_weekly_group:
                furiosa_weekly_group[date_key] = []
            furiosa_weekly_group[date_key].append(to_output(a, kst, include_summary=True))
            daily_count[date_key] = daily_count.get(date_key, 0) + 1

    report = {
        "period": build_period(now),
        "updated_at": now.isoformat(),
        "furiosa_daily": furiosa_daily_group,
        "furiosa_weekly": furiosa_weekly_group,
        "companies": companies_out,
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"Done. Update completed.")

if __name__ == "__main__":
    main()
