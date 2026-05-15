#!/usr/bin/env python3
"""
Daily competitive intelligence report updater.
Uses Google News RSS per company + GitHub Models (GPT-4o-mini).
No external API key required — GITHUB_TOKEN is auto-provided in GitHub Actions.
"""
import datetime
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen, Request

import pytz
from openai import OpenAI

REPO_ROOT   = Path(__file__).parent.parent
REPORT_PATH = REPO_ROOT / "report.json"

GLOBAL_COMPANIES = [
    {"name": "NVIDIA",      "region": "global", "website": "https://www.nvidia.com",    "blog": "https://blogs.nvidia.com",    "query": "NVIDIA AI chip GPU inference", "lang": "en"},
    {"name": "Groq",        "region": "global", "website": "https://groq.com",           "blog": "https://groq.com/blog",       "query": "Groq AI inference chip LPU",   "lang": "en"},
    {"name": "Cerebras",    "region": "global", "website": "https://cerebras.net",       "blog": "https://cerebras.net/blog",   "query": "Cerebras AI chip wafer",       "lang": "en"},
    {"name": "SambaNova",   "region": "global", "website": "https://sambanova.ai",       "blog": "https://sambanova.ai/blog",   "query": "SambaNova AI chip RDU",        "lang": "en"},
    {"name": "Tenstorrent", "region": "global", "website": "https://tenstorrent.com",    "blog": "https://tenstorrent.com/blog","query": "Tenstorrent AI chip RISC-V",   "lang": "en"},
]

KOREA_COMPANIES = [
    {"name": "리벨리온",   "region": "korea", "website": "https://rebellions.ai",   "blog": "https://rebellions.ai/blog",   "query": "리벨리온 AI 반도체 NPU",      "lang": "ko"},
    {"name": "딥엑스",     "region": "korea", "website": "https://deepx.ai",         "blog": "https://deepx.ai/blog",        "query": "딥엑스 DeepX AI NPU 반도체",  "lang": "ko"},
    {"name": "하이퍼엑셀", "region": "korea", "website": "https://hyperaccel.ai",    "blog": "https://hyperaccel.ai/blog",   "query": "하이퍼엑셀 HyperAccel AI",    "lang": "ko"},
    {"name": "모빌린트",   "region": "korea", "website": "https://mobilint.com",     "blog": "https://mobilint.com/blog",    "query": "모빌린트 Mobilint AI NPU",    "lang": "ko"},
]

ALL_COMPANIES = GLOBAL_COMPANIES + KOREA_COMPANIES

FURIOSA_QUERIES = [
    ('FuriosaAI OR "Furiosa AI" chip', "en"),
    ('퓨리오사 OR 퓨리오사AI AI 반도체', "ko"),
]

NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}


def gnews_url(query: str, lang: str = "en") -> str:
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


def fetch_feed(url: str, timeout: int = 10) -> list[dict]:
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FuriosaReport/1.0)"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"  [skip] {url}: {e}")
        return []

    entries = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        desc  = (item.findtext("description") or "").strip()
        entries.append({"title": title, "url": link, "summary": desc[:300]})

    for entry in root.findall("atom:entry", NS):
        title   = (entry.findtext("atom:title", namespaces=NS) or "").strip()
        link_el = entry.find("atom:link[@rel='alternate']", NS) or entry.find("atom:link", NS)
        link    = link_el.get("href") if link_el is not None else ""
        summary = (entry.findtext("atom:summary", namespaces=NS) or
                   entry.findtext("atom:content",  namespaces=NS) or "")[:300]
        entries.append({"title": title, "url": link, "summary": summary})

    return entries


def collect_articles() -> tuple[list[dict], list[dict]]:
    competitor_found: list[dict] = []
    furiosa_found:    list[dict] = []

    print("Fetching Google News RSS per company...")
    for company in ALL_COMPANIES:
        entries = fetch_feed(gnews_url(company["query"], company["lang"]))[:5]
        for entry in entries:
            competitor_found.append({
                "company": company["name"],
                "region":  company["region"],
                "title":   entry["title"],
                "url":     entry["url"],
                "summary": re.sub(r"<[^>]+>", "", entry["summary"])[:300],
            })
        print(f"  {company['name']}: {len(entries)} articles")

    print("Fetching Google News RSS for Furiosa AI...")
    seen_urls: set[str] = set()
    for query, lang in FURIOSA_QUERIES:
        for entry in fetch_feed(gnews_url(query, lang))[:5]:
            if entry["url"] not in seen_urls:
                seen_urls.add(entry["url"])
                furiosa_found.append({
                    "title":   entry["title"],
                    "url":     entry["url"],
                    "summary": re.sub(r"<[^>]+>", "", entry["summary"])[:300],
                })
    print(f"  Furiosa: {len(furiosa_found)} articles")

    return competitor_found, furiosa_found


