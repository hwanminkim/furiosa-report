#!/usr/bin/env python3
"""
Daily report updater.
"""
import datetime
import email.utils
import html
import json
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import urlopen, Request

import pytz
from openai import OpenAI

try:
    import trafilatura
except ImportError:
    trafilatura = None

REPO_ROOT = Path(__file__).parent.parent
REPORT_PATH = REPO_ROOT / "report.json"

FURIOSA_BASE_URL = "https://endpoint.access.furiosa.dev/v1"
EXAONE_MODEL = "furiosa-ai/EXAONE-4.0-32B-FP8"
EMBEDDING_MODEL = "furiosa-ai/Qwen3-Embedding-8B"
SIMILARITY_THRESHOLD = 0.85
COMPETITOR_SIMILARITY_THRESHOLD = 0.85

COMPANIES = [
    {"name": "NVIDIA", "region": "global", "aliases": ["NVIDIA", "엔비디아"], "queries": [("NVIDIA", "en")]},
    {"name": "Tenstorrent", "region": "global", "aliases": ["Tenstorrent", "텐스토렌트"], "queries": [("Tenstorrent", "en")]},
    {"name": "SambaNova", "region": "global", "aliases": ["SambaNova", "삼바노바"], "queries": [("SambaNova", "en")]},
    {"name": "Cerebras", "region": "global", "aliases": ["Cerebras", "세레브라스"], "queries": [("Cerebras", "en")]},
    {"name": "Rebellions", "region": "korea", "aliases": ["Rebellions", "리벨리온"], "queries": [("리벨리온", "ko"), ("Rebellions", "en")]},
    {"name": "DeepX", "region": "korea", "aliases": ["DeepX", "딥엑스"], "queries": [("딥엑스", "ko"), ("DeepX", "en")]},
    {"name": "HyperAccel", "region": "korea", "aliases": ["HyperAccel", "하이퍼엑셀"], "queries": [("하이퍼엑셀", "ko"), ("HyperAccel", "en")]},
    {"name": "Mobilint", "region": "korea", "aliases": ["Mobilint", "모빌린트"], "queries": [("모빌린트", "ko"), ("Mobilint", "en")]},
]

FURIOSA_QUERIES = [
    ('furiosa ai OR furiosaai OR "Furiosa AI" chip', "en"),
    ('퓨리오사ai OR 퓨리오사AI OR FuriosaAI', "ko"),
]

FURIOSA_ALIASES = ["Furiosa", "퓨리오사", "FuriosaAI", "furiosaai", "백준호"]

def title_contains_alias(title: str, aliases: list[str]) -> bool:
    if not title or not aliases: return False
    t_lower = title.lower()
    return any(alias.lower() in t_lower for alias in aliases)

_STOCK_NOISE_PATTERNS = [
    re.compile(r'\b[\d,]+\s+shares?\b', re.I),
    re.compile(r'\bshares?\s+(purchased|acquired|bought|sold|of)\b', re.I),
    re.compile(r'\b(buys|acquires|purchases|sells|sold)\s+[\d,]+\s+shares?\b', re.I),
    re.compile(r'\$[A-Z]{2,5}\b'),
    re.compile(r'\b(raises|lowers|cuts|trims|increases|reduces|boosts|trims)\s+(stake|position|holdings)\b', re.I),
    re.compile(r'\b(stock|price)\s+(target|forecast|rating)\b', re.I),
    re.compile(r'\bstake\s+in\b', re.I),
    re.compile(r'\b(NYSE|NASDAQ):', re.I),
    # 한국 주식/시황 패턴
    re.compile(r'관련주', re.I),
    re.compile(r'\[특징주\]'),
    re.compile(r'상한가|하한가'),
    re.compile(r'주가\s*(급등|급락|상승|하락|강세|약세)'),
]

def is_stock_noise(title: str) -> bool:
    if not title: return False
    return any(p.search(title) for p in _STOCK_NOISE_PATTERNS)

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

