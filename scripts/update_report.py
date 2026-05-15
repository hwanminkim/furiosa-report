#!/usr/bin/env python3
"""
Daily competitive intelligence report updater.
Uses Google News RSS per company + GitHub Models (GPT-4o-mini).
No external API key required — GITHUB_TOKEN is auto-provided in GitHub Actions.
"""
import datetime
import email.utils
import json
import os
import re
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


def parse_pub_date(raw: str) -> str:
    try:
        t = email.utils.parsedate(raw)
        if t:
            return datetime.datetime(*t[:6]).strftime("%m-%d")
    except Exception:
        pass
    return ""


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
        title   = (item.findtext("title") or "").strip()
        link    = (item.findtext("link")  or "").strip()
        desc    = (item.findtext("description") or "").strip()
        pub     = parse_pub_date(item.findtext("pubDate") or "")
        entries.append({"title": title, "url": link, "summary": desc[:80], "date": pub})

    for entry in root.findall("atom:entry", NS):
        title   = (entry.findtext("atom:title", namespaces=NS) or "").strip()
        link_el = entry.find("atom:link[@rel='alternate']", NS) or entry.find("atom:link", NS)
        link    = link_el.get("href") if link_el is not None else ""
        summary = (entry.findtext("atom:summary", namespaces=NS) or
                   entry.findtext("atom:content",  namespaces=NS) or "")[:80]
        pub_raw = (entry.findtext("atom:published", namespaces=NS) or
                   entry.findtext("atom:updated",   namespaces=NS) or "")
        pub     = pub_raw[5:10].replace("-", "") if len(pub_raw) >= 10 else ""
        if pub and len(pub) == 4:
            pub = pub[:2] + "-" + pub[2:]
        entries.append({"title": title, "url": link, "summary": summary, "date": pub})

    return entries


def collect_articles() -> tuple[list[dict], list[dict]]:
    competitor_found: list[dict] = []
    furiosa_found:    list[dict] = []

    print("Fetching Google News RSS per company...")
    for company in ALL_COMPANIES:
        entries = fetch_feed(gnews_url(company["query"], company["lang"]))[:3]
        for entry in entries:
            competitor_found.append({
                "company": company["name"],
                "region":  company["region"],
                "title":   entry["title"],
                "url":     entry["url"],
                "date":    entry["date"],
                "summary": re.sub(r"<[^>]+>", "", entry["summary"])[:80],
            })
        print(f"  {company['name']}: {len(entries)} articles")

    print("Fetching Google News RSS for Furiosa AI...")
    seen_urls: set[str] = set()
    for query, lang in FURIOSA_QUERIES:
        for entry in fetch_feed(gnews_url(query, lang))[:4]:
            if entry["url"] not in seen_urls:
                seen_urls.add(entry["url"])
                furiosa_found.append({
                    "title":   entry["title"],
                    "url":     entry["url"],
                    "date":    entry["date"],
                    "summary": re.sub(r"<[^>]+>", "", entry["summary"])[:80],
                })
    print(f"  Furiosa: {len(furiosa_found)} articles")

    return competitor_found, furiosa_found


def build_period(now: datetime.datetime) -> str:
    today      = now.date()
    week_start = today - datetime.timedelta(days=today.weekday())
    days_ko    = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} "
            f"~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}")


def _ai_call(client, prompt: str, max_tokens: int = 2000) -> dict:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    raw = resp.choices[0].message.content
    # check for truncation
    if resp.choices[0].finish_reason == "length":
        print(f"  [warn] response truncated at {max_tokens} tokens")
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"No JSON in response:\n{raw[:400]}")
    return json.loads(match.group())


def _fmt_articles(articles: list[dict]) -> str:
    lines = []
    for a in articles:
        date_str = f"({a['date']})" if a.get("date") else ""
        lines.append(f"[{a['company']}]{date_str} {a['title']} | {a['url']}")
    return "\n".join(lines) or "없음"


