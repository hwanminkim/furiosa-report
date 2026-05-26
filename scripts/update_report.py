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

FURIOSA_ALIASES = ["Furiosa", "퓨리오사", "FuriosaAI", "furiosaai"]

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

def _extract_keywords(title: str) -> set:
    if not title:
        return set()
    words = re.findall(r"[가-힣]{2,}|[a-zA-Z]{3,}", title)
    stopwords = {
        "있다", "한다", "위한", "위해", "통해", "통한", "관련", "대한", "대해",
        "지난", "오는", "올해", "내년", "최근", "이번", "그리고", "또는", "하는",
        "the", "and", "for", "with", "from", "into", "this", "that", "have",
    }
    return {w.lower() for w in words if w.lower() not in stopwords}


def dedup_by_keyword_overlap(articles: list[dict], min_overlap: int = 3) -> list[dict]:
    if len(articles) < 2:
        return articles
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

def filter_relevant_by_company(company: str, aliases: list[str], articles: list[dict], client: "OpenAI | None") -> list[dict]:
    if not articles: return articles
    # Hard rule 1: 제목에 회사 alias가 없으면 즉시 제외
    # Hard rule 2: 영문 주식거래·시황 패턴이면 즉시 제외
    title_matched = [a for a in articles
                     if title_contains_alias(a.get("title", ""), aliases)
                     and not is_stock_noise(a.get("title", ""))]
    if not title_matched:
        return []
    if client is None:
        return title_matched
    numbered_items = []
    for i, a in enumerate(title_matched):
        desc = (a.get("description") or "").replace("\n", " ").strip()[:200]
        numbered_items.append(f"[{i}] 제목: {a.get('title', '')}\n    요약: {desc}")
    numbered = "\n".join(numbered_items)
    prompt = f"""다음은 '{company}' 관련 뉴스 검색 결과입니다. 각 기사에 대해 '{company}'의 관련도를 1~3점으로 평가하세요.

## 점수 기준

**3점 — 회사가 기사의 주체 (sole actor)**
'{company}'가 직접 한 액션이 기사의 핵심. 제목이 회사의 행동을 묘사.
예: "{company}, 신규 계약 체결" / "{company} 신제품 발표" / "{company} 투자 유치" / "{company} 대표 인터뷰"

**2점 — 양측이 함께 능동적 액션을 한 공동 주체 (co-actor)**
'{company}'와 다른 회사/기관이 **함께 액션을 수행**한 경우만 해당. 회사가 능동적 당사자여야 함.
예: "삼성-{company} 협력 발표" / "{company}, A사와 MOU 체결" / "A사·B사·{company} 컨소시엄 출범"

**1점 — 단순 언급, 수동적 객체, 사이드 등장 (제외 대상)**
다음 케이스는 **무조건 1점**:
- 펀드/투자 기사에서 '{company}'가 *투자 대상* 중 하나로만 언급 (예: "X펀드, A사·B사·{company}에 투자")
- 트렌드/시장 분석/산업 동향 기사에서 여러 회사 중 하나로 나열 (예: "K-팹리스 기업들이 시장에 진출... A사·B사·{company} 등")
- 다른 회사가 주체인 기사에 '{company}'가 곁다리로 언급 (예: 노타/딥엑스 기사 끝부분에 "{company} 등도" 식)
- 시황·증시·주가 기사
- 행사·포럼·전시회 알림에 '{company}' 임원이 연사 중 한 명으로 등장
- '{company}' 제품/칩이 다른 솔루션의 부품으로 한 줄 언급
- 정부 정책/펀드 기사에서 수혜자/대상자로 언급

## 핵심 원칙
- 회사가 **능동적 주체(actor)**여야 2점 이상. **수동적 객체(object)**거나 *언급되는 대상*이면 1점.
- 기사 제목이 다른 회사·주체의 행동을 묘사하면 거의 항상 1점.
- 트렌드 기사에서 다른 여러 회사와 함께 나열되면 1점.
- 애매하면 1점.

기사 목록:
{numbered}

응답 형식 (오직 JSON, 모든 기사에 대해 점수 부여):
{{"scores": [{{"id": 0, "score": 3}}, {{"id": 1, "score": 1}}, ...]}}"""
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
        scores = data.get("scores", [])
        kept_indices = []
        for entry in scores:
            idx = entry.get("id")
            score = entry.get("score")
            if isinstance(idx, int) and 0 <= idx < len(title_matched) and isinstance(score, int) and score >= 2:
                kept_indices.append(idx)
        kept_indices.sort()
        return [title_matched[i] for i in kept_indices]
    except Exception:
        return title_matched

