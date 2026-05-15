#!/usr/bin/env python3
"""
Daily competitive intelligence report updater.
Uses free RSS feeds for news + GitHub Models (GPT-4o-mini) for analysis.
No external API key required — GITHUB_TOKEN is auto-provided in GitHub Actions.
"""
import datetime
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

import pytz
from openai import OpenAI

REPO_ROOT   = Path(__file__).parent.parent
REPORT_PATH = REPO_ROOT / "report.json"

COMPANIES = [
    {"name": "NVIDIA",      "website": "https://www.nvidia.com",    "blog": "https://blogs.nvidia.com"},
    {"name": "Groq",        "website": "https://groq.com",           "blog": "https://groq.com/blog"},
    {"name": "Cerebras",    "website": "https://cerebras.net",       "blog": "https://cerebras.net/blog"},
    {"name": "SambaNova",   "website": "https://sambanova.ai",       "blog": "https://sambanova.ai/blog"},
    {"name": "Tenstorrent", "website": "https://tenstorrent.com",    "blog": "https://tenstorrent.com/blog"},
]

# Public RSS feeds covering AI chip / infrastructure news
RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "https://venturebeat.com/feed/",
    "https://siliconangle.com/feed/",
    "https://www.theregister.com/headlines.rss",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    # Company blogs
    "https://blogs.nvidia.com/feed/",
    "https://cerebras.net/blog/feed/",
    "https://sambanova.ai/blog/feed/",
    "https://tenstorrent.com/blog/feed/",
    "https://groq.com/blog/feed/",
]

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc":   "http://purl.org/dc/elements/1.1/",
}


def fetch_feed(url: str, timeout: int = 10) -> list[dict]:
    """Fetch and parse an RSS/Atom feed, return list of entry dicts."""
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FuriosaReport/1.0)"})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except (URLError, ET.ParseError, Exception) as e:
        print(f"  [skip] {url}: {e}")
        return []

    entries = []
    # RSS 2.0
    for item in root.findall(".//item"):
        title   = (item.findtext("title") or "").strip()
        link    = (item.findtext("link")  or "").strip()
        desc    = (item.findtext("description") or "").strip()
        pubdate = item.findtext("pubDate") or item.findtext("dc:date", namespaces=NS) or ""
        entries.append({"title": title, "url": link, "summary": desc[:400], "pubdate": pubdate})

    # Atom
    for entry in root.findall("atom:entry", NS):
        title = (entry.findtext("atom:title", namespaces=NS) or "").strip()
        link_el = entry.find("atom:link[@rel='alternate']", NS) or entry.find("atom:link", NS)
        link  = (link_el.get("href") if link_el is not None else "")
        summary = (entry.findtext("atom:summary", namespaces=NS) or
                   entry.findtext("atom:content",  namespaces=NS) or "")[:400]
        pubdate = entry.findtext("atom:updated", namespaces=NS) or ""
        entries.append({"title": title, "url": link, "summary": summary, "pubdate": pubdate})

    return entries


def collect_articles(hours: int = 30) -> tuple[list[dict], list[dict]]:
    """Collect recent articles for competitors and Furiosa separately."""
    company_names = [c["name"].lower() for c in COMPANIES]
    furiosa_terms = {"furiosa", "furiosaai", "furiosa ai"}

    competitor_found: list[dict] = []
    furiosa_found:    list[dict] = []

    print("Fetching RSS feeds...")
    for url in RSS_FEEDS:
        for entry in fetch_feed(url):
            text = (entry["title"] + " " + entry["summary"]).lower()
            clean_summary = re.sub(r"<[^>]+>", "", entry["summary"])[:300]

            # Check Furiosa first
            if any(t in text for t in furiosa_terms):
                furiosa_found.append({
                    "title":   entry["title"],
                    "url":     entry["url"],
                    "summary": clean_summary,
                })
                continue

            # Then competitors
            for name in company_names:
                if name in text:
                    competitor_found.append({
                        "company": name.title() if name != "sambanova" else "SambaNova",
                        "title":   entry["title"],
                        "url":     entry["url"],
                        "summary": clean_summary,
                    })
                    break

    def dedup(items):
        seen: set[str] = set()
        result = []
        for a in items:
            if a["url"] not in seen:
                seen.add(a["url"])
                result.append(a)
        return result

    competitor_found = dedup(competitor_found)
    furiosa_found    = dedup(furiosa_found)
    print(f"  Competitor articles: {len(competitor_found)}, Furiosa articles: {len(furiosa_found)}")
    return competitor_found, furiosa_found


