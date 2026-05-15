#!/usr/bin/env python3
"""
Daily report updater.
- Highlights (Furiosa + competitor): AI-generated via GitHub Models
- Company news: raw RSS titles + URLs, no AI (no token issues)
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


def parse_date(raw: str) -> str:
    try:
        t = email.utils.parsedate(raw)
        if t:
            return datetime.datetime(*t[:6]).strftime("%m-%d")
    except Exception:
        pass
    return ""


def fetch_articles(query: str, lang: str, n: int = 3) -> list[dict]:
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
        date  = parse_date(item.findtext("pubDate") or "")
        if title:
            results.append({"title": title, "url": link, "date": date})
    return results


def build_period(now: datetime.datetime) -> str:
    today      = now.date()
    week_start = today - datetime.timedelta(days=today.weekday())
    days_ko    = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} "
            f"~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}")


def safe_json_parse(raw: str) -> dict:
    """
    Robust JSON parser for LLM output.
    Tries strict parse first, then a few common-fix fallbacks.
    Raises ValueError with the raw output if all attempts fail.
    """
    # 1) Strict parse on the whole string
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2) Extract the largest {...} block (in case model added prose)
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"No JSON object found in model output:\n{raw[:800]}")
    candidate = match.group()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # 3) Remove trailing commas (",}" or ",]")
    cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Failed to parse JSON (last attempt): {e}\n"
            f"--- raw model output ---\n{raw}\n"
            f"--- extracted candidate ---\n{candidate}"
        ) from e


def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting report update...")

    # ── 1. Company news (raw RSS, no AI) ─────────────────────────────────
    companies_out = []
    all_competitor_lines = []
    for co in COMPANIES:
        articles = fetch_articles(co["query"], co["lang"], n=3)
        items = [{"title": a["title"], "url": a["url"], "date": a["date"]} for a in articles]
        companies_out.append({
            "name":   co["name"],
            "region": co["region"],
            "items":  items,
        })
        for a in articles[:2]:
            all_competitor_lines.append(f"[{co['name']}] {a['title']} | {a['url']}")
        print(f"  {co['name']}: {len(articles)} articles")

    # ── 2. Highlights (AI) ────────────────────────────────────────────────
    furiosa_lines = []
    seen = set()
    for query, lang in FURIOSA_QUERIES:
        for a in fetch_articles(query, lang, n=4):
            if a["url"] not in seen:
                seen.add(a["url"])
                furiosa_lines.append(f"{a['title']} | {a['url']}")
    print(f"  Furiosa: {len(furiosa_lines)} articles")

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN is not set")

    client = OpenAI(base_url="https://models.inference.ai.azure.com", api_key=token)

    prompt = f"""Date: {now.strftime('%Y-%m-%d KST')}. Competitive intelligence analyst for Furiosa AI.

COMPETITOR ARTICLES:
{chr(10).join(all_competitor_lines) or '없음'}

FURIOSA ARTICLES:
{chr(10).join(furiosa_lines) or '없음'}

Return a JSON object with this exact schema:
{{
  "furiosa_highlights": [{{"text": "팩트 한줄(Korean)", "url": ""}}],
  "highlights":         [{{"company": "회사명", "text": "팩트 한줄(Korean)", "url": ""}}]
}}

Rules:
- furiosa_highlights: 2-3 items. Furiosa 뉴스 팩트만 (Korean). 추천/의견 금지.
- highlights: 3-4 items. 경쟁사 뉴스 팩트만 (Korean). Furiosa 언급 절대 금지. "Furiosa는" 시작 금지.
- text 안에는 쌍따옴표(") 를 사용하지 말 것. 필요한 경우 작은따옴표(') 또는 한국식 따옴표(「」, ‘’)로 대체할 것.
- URL은 입력에서 제공된 것 그대로 복사할 것."""

    print("  Calling AI for highlights...")

    # JSON 모드 시도 (지원하지 않는 모델/엔드포인트면 자동 fallback)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        print(f"  [warn] response_format unsupported, retrying without it: {e}")
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.3,
        )

    raw = resp.choices[0].message.content or ""
    hl = safe_json_parse(raw)

    report = {
        "period":             build_period(now),
        "updated_at":         now.isoformat(),
        "furiosa_highlights": hl.get("furiosa_highlights", []),
        "highlights":         hl.get("highlights", []),
        "companies":          companies_out,
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"Done. {len(report['furiosa_highlights'])} Furiosa / {len(report['highlights'])} competitor highlights.")


if __name__ == "__main__":
    main()