_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_BODY_WS_RE = re.compile(r"\s+")
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_body_cache: dict[str, str] = {}
_body_cache_lock = threading.Lock()
_gnews_url_cache: dict[str, str] = {}
_gnews_url_lock = threading.Lock()


def resolve_google_news_url(url: str) -> str:
    """Resolve a news.google.com/rss/articles/... redirect to the real article URL.

    Google News no longer embeds the source URL in the path; it must be fetched
    via the internal batchexecute endpoint using the page's id/timestamp/signature.
    Returns the original url on any failure.
    """
    if "news.google.com" not in url:
        return url
    with _gnews_url_lock:
        if url in _gnews_url_cache:
            return _gnews_url_cache[url]
    resolved = url
    try:
        req = Request(url, headers={"User-Agent": _BROWSER_UA})
        with urlopen(req, timeout=10) as resp:
            page = resp.read().decode("utf-8", errors="ignore")
        aid = re.search(r'data-n-a-id="([^"]+)"', page)
        sig = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts  = re.search(r'data-n-a-ts="([^"]+)"', page)
        if aid and sig and ts:
            inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
                     'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
                     f'"{aid.group(1)}",{ts.group(1)},"{sig.group(1)}"]')
            body = urlencode({"f.req": json.dumps([[["Fbv4je", inner]]])})
            req2 = Request("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                           data=body.encode(),
                           headers={"User-Agent": _BROWSER_UA,
                                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
            with urlopen(req2, timeout=10) as resp2:
                txt = resp2.read().decode("utf-8", errors="ignore")
            m = re.search(r'\[\\"garturlres\\",\\"(.*?)\\"', txt)
            if m:
                resolved = m.group(1).encode().decode("unicode_escape")
    except Exception:
        pass
    with _gnews_url_lock:
        _gnews_url_cache[url] = resolved
    return resolved


def resolve_article_urls(articles: list[dict], max_workers: int = 10) -> None:
    """Resolve google-news redirect URLs in-place for the given articles."""
    targets = [a for a in articles if a.get("url") and "news.google.com" in a["url"]]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(resolve_google_news_url, a["url"]): a for a in targets}
        for f in as_completed(futures):
            a = futures[f]
            try:
                a["url"] = f.result()
            except Exception:
                pass


def _extract_body_regex(raw: str, max_chars: int) -> str:
    """Fallback body extraction: join <p> paragraphs, skipping short nav snippets."""
    raw = _SCRIPT_STYLE_RE.sub(" ", raw)
    paragraphs = []
    for m in _PARAGRAPH_RE.finditer(raw):
        t = _BODY_WS_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", m.group(1)))).strip()
        if len(t) >= 40:
            paragraphs.append(t)
    para_text = " ".join(paragraphs)
    if len(para_text) >= 200:
        return para_text[:max_chars]
    text = _BODY_WS_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub(" ", raw))).strip()
    return text[:max_chars]


def fetch_article_body(url: str, timeout: int = 8, max_chars: int = 4000) -> str:
    with _body_cache_lock:
        if url in _body_cache:
            return _body_cache[url]
    result = ""
    try:
        req = Request(url, headers={"User-Agent": _BROWSER_UA})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(500000).decode("utf-8", errors="ignore")
        # Prefer trafilatura's main-content extraction (strips nav/ads/boilerplate).
        if trafilatura is not None:
            try:
                extracted = trafilatura.extract(
                    raw,
                    include_comments=False,
                    include_tables=False,
                    no_fallback=False,
                )
                if extracted:
                    extracted = _BODY_WS_RE.sub(" ", extracted).strip()
                    if len(extracted) >= 200:
                        result = extracted[:max_chars]
            except Exception:
                result = ""
        # Fall back to the regex <p>-join when trafilatura is unavailable or too thin.
        if not result:
            result = _extract_body_regex(raw, max_chars)
    except Exception:
        pass
    with _body_cache_lock:
        _body_cache[url] = result
    return result

