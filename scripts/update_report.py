#!/usr/bin/env python3
"""
Daily competitive intelligence report updater.
Runs at 06:00 KST via GitHub Actions, searches for news using Claude web search,
and updates report.json.
"""
import anthropic
import json
import re
import os
import datetime
import pytz
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
REPORT_PATH = REPO_ROOT / "report.json"

COMPANIES = [
    {"name": "NVIDIA",       "website": "https://www.nvidia.com",     "blog": "https://blogs.nvidia.com"},
    {"name": "Groq",         "website": "https://groq.com",            "blog": "https://groq.com/blog"},
    {"name": "Cerebras",     "website": "https://cerebras.net",        "blog": "https://cerebras.net/blog"},
    {"name": "SambaNova",    "website": "https://sambanova.ai",        "blog": "https://sambanova.ai/blog"},
    {"name": "Tenstorrent",  "website": "https://tenstorrent.com",     "blog": "https://tenstorrent.com/blog"},
]

SYSTEM = """You are a competitive intelligence analyst for Furiosa AI, a Korean AI chip startup.
Your task: find and summarize news from the last 24 hours about AI chip and inference companies.
Write all Korean text in clear, concise business Korean.
Focus on: product launches, funding rounds, partnerships, customer wins, and market moves
that could directly affect Furiosa AI's business development."""

PROMPT_TEMPLATE = """Today is {today} (KST). Search for news from the last 24 hours about these companies:
NVIDIA, Groq, Cerebras, SambaNova, Tenstorrent — all in the context of AI chips and inference.

Return ONLY a valid JSON object. No markdown, no explanation, just raw JSON:

{{
  "period": "{period}",
  "updated_at": "{updated_at}",
  "highlights": [
    {{
      "company": "회사명",
      "text": "한 줄 핵심 요약 — 시사점 포함 (Korean)",
      "url": "기사 URL 또는 빈 문자열"
    }}
  ],
  "companies": [
    {{
      "name": "NVIDIA",
      "website": "https://www.nvidia.com",
      "blog": "https://blogs.nvidia.com",
      "no_update": false,
      "items": [
        {{"text": "1. 제목 (MM-DD) — 핵심 내용 (Korean)", "url": "기사 URL 또는 빈 문자열"}}
      ],
      "watch": "Furiosa BD팀을 위한 시사점 한 문장 (Korean)"
    }}
  ]
}}

Rules:
- highlights: 2–3 most important items across all companies
- items: max 3 per company, only real news from last 24h
- no_update: true (+ items: []) if nothing significant found
- watch: what this means for Furiosa's business development
- Keep all company website/blog URLs exactly as shown above"""


def call_claude(client: anthropic.Anthropic, messages: list) -> str:
    tools = [{"type": "web_search_20250305", "name": "web_search"}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return "".join(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            messages = messages + [{"role": "assistant", "content": response.content}]
            tool_results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                for b in response.content
                if b.type == "tool_use"
            ]
            messages = messages + [{"role": "user", "content": tool_results}]
        else:
            # Unexpected stop reason — return whatever text we have
            return "".join(b.text for b in response.content if hasattr(b, "text"))


def build_period(now: datetime.datetime) -> str:
    today = now.date()
    week_start = today - datetime.timedelta(days=today.weekday())  # Monday
    weekdays_ko = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]
    return (
        f"{week_start.strftime('%Y-%m-%d')}{weekdays_ko[week_start.weekday()]} "
        f"~ {today.strftime('%Y-%m-%d')}{weekdays_ko[today.weekday()]}"
    )


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY environment variable not set")

    client = anthropic.Anthropic(api_key=api_key)
    kst = pytz.timezone("Asia/Seoul")
    now = datetime.datetime.now(kst)

    prompt = PROMPT_TEMPLATE.format(
        today=now.strftime("%Y-%m-%d %H:%M KST"),
        period=build_period(now),
        updated_at=now.isoformat(),
    )

    print(f"[{now.strftime('%Y-%m-%d %H:%M KST')}] Fetching news via Claude web search...")
    raw = call_claude(client, [{"role": "user", "content": prompt}])

    # Extract JSON from response
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"No JSON found in Claude response:\n{raw[:800]}")

    report = json.loads(match.group())

    # Ensure website/blog are always correct
    meta = {c["name"]: c for c in COMPANIES}
    for company in report.get("companies", []):
        if company["name"] in meta:
            company["website"] = meta[company["name"]]["website"]
            company["blog"]    = meta[company["name"]]["blog"]

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"report.json updated. Highlights: {len(report.get('highlights', []))}, "
          f"Companies with news: {sum(1 for c in report.get('companies', []) if not c.get('no_update'))}")


if __name__ == "__main__":
    main()