def filter_furiosa_subject(articles: list[dict], client: "OpenAI | None") -> list[dict]:
    """
    Furiosa AI 기사 필터. 제목 alias 하드 룰 + 3단계 점수제, 2점 이상만 keep.
    """
    if not articles: return articles
    title_matched = [a for a in articles
                     if title_contains_alias(a.get("title", ""), FURIOSA_ALIASES)
                     and not is_stock_noise(a.get("title", ""))]
    if not title_matched:
        return []
    if client is None:
        return title_matched
    numbered_items = []
    for i, a in enumerate(title_matched):
        desc = (a.get("description") or "").replace("\n", " ").strip()[:200]
        numbered_items.append(f"[{i}] 제목: {a.get('title', '')}\n    요약: {desc}")
    numbered = "\n".join(numbered_items)
    prompt = f"""다음은 'Furiosa AI(퓨리오사AI)' 키워드로 검색된 뉴스 기사 목록입니다. 각 기사에 대해 Furiosa AI의 관련도를 1~3점으로 평가하세요.

## 점수 기준

**3점 — Furiosa AI가 기사의 주체 (sole actor)**
Furiosa AI가 직접 한 액션이 기사의 핵심. 제목이 Furiosa AI의 행동을 묘사.
예: "퓨리오사AI, 신규 계약 체결" / "퓨리오사AI 레니게이드 발표" / "퓨리오사AI 투자 유치" / "백준호 대표 인터뷰"

**2점 — 양측이 함께 능동적 액션을 한 공동 주체 (co-actor)**
Furiosa AI와 다른 회사/기관이 **함께 발표·연동·도입·채택**한 경우. 본격적인 파트너십·연동·공동 개발·도입 기사가 여기 해당.
예: "삼성-퓨리오사AI 협력 발표" / "퓨리오사AI, A사와 MOU" / "A사 플랫폼에 퓨리오사AI NPU 연동" / "A사·B사·퓨리오사AI 컨소시엄" / "A사, 퓨리오사 NPU 채택"
**핵심: 파트너십/연동/도입/채택 발표 기사는 2점.** (양사가 공동 발표하거나, 한쪽이 도입·연동한 사실이 기사 본문의 핵심이면 OK)
구분: 다른 회사 기사 끝에 곁다리로 "퓨리오사 등도 사용" 식이면 1점.

**1점 — 단순 언급, 수동적 객체, 사이드 등장 (제외 대상)**
다음 케이스는 **무조건 1점**:
- 펀드/투자 기사에서 퓨리오사AI가 *투자 대상* 중 하나로만 언급 (예: "X펀드, A사·B사·퓨리오사AI에 투자")
- 트렌드/시장 분석/산업 동향 기사에서 여러 회사 중 하나로 나열 (예: "K-팹리스 기업들... 리벨리온·퓨리오사·딥엑스 등")
- 다른 회사가 주체인 기사에 퓨리오사가 곁다리로 언급 (예: 노타/딥엑스 기사 끝부분에 "퓨리오사AI 등도" 식)
- 시황·증시·주가 기사
- 행사·포럼·전시회 알림에 백준호 대표가 연사 중 한 명으로 등장 (단순 알림)
- 정부 정책/펀드 기사에서 수혜자/대상자로 언급

## 핵심 원칙
- Furiosa AI가 **능동적 주체(actor)**여야 2점 이상. **수동적 객체(object)**거나 *언급되는 대상*이면 1점.
- 기사 제목이 다른 회사·주체의 행동을 묘사하면 거의 항상 1점.
- 트렌드 기사에서 다른 여러 회사와 함께 나열되면 1점.
- 애매하면 1점.

기사 목록:
{numbered}

응답 형식 (오직 JSON, 모든 기사에 대해 점수 부여):
{{"scores": [{{"id": 0, "score": 3}}, {{"id": 1, "score": 1}}, ...]}}"""
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
        scores = data.get("scores", [])
        kept_indices = []
        for entry in scores:
            idx = entry.get("id")
            score = entry.get("score")
            if isinstance(idx, int) and 0 <= idx < len(title_matched) and isinstance(score, int) and score >= 2:
                kept_indices.append(idx)
        kept_indices.sort()
        return [title_matched[i] for i in kept_indices]
    except Exception:
        return title_matched


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
    items_in = []
    for i, (company, a) in enumerate(articles_with_company):
        items_in.append({
            "id": i, "company": company, "title": a.get("title", ""), "snippet": (a.get("description") or "")[:500]
        })
    prompt = f"""다음은 AI 반도체 경쟁사 뉴스 기사 목록입니다.

각 기사에 대해 한국어 요약을 만들어 주세요:
- "summary": 3문장, 사실 위주, 250~350자.

## 절대 금지 표현
- "~분야의 경쟁력을 높이는 데 중요한 역할을 할 것이다"
- "~기술의 발전에 기여할 것으로 기대된다"
- "기대된다", "전망된다" 같은 막연한 미래 추측
- "본문 정보 부족", "원문 확인 필요", "추가 정보 필요" 등 정보 부족을 명시하는 문구

## 작성 원칙
1. 제목과 snippet에 있는 사실만 활용.
2. 정보가 적더라도 제목과 snippet에서 추출 가능한 사실만으로 자연스러운 3문장 요약을 작성. 정보 부족을 언급하지 말 것.

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
        items_in.append({"id": i, "title": a.get("title", ""), "snippet": (a.get("description") or "")[:500]})
    prompt = f"""다음은 Furiosa AI 관련 뉴스 기사 목록입니다.

