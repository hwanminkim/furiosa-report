#!/usr/bin/env python3
"""
Daily report updater.
- Furiosa daily / weekly: raw RSS, deduped, filtered by pubDate
- Company news (competitors): raw RSS titles + URLs
- No AI calls (no token issues)
"""
import datetime
import email.utils
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen, Request
import pytz

REPO_ROOT   = Path(__file__).parent.parent
REPORT_PATH = REPO_ROOT / "report.json"

COMPANIES = [
    {"name": "NVIDIA",      "region": "global", "query": "NVIDIA AI chip GPU inference",  "lang": "en"},
    {"name": "Tenstorrent", "region": "global", "query": "Tenstorrent AI chip RISC-V",    "lang": "en"},
    {"name": "Groq",        "region": "global", "query": "Groq AI inference chip LPU",    "lang": "en"},
    {"name": "SambaNova",   "region": "global", "query": "SambaNova AI chip RDU",         "lang": "en"},
    {"name": "Cerebras",    "region": "global", "query": "Cerebras AI chip wafer",        "lang": "en"},
    {"name": "Rebellions",  "region": "korea",  "query": "리벨리온 AI 반도체 NPU",         "lang": "ko"},
    {"name": "DeepX",       "region": "korea",  "query": "딥엑스 DeepX AI NPU 반도체",     "lang": "ko"},
    {"name": "HyperAccel",  "region": "korea",  "query": "하이퍼엑셀 HyperAccel AI",       "lang": "ko"},
    {"name": "Mobilint",    "region": "korea",  "query": "모빌린트 Mobilint AI NPU",       "lang": "ko"},
]

FURIOSA_QUERIES = [
    ('FuriosaAI OR "Furiosa AI" chip', "en"),
    ('퓨리오사 OR 퓨리오사AI AI 반도체', "ko"),
]


def gnews_url(query: str, lang: str) -> str:
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


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


def fetch_articles(query: str, lang: str, n: int = 20) -> list[dict]:
    """Fetch RSS items. Returns up to n items with title/url/pub_dt."""
    try:
        req = Request(gnews_url(query, lang),
                      headers={"User-Agent": "Mozilla/5.0 (compatible; FuriosaReport/1.0)"})
        with urlopen(req, timeout=10) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"  [skip] {query}: {e}")
        return []

    results = []
    for item in root.findall(".//item")[:n]:
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        pub_dt = parse_pub_datetime(item.findtext("pubDate") or "")
        if title and link:
            results.append({"title": title, "url": link, "pub_dt": pub_dt})
    return results


def build_period(now: datetime.datetime) -> str:
    today      = now.date()
    week_start = today - datetime.timedelta(days=today.weekday())
    days_ko    = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} "
            f"~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}")


def to_output(a: dict, kst: pytz.BaseTzInfo) -> dict:
    """Strip non-JSON-serializable fields and format date for display."""
    return {
        "title": a["title"],
        "url":   a["url"],
        "date":  format_date_kst(a.get("pub_dt"), kst),
    }


def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    now_utc = now.astimezone(pytz.utc)
    daily_cutoff  = now_utc - datetime.timedelta(hours=24)
    weekly_cutoff = now_utc - datetime.timedelta(days=7)

    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    # ── 1. Competitor news (raw RSS) ─────────────────────────────────────
    companies_out = []
    for co in COMPANIES:
        articles = fetch_articles(co["query"], co["lang"], n=3)
        items = [to_output(a, kst) for a in articles]
        companies_out.append({
            "name":   co["name"],
            "region": co["region"],
            "items":  items,
        })
        print(f"  {co['name']}: {len(articles)} articles")

    # ── 2. Furiosa daily / weekly (raw RSS, deduped by URL) ──────────────
    all_furiosa: list[dict] = []
    seen_urls = set()
    for query, lang in FURIOSA_QUERIES:
        for a in fetch_articles(query, lang, n=30):
            if a["url"] in seen_urls:
                continue
            seen_urls.add(a["url"])
            all_furiosa.append(a)

    # Google News 기본 정렬을 그대로 보존 (인기도/relevance proxy).
    # 날짜 필터만 걸고 재정렬은 안 함. pub_dt 없는 기사는 제외.
    def in_window(a: dict, cutoff: datetime.datetime) -> bool:
        return a.get("pub_dt") is not None and a["pub_dt"] >= cutoff

    furiosa_daily  = [a for a in all_furiosa if in_window(a, daily_cutoff)]
    furiosa_weekly = [a for a in all_furiosa if in_window(a, weekly_cutoff)]

    print(f"  Furiosa: total={len(all_furiosa)}, daily(24h)={len(furiosa_daily)}, weekly(7d)={len(furiosa_weekly)}")

    # ── 3. Write report.json ─────────────────────────────────────────────
    report = {
        "period":         build_period(now),
        "updated_at":     now.isoformat(),
        "furiosa_daily":  [to_output(a, kst) for a in furiosa_daily],
        "furiosa_weekly": [to_output(a, kst) for a in furiosa_weekly],
        "companies":      companies_out,
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"Done. {len(report['furiosa_daily'])} daily / {len(report['furiosa_weekly'])} weekly.")


if __name__ == "__main__":
    main()
