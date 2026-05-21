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
    """
    cid = os.environ.get("NAVER_CLIENT_ID")
    csec = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not csec:
        return []

    naver_q = query.replace(" OR ", " | ")
    
    # [수정됨] sort=date 대신 sort=sim(정확도순)으로 변경하여 양질의 기사가 먼저 오게 함
    # 최소 display 개수도 조금 늘려 더 넓은 범위에서 유의미한 기사를 탐색
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
            rep = min(valid)  # preserve order
            if rep not in used:
                kept_indices.append(rep)
            for i in valid:
                used.add(i)
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
    회사명이 포함된 유의미한 기사를 필터링합니다. 
    제목뿐만 아니라 description(스니펫)을 함께 LLM에 제공하여 정확도를 높입니다.
    """
    if client is None or not articles:
        return articles

    # [수정됨] 제목과 요약(description)을 함께 묶어서 프롬프트 생성
    numbered_items = []
    for i, a in enumerate(articles):
        desc = (a.get("description") or "").replace("\n", " ").strip()[:150]
        numbered_items.append(f"[{i}] 제목: {a.get('title', '')}\n    요약: {desc}")
    numbered = "\n".join(numbered_items)

    # [수정됨] 제외 기준을 완화하여 업계 트렌드 종합 분석 기사도 포함하도록 지시
    prompt = f"""다음은 '{company}' 관련 뉴스 검색 결과입니다.
각 기사가 '{company}'의 사업 개발(BD) 및 시장 동향 파악에 유용한 정보인지 판단하세요.

기사 목록:
{numbered}

## Keep 기준 (다음 중 하나라도 해당하면 반드시 포함)
1. '{company}'가 주도적으로 무언가를 한 기사 (제품 발표, 투자 유치, 계약, 인사 등)
2. 업계 트렌드, 시장 분석, 다수 기업을 다루는 기획 기사 중 '{company}'가 의미 있게 언급되거나 비교군으로 등장하는 경우 (BD 관점에서 경쟁 구도 파악에 매우 중요함)

## 제외 기준 (여기에만 해당하면 제외)
- 단순 주식 시황, 증시 마감 (주가 등락만 기계적으로 나열)
- 기업의 단순 채용 공고나 기계적 공시
- 이름만 스치듯 지나가고 AI 반도체 시장 동향과 전혀 무관한 내용

응답 형식 (오직 JSON):
{{"keep": [0, 2, 5]}}

규칙:
- BD 분석 관점에서 조금이라도 가치가 있다면 과감하게 Keep 하세요. (너무 깐깐하게 제외하지 말 것)
- 확신이 서지 않으면 Keep에 포함시키세요.
- 해당 기사가 아예 없으면 {{"keep": []}} 반환."""

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
        filtered = [articles[i] for i in valid]
        print(f"  Relevance filter for {company}: {len(articles)} -> {len(filtered)}")
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

    prompt = f"""You are a BD (business development) analyst at Furiosa AI.

## Furiosa AI 핵심 컨텍스트
- 한국 AI 추론(inference) 전용 칩 스타트업
- 주력 제품: RNGD (현재 주력 NPU) — 데이터센터 LLM 추론용
- 강점: 전력 효율 (W당 성능), 추론 전용 최적화, MLPerf 벤치마크 실적
- 타겟 시장: 데이터센터 LLM 추론 서비스, 엔터프라이즈 AI, sovereign AI 인프라
- 경쟁 포지셔닝:
  - 글로벌: NVIDIA H100/H200, RTX Pro 6000 같은 GPU 라인업이 직접 경쟁
  - 추론 특화 그룹: Tenstorrent Wormhole/Blackhole, Groq, Cerebras, SambaNova
  - 국내: Rebellions가 가장 유사한 포지셔닝
- 사업 단계: 대규모 투자 라운드 진행 중 (후기 단계 스타트업)

## 경쟁사 그룹
- 글로벌: NVIDIA (시장 지배), Tenstorrent (RISC-V 기반, Wormhole/Blackhole), Groq/Cerebras/SambaNova (추론 특화)
- 한국: Rebellions (유사 포지셔닝), DeepX/Mobilint (엣지 NPU), HyperAccel (LPU)

## 작업
각 경쟁사 뉴스에 대해 두 한국어 필드 생성:

- **"summary"**: 3문장, 사실 위주, 250~350자. 무엇/언제/어떻게/왜 중요한지 포함.

- **"bd_perspective"**: 1~2문장, 100~200자. Furiosa BD 입장에서 **구체적인** 함의.

  ## 작성 원칙 (중요)

  **원칙 1: 본문 정보가 약하면 무리하지 말 것**
  제목만 보고 본문 디테일이 부족한 경우(예: "패널 토론에 참석", "동향 논의" 같은 행사/세미나 기사),
  반드시 다음 중 하나로 답하세요:
  - "원문 확인 필요 — [구체적으로 뭐가 궁금한지: 예. 어떤 고객사 언급되었는지, 어떤 제품 비교가 있었는지]"
  - "BD 액션 사항 없음 — 일반 행사 보도"
  - "분석 가치 낮음 — [이유]"

  **원칙 2: 추측해서 답을 만들지 말 것**
  본문 근거 없이 "협력 기회 모색", "네트워킹 강화", "자료 업데이트" 같은 액션을 만들어내는 건 금지.
  실제 기사 안에 명확한 사실(가격, 수치, 고객사명, 일정, 정책 등)이 있을 때만 그것에 근거한 함의를 적기.

  **원칙 3: 문장 형식 반복 금지**
  여러 기사 답변 중 다음 같은 패턴 반복하지 말 것:
  - "Furiosa는 ~을 인식하고, ~해야 한다" (이거 절대 쓰지 말 것)
  - "~기회를 모색해야 한다"
  - "~경쟁이 심화될 수 있다"

  같은 batch 안에서 모든 답변의 문장 시작과 끝을 다양화. 어떤 답은 데이터 인용으로,
  어떤 답은 구체적 회사명/제품명 비교로, 어떤 답은 솔직히 "확인 필요"로.

  **원칙 4: 구체성 검증**
  답을 쓰고 나서 자문: "이 문장이 NVIDIA 기사에 붙어도 말이 되고 Cerebras 기사에 붙어도 말이 되면 → 너무 일반론이라 다시 쓰기."

  **금지 표현 (절대 사용 금지)**:
  - "차별화된 기술 개발이 필요하다"
  - "주의가 필요하다", "주목해야 한다"
  - "경쟁이 치열해진다/심화된다"
  - "긍정/부정적 신호로 작용"
  - "기회가 될 수 있다"
  - "협력 기회 모색"
  - "네트워킹 강화"
  - "~을 인식하고 ~해야 한다"
  - "관련 자료 업데이트"

Input articles:
{json.dumps(items_in, ensure_ascii=False)}

Return JSON ONLY:
{{"items": [{{"id": 0, "summary": "...", "bd_perspective": "..."}}, ...]}}

Rules:
- 모든 id가 정확히 한 번씩 포함되어야 함.
- 한국어만.
- 진부한 일반론 절대 금지. 차라리 "원문 확인 필요"가 낫다."""

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
    COMPETITOR_CUTOFF_DAYS = 30
    COMPETITOR_MAX_ITEMS = 3
    competitor_cutoff = now_utc - datetime.timedelta(days=COMPETITOR_CUTOFF_DAYS)

    companies_raw = []
    for co in COMPANIES:
        fetched = []
        seen_urls: set[str] = set()
        for query, lang in co["queries"]:
            for a in fetch_articles(query, lang, n=20):
                if a["url"] not in seen_urls:
                    seen_urls.add(a["url"])
                    fetched.append(a)
        
        recent = [a for a in fetched
                  if a.get("pub_dt") is not None and a["pub_dt"] >= competitor_cutoff]
        
        relevant = filter_relevant_by_company(co["name"], recent, client)
        
        deduped = cluster_articles_by_event(relevant, client)
        
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
        # 여기서 n=30 을 n=100 으로 늘려줍니다!
        for a in fetch_articles(query, lang, n=100):
            if a["url"] in seen_urls:
                continue
            norm = normalize_title(a["title"])
            if norm and norm in seen_titles:
                continue
            seen_urls.add(a["url"])
            if norm:
                seen_titles.add(norm)
            all_furiosa.append(a)

    def in_window(a: dict, cutoff: datetime.datetime) -> bool:
        return a.get("pub_dt") is not None and a["pub_dt"] >= cutoff

    # ── 2.5 LLM 기반 의미 단위 클러스터링 (같은 사건 dedup) ───────────────
    in_weekly_window = [a for a in all_furiosa if in_window(a, weekly_cutoff)]
    if client:
        deduped = cluster_articles_by_event(in_weekly_window, client)
        deduped_urls = {a["url"] for a in deduped}
        all_furiosa = [a for a in all_furiosa if a["url"] in deduped_urls
                       or not in_window(a, weekly_cutoff)]

    def sort_key(a: dict):
        return a["pub_dt"]

    furiosa_daily = sorted(
        [a for a in all_furiosa if in_window(a, daily_cutoff)],
        key=sort_key,
        reverse=True,
    )[:DAILY_LIMIT]
    
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