def build_period(now: datetime.datetime) -> str:
    today     = now.date()
    week_start = today - datetime.timedelta(days=today.weekday())
    days_ko   = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} "
            f"~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}")


def analyze(articles: list[dict], furiosa_articles: list[dict], now: datetime.datetime) -> dict:
    token  = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN is not set")

    client = OpenAI(
        base_url="https://models.inference.ai.azure.com",
        api_key=token,
    )

    articles_text = "\n\n".join(
        f"[{a['company']}] {a['title']}\nURL: {a['url']}\n{a['summary']}"
        for a in articles
    ) or "수집된 기사가 없습니다."

    furiosa_text = "\n\n".join(
        f"[Furiosa] {a['title']}\nURL: {a['url']}\n{a['summary']}"
        for a in furiosa_articles
    ) or "수집된 기사가 없습니다."

    companies_template = json.dumps(
        [{"name": c["name"], "website": c["website"], "blog": c["blog"],
          "no_update": True,
          "items": [{"text": "N. 제목 (MM-DD) — 핵심내용 (Korean)", "url": "기사URL"}],
          "watch": ""}
         for c in COMPANIES],
        ensure_ascii=False, indent=2
    )

    prompt = f"""Today is {now.strftime('%Y-%m-%d %H:%M KST')}.
You are a competitive intelligence analyst for Furiosa AI (Korean AI chip startup).
Analyze the articles below and return a JSON report.

=== COMPETITOR ARTICLES ===
{articles_text}
=== END ===

=== FURIOSA AI ARTICLES ===
{furiosa_text}
=== END ===

Return ONLY valid JSON — no markdown fences, no explanation:

{{
  "period": "{build_period(now)}",
  "updated_at": "{now.isoformat()}",
  "furiosa_highlights": [
    {{"text": "Furiosa AI 관련 뉴스 팩트 요약 (Korean)", "url": "기사URL또는빈문자열"}}
  ],
  "highlights": [
    {{"company": "회사명", "text": "뉴스 팩트 요약 한 줄 (Korean)", "url": "기사URL또는빈문자열"}}
  ],
  "companies": {companies_template}
}}

Rules:
- furiosa_highlights: 1–3 items from FURIOSA AI ARTICLES — factual only, no "Furiosa는..." advice. If no Furiosa articles found, return []
- highlights: 2–3 most impactful competitor news — factual only, no recommendations
- For each company with news: set no_update=false, fill items (max 3)
- items MUST be objects with "text" and "url" — use the exact article URL from ARTICLES above
- items text format: "N. 제목 (MM-DD) — 핵심내용" in Korean
- watch: specific market intelligence for Furiosa BD — what deals/customers/markets are shifting and why it matters. No generic advice.
- Keep website/blog URLs exactly as in the template above
- ALWAYS fill items with the most recent articles available, even if they are older than 24h. Do NOT set no_update: true just because there is no news today. Only set no_update: true if there are truly zero articles about that company across all provided data.
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000,
        temperature=0.3,
    )
    raw = resp.choices[0].message.content

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"No JSON in response:\n{raw[:600]}")
    return json.loads(match.group())


def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    articles, furiosa_articles = collect_articles(hours=30)
    report = analyze(articles, furiosa_articles, now)

    # Always keep correct website/blog metadata
    meta = {c["name"]: c for c in COMPANIES}
    for co in report.get("companies", []):
        if co["name"] in meta:
            co["website"] = meta[co["name"]]["website"]
            co["blog"]    = meta[co["name"]]["blog"]

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    updated = sum(1 for c in report.get("companies", []) if not c.get("no_update"))
    print(f"Done. {updated} companies with news, {len(report.get('highlights', []))} highlights.")


if __name__ == "__main__":
    main()