def build_period(now: datetime.datetime) -> str:
    today      = now.date()
    week_start = today - datetime.timedelta(days=today.weekday())
    days_ko    = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} "
            f"~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}")


def ai_call(client, prompt: str, max_tokens: int = 1500) -> dict:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    raw = resp.choices[0].message.content
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"No JSON:\n{raw[:400]}")
    return json.loads(match.group())


def analyze(articles: list[dict], furiosa_articles: list[dict], now: datetime.datetime) -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN is not set")

    client = OpenAI(base_url="https://models.inference.ai.azure.com", api_key=token)

    # ── Step 1: highlights only (small call) ──────────────────────────────
    global_titles = "\n".join(
        f"[{a['company']}] {a['title']} | {a['url']}"
        for a in articles if a["region"] == "global"
    )
    furiosa_titles = "\n".join(
        f"{a['title']} | {a['url']}" for a in furiosa_articles
    )

    hl_prompt = f"""Date: {now.strftime('%Y-%m-%d KST')}. Competitive intelligence analyst for Furiosa AI.

GLOBAL COMPETITOR ARTICLES:
{global_titles}

FURIOSA ARTICLES:
{furiosa_titles}

Return ONLY valid JSON (no markdown):
{{"furiosa_highlights":[{{"text":"팩트 한줄(Korean)","url":""}}],"highlights":[{{"company":"name","text":"팩트 한줄(Korean)","url":""}}]}}

Rules:
- furiosa_highlights: 1-3 items. Furiosa 관련 뉴스 팩트만. 추천/의견 금지.
- highlights: 2-3 items. 글로벌 경쟁사 뉴스 팩트만 (Korean). Furiosa 언급 절대 금지. "Furiosa는" 시작 금지."""

    print("  Getting highlights...")
    hl = ai_call(client, hl_prompt, max_tokens=800)
    time.sleep(2)

    # ── Step 2: per-company items (one call per company) ──────────────────
    companies_out = []
    for meta in ALL_COMPANIES:
        co_articles = [a for a in articles if a["company"] == meta["name"]]
        if not co_articles:
            companies_out.append({**meta, "no_update": True, "items": []})
            continue

        art_text = "\n\n".join(
            f"기사{i+1}: {a['title']}\nURL: {a['url']}\n{a['summary']}"
            for i, a in enumerate(co_articles)
        )

        co_prompt = f"""Date: {now.strftime('%Y-%m-%d KST')}. Competitive intelligence analyst for Furiosa AI.

ARTICLES for {meta['name']}:
{art_text}

Return ONLY valid JSON (no markdown):
{{"items":[{{"text":"N. 제목 (MM-DD) — 한줄요약","url":"기사URL","summary":"기사 내용 2-3문장 요약 (Korean)","bd_watch":"Furiosa BD 시사점 1문장 — 구체적 딜/고객/시장변화 언급, 절대 Furiosa는~ 시작 금지"}}]}}

Rules:
- items: max 3. Use actual articles above only.
- text: "N. 제목 (MM-DD) — 한줄요약" 형식
- summary: 2-3문장 팩트 요약 (Korean)
- bd_watch: 구체적 BD 시사점 1문장. Generic advice 금지."""

        print(f"  Getting items for {meta['name']}...")
        try:
            co_result = ai_call(client, co_prompt, max_tokens=800)
            items = co_result.get("items", [])
        except Exception as e:
            print(f"  [warn] {meta['name']}: {e}")
            items = []

        companies_out.append({
            "name":      meta["name"],
            "region":    meta["region"],
            "website":   meta["website"],
            "blog":      meta["blog"],
            "no_update": len(items) == 0,
            "items":     items,
        })
        time.sleep(2)

    return {
        "period":             build_period(now),
        "updated_at":         now.isoformat(),
        "furiosa_highlights": hl.get("furiosa_highlights", []),
        "highlights":         hl.get("highlights", []),
        "companies":          companies_out,
    }


def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    articles, furiosa_articles = collect_articles()
    report = analyze(articles, furiosa_articles, now)

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    updated = sum(1 for c in report.get("companies", []) if not c.get("no_update"))
    print(f"Done. {updated}/{len(ALL_COMPANIES)} companies with news.")


if __name__ == "__main__":
    main()