def prefetch_bodies(articles: list[dict], max_workers: int = 10) -> None:
    urls = [a["url"] for a in articles if a.get("url")]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_article_body, url) for url in urls]
        for f in as_completed(futures):
            f.result()

def _article_body_for_prompt(a: dict) -> str:
    body = fetch_article_body(a.get("url", ""))
    if body:
        return body[:3000]
    return (a.get("description") or "").replace("\n", " ").strip()[:500]

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
    items = _fetch_gdelt(query, n)
    if not items:
        return _fetch_google(query, lang, n)
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

_KOREAN_PARTICLES = (
    "에서는", "에서도", "에서", "에게도", "에게서", "에게", "한테",
    "까지", "부터", "조차", "마저", "에는", "에도",
    "으로", "라고", "이라고",
    "은", "는", "이", "가", "을", "를", "와", "과", "의", "도", "만",
    "에", "로", "서",
)

def _normalize_korean(word: str) -> str:
    """Strip common Korean particles from word end to merge variants like 퓨리오사/퓨리오사와, 동서발전/동서발전서."""
    if not word: return word
    for p in _KOREAN_PARTICLES:
        if word.endswith(p) and len(word) - len(p) >= 2:
            return word[:-len(p)]
    return word

def _extract_keywords(title: str) -> set:
    if not title:
        return set()
    words = re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", title)
    stopwords = {
        "있다", "한다", "위한", "위해", "통해", "통한", "관련", "대한", "대해",
        "지난", "오는", "올해", "내년", "최근", "이번", "그리고", "또는", "하는",
        "the", "and", "for", "with", "from", "into", "this", "that", "have",
    }
    result = set()
    for w in words:
        lw = w.lower()
        if lw in stopwords: continue
        if re.match(r"^[가-힣]+$", lw):
            lw = _normalize_korean(lw)
            if len(lw) < 2: continue
        result.add(lw)
    return result

def _dedup_by_keyword_overlap(articles: list[dict], min_overlap: int = 3) -> list[dict]:
    if len(articles) < 2:
        return articles
    kept = []
    kept_keywords = []
    for a in articles:
        kw = _extract_keywords(a.get("title", ""))
        is_dup = any(len(kw & prev_kw) >= min_overlap for prev_kw in kept_keywords)
        if not is_dup:
            kept.append(a)
            kept_keywords.append(kw)
    return kept

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

def _get_embeddings(texts: list[str], client: OpenAI) -> list[list[float]]:
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [e.embedding for e in resp.data]

def dedup_by_semantic_similarity(articles: list[dict], embedding_client: "OpenAI | None", threshold: float = SIMILARITY_THRESHOLD) -> list[dict]:
    if len(articles) < 2:
        return articles
    sorted_arts = sorted(articles, key=lambda a: a.get("pub_dt") or datetime.datetime.min.replace(tzinfo=pytz.utc))
    if embedding_client is None:
        return _dedup_by_keyword_overlap(sorted_arts)
    titles = [a.get("title", "") for a in sorted_arts]
    try:
        embeddings = _get_embeddings(titles, embedding_client)
    except Exception:
        return _dedup_by_keyword_overlap(sorted_arts)
    kept = []
    kept_embeddings: list[list[float]] = []
    for a, emb in zip(sorted_arts, embeddings):
        if any(_cosine_similarity(emb, prev) >= threshold for prev in kept_embeddings):
            continue
        kept.append(a)
        kept_embeddings.append(emb)
    return kept


