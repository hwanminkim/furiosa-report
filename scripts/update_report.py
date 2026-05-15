#!/usr/bin/env python3
"""
Weekly highlights updater — highlights only, no per-company articles.
Uses Google News RSS + GitHub Models (GPT-4o-mini via GITHUB_TOKEN).
"""
import datetime
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

GLOBAL_QUERIES = [
    ("NVIDIA AI chip GPU inference",    "en"),
    ("Groq AI inference chip LPU",      "en"),
    ("Cerebras AI chip wafer",          "en"),
    ("SambaNova AI chip RDU",           "en"),
    ("Tenstorrent AI chip RISC-V",      "en"),
]

KOREA_QUERIES = [
    ("리벨리온 AI 반도체 NPU",   "ko"),
    ("딥엑스 DeepX AI NPU",      "ko"),
    ("하이퍼엑셀 HyperAccel AI", "ko"),
    ("모빌린트 Mobilint AI NPU", "ko"),
]

FURIOSA_QUERIES = [
    ('FuriosaAI OR "Furiosa AI" chip', "en"),
    ('퓨리오사 OR 퓨리오사AI AI 반도체', "ko"),
]


def gnews_url(query: str, lang: str = "en") -> str:
    if lang == "ko":
        return f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


def fetch_titles(query: str, lang: str, n: int = 3) -> list[dict]:
    url = gnews_url(query, lang)
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FuriosaReport/1.0)"})
        with urlopen(req, timeout=10) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"  [skip] {query}: {e}")
        return []

    results = []
    for item in root.findall(".//item")[:n]:
        title = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        if title:
            results.append({"title": title, "url": link})
    return results


def build_period(now: datetime.datetime) -> str:
    today      = now.date()
    week_start = today - datetime.timedelta(days=today.weekday())
    days_ko    = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (f"{week_start.strftime('%Y-%m-%d')}{days_ko[week_start.weekday()]} "
            f"~ {today.strftime('%Y-%m-%d')}{days_ko[today.weekday()]}")


def main():
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)
    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Starting highlights update...")

    # Fetch competitor headlines
    competitor_lines = []
    for query, lang in GLOBAL_QUERIES + KOREA_QUERIES:
        for a in fetch_titles(query, lang, n=2):
            competitor_lines.append(f"{a['title']} | {a['url']}")
    print(f"  Competitor articles: {len(competitor_lines)}")

    # Fetch Furiosa headlines
    furiosa_lines = []
    seen = set()
    for query, lang in FURIOSA_QUERIES:
        for a in fetch_titles(query, lang, n=4):
            if a['url'] not in seen:
                seen.add(a['url'])
                furiosa_lines.append(f"{a['title']} | {a['url']}")
    print(f"  Furiosa articles: {len(furiosa_lines)}")

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError("GITHUB_TOKEN is not set")

    client = OpenAI(base_url="https://models.inference.ai.azure.com", api_key=token)

    prompt = f"""Date: {now.strftime('%Y-%m-%d KST')}. Competitive intelligence analyst for Furiosa AI.

COMPETITOR ARTICLES:
{chr(10).join(competitor_lines) or '없음'}

FURIOSA ARTICLES:
{chr(10).join(furiosa_lines) or '없음'}

Return ONLY valid JSON (no markdown):
{{"furiosa_highlights":[{{"text":"팩트 한줄(Korean)","url":""}}],"highlights":[{{"company":"회사명","text":"팩트 한줄(Korean)","url":""}}]}}

Rules:
- furiosa_highlights: 2-3 items. Furiosa 뉴스 팩트만 (Korean). 추천/의견 금지.
- highlights: 3-4 items. 경쟁사 뉴스 팩트만 (Korean). Furiosa 언급 절대 금지. "Furiosa는" 시작 금지. 실제 기사 내용만."""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
        temperature=0.3,
    )
    raw = resp.choices[0].message.content
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"No JSON:\n{raw[:400]}")
    result = json.loads(match.group())

    report = {
        "period":             build_period(now),
        "updated_at":         now.isoformat(),
        "furiosa_highlights": result.get("furiosa_highlights", []),
        "highlights":         result.get("highlights", []),
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"Done. {len(report['furiosa_highlights'])} Furiosa / {len(report['highlights'])} competitor highlights.")


if __name__ == "__main__":
    main()