각 기사에 대해 한국어 요약을 만들어 주세요:
- "summary": 3문장, 사실 위주.

진부한 일반론 금지.

## 절대 금지 표현
- "본문 정보 부족", "원문 확인 필요", "추가 정보 필요" 등 정보 부족을 명시하는 문구
- "기대된다", "전망된다" 같은 막연한 미래 추측

정보가 적더라도 제목과 snippet에서 추출 가능한 사실만으로 자연스러운 3문장 요약을 작성. 정보 부족을 언급하지 말 것.

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
    
    daily_start_kst = kst.localize(datetime.datetime.combine(
        now.date(), datetime.time(0, 0)
    ))
    daily_cutoff = daily_start_kst.astimezone(pytz.utc)
    weekly_start_kst = kst.localize(datetime.datetime.combine(
        now.date() - datetime.timedelta(days=6), datetime.time(0, 0)
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
        relevant = filter_relevant_by_company(co["name"], co["aliases"], recent, client)
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
    # 주간 윈도우 안의 모든 제목 출력 (어떤 기사가 들어왔는지)
    for a in in_weekly_window_raw:
        pub_str = a['pub_dt'].astimezone(kst).strftime('%m-%d %H:%M') if a.get('pub_dt') else 'no_date'
        print(f"  [in_window] {pub_str} | {a['title'][:80]}")

    if client and in_weekly_window_raw:
        kept_subject = filter_furiosa_subject(in_weekly_window_raw, client)
        print(f"[furiosa] after filter_furiosa_subject: {len(kept_subject)} (dropped {len(in_weekly_window_raw) - len(kept_subject)})")
        # 제외된 기사 출력
        kept_urls_subj = {a["url"] for a in kept_subject}
        for a in in_weekly_window_raw:
            if a["url"] not in kept_urls_subj:
                print(f"  [DROPPED by filter] {a['title'][:80]}")
        kept_subject_urls = {a["url"] for a in kept_subject}
        all_furiosa = [a for a in all_furiosa if a["url"] in kept_subject_urls or not in_window(a, weekly_cutoff)]

    in_weekly_window = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    if client:
        deduped = cluster_articles_by_event(in_weekly_window, client)
        print(f"[furiosa] after cluster_articles_by_event: {len(deduped)} (dropped {len(in_weekly_window) - len(deduped)})")
        deduped_urls = {a["url"] for a in deduped}
        # 제외된 기사 출력
        for a in in_weekly_window:
            if a["url"] not in deduped_urls:
                print(f"  [DROPPED by cluster] {a['title'][:80]}")
        all_furiosa = [a for a in all_furiosa if a["url"] in deduped_urls or not in_window(a, weekly_cutoff)]

    in_weekly_window_2 = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    kept = dedup_by_keyword_overlap(in_weekly_window_2, min_overlap=3)
    print(f"[furiosa] after dedup_by_keyword_overlap: {len(kept)} (dropped {len(in_weekly_window_2) - len(kept)})")
    # 제외된 기사 출력
    kept_url_set = {a["url"] for a in kept}
    for a in in_weekly_window_2:
        if a["url"] not in kept_url_set:
            print(f"  [DROPPED by dedup] {a['title'][:80]}")
    kept_urls = {a["url"] for a in kept}
    all_furiosa = [a for a in all_furiosa if a["url"] in kept_urls or not in_window(a, weekly_cutoff)]

    furiosa_daily_raw = sorted([a for a in all_furiosa if in_window(a, daily_cutoff)], key=lambda x: x["pub_dt"], reverse=True)[:5]
    furiosa_weekly_raw = sorted([a for a in all_furiosa if in_window(a, weekly_cutoff) and not in_window(a, daily_cutoff)], key=lambda x: x["pub_dt"], reverse=True)
    print(f"[furiosa] FINAL daily={len(furiosa_daily_raw)}, weekly={len(furiosa_weekly_raw)}")

    furiosa_articles = furiosa_daily_raw + furiosa_weekly_raw
    furiosa_summaries = generate_furiosa_summaries(furiosa_articles, client)
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