def _company_block(companies: list[dict]) -> str:
    return json.dumps(
        [{"name": c["name"], "region": c["region"],
          "website": c["website"], "blog": c["blog"],
          "no_update": False, "items": []}
         for c in companies],
        ensure_ascii=False
    )


ITEM_RULE = 'Each item: {"text":"N. 제목(MM-DD — use actual date from article)","url":"URL","summary":"한국어 1문장 요약","bd_watch":"구체적 BD 시사점 1문장 — 특정 고객/딜/시장변화 언급, 절대 Furiosa는~으로 시작 금지"}. Max 2 items.'


def analyze(articles: list[dict], furiosa_articles: list[dict], now: datetime.datetime) -> dict:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN is not set")

    client = OpenAI(base_url="https://models.inference.ai.azure.com", api_key=token)

    global_articles = [a for a in articles if a["region"] == "global"]
    korea_articles  = [a for a in articles if a["region"] == "korea"]

    furiosa_text = "\n".join(
        f"[Furiosa]({a.get('date','')}) {a['title']} | {a['url']}"
        for a in furiosa_articles
    ) or "없음"

    # ── Call 1: highlights + global companies ──────────────────────────────
    global_text = _fmt_articles(global_articles)
    global_block = _company_block(GLOBAL_COMPANIES)

    prompt1 = f"""Date:{now.strftime('%Y-%m-%d')}. Competitive intelligence analyst for Furiosa AI.
GLOBAL ARTICLES:\n{global_text}
FURIOSA ARTICLES:\n{furiosa_text}
Return ONLY valid JSON (no markdown):
{{"period":"{build_period(now)}","updated_at":"{now.isoformat()}","furiosa_highlights":[{{"text":"팩트 한줄(Korean)","url":""}}],"highlights":[{{"company":"name","text":"팩트 한줄(Korean)","url":""}}],"companies":{global_block}}}
Rules:
- furiosa_highlights: 1-2 items. Facts only (Korean). No advice.
- highlights: 2-3 global competitor facts (Korean). Never write about Furiosa. No "Furiosa는" sentences.
- companies: fill items from global articles. {ITEM_RULE}"""

    print("  Calling AI (global+highlights)...")
    result = _ai_call(client, prompt1, max_tokens=2500)

    # ── Call 2: Korea companies ────────────────────────────────────────────
    korea_text  = _fmt_articles(korea_articles)
    korea_block = _company_block(KOREA_COMPANIES)

    prompt2 = f"""Date:{now.strftime('%Y-%m-%d')}. Competitive intelligence analyst for Furiosa AI.
KOREA ARTICLES:\n{korea_text}
Return ONLY valid JSON (no markdown):
{{"companies":{korea_block}}}
Rule: fill items from articles above. {ITEM_RULE}"""

    print("  Calling AI (korea)...")
    try:
        korea_result = _ai_call(client, prompt2, max_tokens=2000)
    except Exception as e:
        print(f"  Korea call failed ({e}), retrying concise...")
        prompt2b = prompt2.replace("Max 2 items.", "Max 1 item. Summary max 20 Korean chars.")
        korea_result = _ai_call(client, prompt2b, max_tokens=1200)

    result["companies"] = result.get("companies", []) + korea_result.get("companies", [])
    return result


def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    articles, furiosa_articles = collect_articles()
    report = analyze(articles, furiosa_articles, now)

    meta = {c["name"]: c for c in ALL_COMPANIES}
    for co in report.get("companies", []):
        if co["name"] in meta:
            co["website"] = meta[co["name"]]["website"]
            co["blog"]    = meta[co["name"]]["blog"]
            co["region"]  = meta[co["name"]]["region"]
        co["no_update"] = not bool(co.get("items"))

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    updated = sum(1 for c in report.get("companies", []) if not c.get("no_update"))
    print(f"Done. {updated}/{len(ALL_COMPANIES)} companies with news.")


if __name__ == "__main__":
    main()