def cluster_articles_by_event(articles: list[dict], client: "OpenAI | None") -> list[dict]:
    if client is None or len(articles) < 2: return articles
    numbered = "\n".join(f"[{i}] {a['title']}" for i, a in enumerate(articles))
    prompt = f"""다음은 뉴스 제목 목록입니다. 각 줄은 [번호] 제목 형식.

같은 사건을 다룬 제목들을 같은 클러스터로 묶어주세요.

## 같은 사건 (= 같은 클러스터로 묶기) 판단 기준
**다음 조건을 모두 만족해야 같은 사건입니다.**
1. **같은 주체** (동일한 회사/기관이 발표/행동)
2. **같은 액션** (예: 같은 협약 체결, 같은 제품 발표, 같은 행사 개최)
3. **같은 시점** (보통 같은 날짜이거나 며칠 차이 후속 보도)

예시 (같은 사건):
- "동서발전, 외산 GPU 의존 탈피...국산 AI 생태계 조성 박차"
- "동서발전, 국산 인공지능 성장 생태계 조성 '박차'"
- "코난테크놀로지·동서발전·퓨리오사, 국산 AI 인프라 실증 협력"
→ 모두 동서발전-퓨리오사-코난 협약 사건. **같은 클러스터.**

## 다른 사건 (= 별도 클러스터로 유지) 판단 기준
**아래 중 하나라도 다르면 다른 사건입니다.**
- 액션이 다름 (협약 체결 vs IPO 평가 vs 인터뷰 vs 행사 알림)
- 주체가 다름 (퓨리오사 vs 망고부스트-퓨리오사 협력)
- 다루는 내용이 다름 (정부 펀드 분석 vs 회사 간 협력)

## 매우 중요: 다음은 절대 같은 사건이 아닙니다
- 같은 회사 등장하지만 다른 액션: "백준호 대표 인터뷰" vs "퓨리오사 IPO 평가" → 다른 사건
- 같은 주제이지만 다른 회사: "동서발전 협약" vs "유라클 NPU 연동" → 다른 사건
- 다른 협력: "망고부스트-퓨리오사 협력" vs "유라클-퓨리오사 연동" → 다른 사건
- 트렌드 기사 vs 구체적 발표: "K-엔비디아 분석" vs "퓨리오사 협약 발표" → 다른 사건
- 일반 시장 분석 vs 특정 사건: "AI 최적화 기업 주목" vs "동서발전 협약" → 다른 사건

## 판단 원칙
**애매하면 다른 사건으로 분리.** 같은 사건은 확실할 때만 묶기. 무리한 클러스터링 금지.

제목 목록:
{numbered}

응답 형식 (오직 JSON):
{{"clusters": [[0, 2], [1], [3, 4, 5]]}}"""
    try:
        resp = client.chat.completions.create(
            model=EXAONE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.1,

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
            ko_reps = [i for i in valid if re.search(r'[가-힣]', articles[i].get('title', ''))]
            en_reps = [i for i in valid if not re.search(r'[가-힣]', articles[i].get('title', ''))]
            reps = []
            if ko_reps: reps.append(min(ko_reps))
            if en_reps: reps.append(min(en_reps))
            if not reps: reps = [min(valid)]
            for rep in reps:
                if rep not in used: kept_indices.append(rep)
            for i in valid: used.add(i)
        for i in range(len(articles)):
            if i not in used: kept_indices.append(i)
        kept_indices.sort()
        return [articles[i] for i in kept_indices]
    except Exception:
        return articles

RELEVANCE_THRESHOLD = 4  # 0~3점은 리포트 제외, 4~10점만 기재 대상

def _passes_relevance_gate(a: dict, aliases: list[str]) -> bool:
    # 제목뿐 아니라 RSS 요약 스니펫까지 보고 게이트 통과 여부 판단.
    # (제목에 회사명이 없어도 본문/요약에서 비중 있게 다뤄지면 LLM 채점 단계로 넘긴다)
    text = (a.get("title", "") + " " + (a.get("description") or "")).strip()
    return title_contains_alias(text, aliases) and not is_stock_noise(a.get("title", ""))


def _relevance_prompt(subject: str, numbered: str) -> str:
    return f"""다음은 '{subject}' 관련 뉴스 검색 결과입니다. 당신은 '{subject}'의 경쟁 동향을 추적하는 BD(사업개발) 담당자를 위해, 각 기사가 동향 리포트에 실릴 가치가 있는지 0~10점으로 평가합니다.

핵심 질문: "이 기사가 '{subject}'의 사업·기술·시장 동향을 이해하는 데 의미 있는 시그널인가?"

## 점수 가이드 (딱 떨어지는 규칙이 아니라 연속적인 판단입니다. 경계는 유연하게.)
- **7~10** — '{subject}'가 기사의 핵심. 직접 한 발표·계약·투자·제품 출시, 또는 '{subject}' 자체에 대한 심층 분석·기업가치·IPO 전망. **제목에 회사명이 없어도 본문에서 핵심으로 다뤄지면 이 구간.**
- **4~6** — '{subject}'가 다른 주체와 함께한 협력·MOU·연동·도입·채택, 또는 비중 있게 다뤄지지만 단독 주인공은 아닌 경우.
- **0~3** — 여러 회사 중 하나로 나열되거나 곁다리 언급, 펀드/정책 수혜자 목록, 경쟁사 비교 기사, 단순 시황·주가·관련주·특징주, 제품이 부품으로 한 줄 언급 → 리포트 제외.

## 판단 원칙
- "회사가 문법상 주체(actor)냐 객체냐"를 기계적으로 따지지 말 것. 기준은 **BD 담당자에게 유용한 시그널인가**이다.
- 제목에 회사명이 없어도 본문에서 비중 있게 다뤄지면 그 비중대로 평가하라.
- 각 기사마다 **먼저 한 줄 이유(reason)를 쓰고, 그 근거에 따라 점수**를 매겨라.
- 확신이 안 서면 무작정 낮추지 말고 중간(4~6)에 둘 것.

기사 목록:
{numbered}

응답 형식 (오직 JSON):
{{"scores": [{{"id": 0, "reason": "한 줄 이유", "score": 8}}, ...]}}"""


def score_relevance(subject: str, aliases: list[str], articles: list[dict], client: "OpenAI | None") -> list[dict]:
    """게이트를 통과한 기사에 LLM 관련도 점수(0~10)를 a['relevance']로 부여하고
    THRESHOLD 이상만 반환한다. client가 없으면 게이트 통과분에 중립 점수(5)를 준다."""
    if not articles:
        return []
    gated = [a for a in articles if _passes_relevance_gate(a, aliases)]
    if not gated:
        return []
    if client is None:
        for a in gated:
            a["relevance"] = 5
        return gated
    prefetch_bodies(gated)
    numbered = "\n".join(
        f"[{i}] 제목: {a.get('title', '')}\n    본문: {_article_body_for_prompt(a)}"
        for i, a in enumerate(gated)
    )
    try:
        resp = client.chat.completions.create(
            model=EXAONE_MODEL,
            messages=[{"role": "user", "content": _relevance_prompt(subject, numbered)}],
            max_tokens=1500,
            temperature=0.1,
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(m.group() if m else raw)
        by_id = {}
        for e in data.get("scores", []):
            if isinstance(e.get("id"), int) and 0 <= e["id"] < len(gated) and isinstance(e.get("score"), (int, float)):
                by_id[e["id"]] = int(e["score"])
    except Exception:
        # 채점 자체가 실패하면 과도한 탈락을 막기 위해 게이트 통과분을 중립 점수로 유지
        for a in gated:
            a["relevance"] = 5
        return gated
    kept = []
    for i, a in enumerate(gated):
        score = by_id.get(i, 5)  # 응답에서 누락된 id는 중립 처리
        a["relevance"] = score
        if score >= RELEVANCE_THRESHOLD:
            kept.append(a)
    return kept


def tiered_pick(articles: list[dict], n: int) -> list[dict]:
    """7~10점을 날짜순으로 먼저 채우고, n개에 못 미치면 4~6점으로 보충한다."""
    def by_date(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda a: a.get("pub_dt") or datetime.datetime.min.replace(tzinfo=pytz.utc), reverse=True)
    high = by_date([a for a in articles if a.get("relevance", 0) >= 7])
    mid = by_date([a for a in articles if 4 <= a.get("relevance", 0) <= 6])
    picked = high[:n]
    if len(picked) < n:
        picked += mid[:n - len(picked)]
    return picked


def filter_relevant_by_company(company: str, aliases: list[str], articles: list[dict], client: "OpenAI | None") -> list[dict]:
    return score_relevance(company, aliases, articles, client)

def filter_furiosa_subject(articles: list[dict], client: "OpenAI | None") -> list[dict]:
    return score_relevance("Furiosa AI(퓨리오사AI)", FURIOSA_ALIASES, articles, client)


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
    if client is None or not articles_with_company: return {}
    prefetch_bodies([a for _, a in articles_with_company])
    out: dict = {}
    CHUNK = 6  # keep each call's output well under max_tokens to avoid truncation
    for start in range(0, len(articles_with_company), CHUNK):
        chunk = articles_with_company[start:start + CHUNK]
        items_in = []
        for offset, (company, a) in enumerate(chunk):
            body = _article_body_for_prompt(a)
            items_in.append({
                "id": offset, "company": company, "title": a.get("title", ""), "snippet": body[:3000]
            })
        prompt = f"""다음은 AI 반도체 경쟁사 뉴스 기사 목록입니다.

각 기사에 대해 한국어 요약을 만들어 주세요:
- "summary": 4~5문장, 사실 위주, 350~450자.

## 작성 원칙
1. 제목과 snippet에 있는 사실만 활용. 핵심 사실(주체, 액션, 협력 대상, 금액·규모·수치, 시점, 목적)을 빠짐없이 담을 것.
2. 일반론·미사여구로 분량을 채우지 말 것. 사실을 구체적으로 풀어 4~5문장을 채울 것.
3. 정보가 적더라도 제목과 snippet에서 추출 가능한 사실만으로 자연스러운 요약을 작성. 정보 부족을 언급하지 말 것.

## 절대 금지 표현
- "~분야의 경쟁력을 높이는 데 중요한 역할을 할 것이다"
- "~기술의 발전에 기여할 것으로 기대된다"
- "기대된다", "전망된다" 같은 막연한 미래 추측
- "본문 정보 부족", "원문 확인 필요", "추가 정보 필요" 등 정보 부족을 명시하는 문구

Input articles:
{json.dumps(items_in, ensure_ascii=False)}

Return JSON ONLY:
{{"items": [{{"id": 0, "summary": "..."}}, ...]}}"""
        try:
            resp = client.chat.completions.create(
                model=EXAONE_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8000,
                temperature=0.3,
            )
            raw = resp.choices[0].message.content or ""
            m = re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(m.group() if m else raw)
            for entry in data.get("items", []):
                idx = entry.get("id")
                if not isinstance(idx, int) or not (0 <= idx < len(chunk)): continue
                company, a = chunk[idx]
                out[(company, a["url"])] = (entry.get("summary") or "").strip()
        except Exception:
            continue
    return out

def generate_furiosa_summaries(articles: list[dict], client: "OpenAI | None") -> dict:
    if client is None or not articles: return {}
    prefetch_bodies(articles)
    items_in = []
    for i, a in enumerate(articles):
        body = _article_body_for_prompt(a)
        items_in.append({"id": i, "title": a.get("title", ""), "snippet": body[:3000]})
    prompt = f"""다음은 Furiosa AI 관련 뉴스 기사 목록입니다.

각 기사에 대해 한국어 요약을 만들어 주세요:
- "summary": 4~5문장, 사실 위주, 350~450자.

진부한 일반론 금지. 핵심 사실(주체, 액션, 협력 대상, 금액·규모·수치, 시점, 목적)을 빠짐없이 담을 것. 일반론·미사여구로 분량을 채우지 말고 사실을 구체적으로 풀어 4~5문장을 채울 것.

## 절대 금지 표현
- "본문 정보 부족", "원문 확인 필요", "추가 정보 필요" 등 정보 부족을 명시하는 문구
- "기대된다", "전망된다" 같은 막연한 미래 추측

정보가 적더라도 제목과 snippet에서 추출 가능한 사실만으로 자연스러운 요약을 작성. 정보 부족을 언급하지 말 것.

Input articles:
{json.dumps(items_in, ensure_ascii=False)}

Return JSON ONLY:
{{"items": [{{"id": 0, "summary": "..."}}, ...]}}"""
    try:
        resp = client.chat.completions.create(
            model=EXAONE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8000,
            temperature=0.3,

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

    daily_start_kst = kst.localize(datetime.datetime.combine(
        now.date(), datetime.time(0, 0)
    ))
    daily_cutoff = daily_start_kst.astimezone(pytz.utc)
    weekly_start_kst = kst.localize(datetime.datetime.combine(
        now.date() - datetime.timedelta(days=6), datetime.time(0, 0)
    ))
    weekly_cutoff = weekly_start_kst.astimezone(pytz.utc)

    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    exaone_key = os.environ.get("FURIOSA_EXAONE_API_KEY")
    embedding_key = os.environ.get("FURIOSA_EMBEDDING_API_KEY")

    exaone_client = OpenAI(base_url=FURIOSA_BASE_URL, api_key=exaone_key) if exaone_key else None
    embedding_client = OpenAI(base_url=FURIOSA_BASE_URL, api_key=embedding_key) if embedding_key else None

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
        print(f"[{co['name']}] fetched={len(fetched)}, recent({COMPETITOR_CUTOFF_DAYS}d)={len(recent)}")

        relevant = filter_relevant_by_company(co["name"], co["aliases"], recent, exaone_client)
        print(f"[{co['name']}] after relevance filter: {len(relevant)} (dropped {len(recent) - len(relevant)})")

        before_dedup = relevant
        relevant = dedup_by_semantic_similarity(relevant, embedding_client, threshold=COMPETITOR_SIMILARITY_THRESHOLD)
        print(f"[{co['name']}] after semantic dedup: {len(relevant)} (dropped {len(before_dedup) - len(relevant)})")
        dedup_urls = {a["url"] for a in relevant}
        for a in before_dedup:
            if a["url"] not in dedup_urls:
                print(f"  [DROPPED by dedup] {a['title'][:80]}")

        before_cluster = relevant
        deduped = cluster_articles_by_event(relevant, exaone_client)
        print(f"[{co['name']}] after cluster: {len(deduped)} (dropped {len(before_cluster) - len(deduped)})")
        cluster_urls = {a["url"] for a in deduped}
        for a in before_cluster:
            if a["url"] not in cluster_urls:
                print(f"  [DROPPED by cluster] {a['title'][:80]}")

        picked = tiered_pick(deduped, COMPETITOR_MAX_ITEMS)
        print(f"[{co['name']}] FINAL: {len(picked)} (capped at {COMPETITOR_MAX_ITEMS}, 7~10점 우선 → 4~6점 보충)")
        companies_raw.append({"name": co["name"], "region": co["region"], "articles": picked})

    all_pairs = [(c["name"], a) for c in companies_raw for a in c["articles"]]
    resolve_article_urls([a for _, a in all_pairs])  # google-news redirects → real article URLs
    summaries = generate_competitor_summaries(all_pairs, exaone_client)
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
        before = len(all_furiosa)
        for a in fetch_articles(query, lang, n=100):
            if a["url"] in seen_urls: continue
            norm = normalize_title(a["title"])
            if norm and norm in seen_titles: continue
            seen_urls.add(a["url"])
            if norm: seen_titles.add(norm)
            all_furiosa.append(a)
        print(f"[furiosa] query='{query[:40]}' lang={lang} → {len(all_furiosa) - before} new (total {len(all_furiosa)})")

    def in_window(a: dict, cutoff: datetime.datetime) -> bool:
        return a.get("pub_dt") is not None and a["pub_dt"] >= cutoff

    in_weekly_window_raw = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    print(f"[furiosa] after weekly window filter: {len(in_weekly_window_raw)}")
    for a in in_weekly_window_raw:
        pub_str = a['pub_dt'].astimezone(kst).strftime('%m-%d %H:%M') if a.get('pub_dt') else 'no_date'
        print(f"  [in_window] {pub_str} | {a['title'][:80]}")

    if exaone_client and in_weekly_window_raw:
        kept_subject = filter_furiosa_subject(in_weekly_window_raw, exaone_client)
        print(f"[furiosa] after filter_furiosa_subject: {len(kept_subject)} (dropped {len(in_weekly_window_raw) - len(kept_subject)})")
        kept_urls_subj = {a["url"] for a in kept_subject}
        for a in in_weekly_window_raw:
            if a["url"] not in kept_urls_subj:
                print(f"  [DROPPED by filter] {a['title'][:80]}")
        kept_subject_urls = {a["url"] for a in kept_subject}
        all_furiosa = [a for a in all_furiosa if a["url"] in kept_subject_urls or not in_window(a, weekly_cutoff)]

    in_weekly_window = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    if exaone_client:
        deduped = cluster_articles_by_event(in_weekly_window, exaone_client)
        print(f"[furiosa] after cluster_articles_by_event: {len(deduped)} (dropped {len(in_weekly_window) - len(deduped)})")
        deduped_urls = {a["url"] for a in deduped}
        for a in in_weekly_window:
            if a["url"] not in deduped_urls:
                print(f"  [DROPPED by cluster] {a['title'][:80]}")
        all_furiosa = [a for a in all_furiosa if a["url"] in deduped_urls or not in_window(a, weekly_cutoff)]

    in_weekly_window_2 = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    kept = dedup_by_semantic_similarity(in_weekly_window_2, embedding_client)
    print(f"[furiosa] after dedup_by_semantic_similarity: {len(kept)} (dropped {len(in_weekly_window_2) - len(kept)})")
    kept_url_set = {a["url"] for a in kept}
    for a in in_weekly_window_2:
        if a["url"] not in kept_url_set:
            print(f"  [DROPPED by dedup] {a['title'][:80]}")
    kept_urls = {a["url"] for a in kept}
    all_furiosa = [a for a in all_furiosa if a["url"] in kept_urls or not in_window(a, weekly_cutoff)]

    # 일간 5칸: 7~10점 기사를 날짜순으로 먼저 채우고, 5개에 못 미치면 4~6점으로 보충.
    daily_pool = [a for a in all_furiosa if in_window(a, daily_cutoff)]
    furiosa_daily_raw = sorted(tiered_pick(daily_pool, 5), key=lambda x: x["pub_dt"], reverse=True)
    daily_picked_urls = {a["url"] for a in furiosa_daily_raw}
    furiosa_weekly_raw = sorted(
        [a for a in all_furiosa
         if in_window(a, weekly_cutoff) and not in_window(a, daily_cutoff)
         and a["url"] not in daily_picked_urls],
        key=lambda x: x["pub_dt"], reverse=True)
    print(f"[furiosa] FINAL daily={len(furiosa_daily_raw)} (7~10 우선 → 4~6 보충), weekly={len(furiosa_weekly_raw)}")

    furiosa_articles = furiosa_daily_raw + furiosa_weekly_raw
    resolve_article_urls(furiosa_articles)  # google-news redirects → real article URLs
    furiosa_summaries = generate_furiosa_summaries(furiosa_articles, exaone_client)
    for a in furiosa_articles:
        if a["url"] in furiosa_summaries:
            a["summary"] = furiosa_summaries[a["url"]]

    furiosa_daily_group = {}
    for a in furiosa_daily_raw:
        date_key = format_date_with_weekday(a.get("pub_dt"), kst)
        if date_key not in furiosa_daily_group:
            furiosa_daily_group[date_key] = []
        furiosa_daily_group[date_key].append(to_output(a, kst, include_summary=True))

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
