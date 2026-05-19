"""
News pipeline: fetch free RSS feeds -> Gemini-assess each story -> write data.json.

Usage:
    set GEMINI_API_KEY=your_key_here
    python pipeline.py

The HTML frontend (index.html) reads data.json and renders the morning brief.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape

import feedparser
import google.generativeai as genai

# ----- config -----

FEEDS = [
    ("CNBC Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ("CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("MarketWatch Top", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("MarketWatch Bonds", "https://feeds.content.dowjones.io/public/rss/mw_bondsmarkets"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
    ("Investing.com Forex", "https://www.investing.com/rss/news_1.rss"),
    ("Investing.com Commodities", "https://www.investing.com/rss/news_11.rss"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
    ("Economist Finance", "https://www.economist.com/finance-and-economics/rss.xml"),
]

MAX_PER_FEED = 4
MAX_STORIES_TOTAL = 20
BATCH_SIZE = 4  # number of stories analyzed per Gemini call
MODEL_NAME = "gemini-2.0-flash"  # highest free-tier RPD (1500/day); fallback chain handles unavailability
MAX_TICKERS_PER_PROMPT = 45  # slightly larger since batch covers multiple stories
# Always include these macro/sector tickers regardless of headline keywords
ALWAYS_INCLUDE = {"SPY", "QQQ", "TLT", "GLD", "UUP", "VXX", "XLF", "XLE", "XLK", "EEM", "FXI", "HYG", "LQD"}

DATA_PATH = os.path.join(os.path.dirname(__file__), "data.json")
TICKERS_PATH = os.path.join(os.path.dirname(__file__), "tickers.json")

# ----- helpers -----

def strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    return unescape(s).strip()


def load_universe():
    with open(TICKERS_PATH, encoding="utf-8") as f:
        return json.load(f)["tickers"]


def fetch_news():
    items = []
    seen_titles = set()
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"  ! {source}: feed parse failed: {e}", file=sys.stderr)
            continue
        if not feed.entries:
            print(f"  ! {source}: no entries", file=sys.stderr)
            continue
        added = 0
        for entry in feed.entries:
            if added >= MAX_PER_FEED:
                break
            title = strip_html(entry.get("title", ""))
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            items.append({
                "source": source,
                "title": title,
                "link": entry.get("link", ""),
                "published": entry.get("published", entry.get("updated", "")),
                "summary": strip_html(entry.get("summary", ""))[:800],
            })
            added += 1
        print(f"  + {source}: {added} items")
    return items[:MAX_STORIES_TOTAL]


# ----- prompts -----

ASSESS_SCHEMA_INSTRUCTIONS = """
Return a single JSON object with this exact shape:

{
  "skip": false,
  "skip_reason": "",
  "plain_headline": "<one-line plain-English headline, no jargon, under 90 chars>",
  "what_happened": "<2-3 sentences explaining the news in plain English for a finance student>",
  "what_happened_verbose": "<longer version (4-5 sentences) for users who toggle 'explain like I'm starting out' — define any jargon inline>",
  "theme": "<one of: Macro, Rates, Monetary Policy, Fiscal, Geopolitics, Energy, Commodities, Banks, Financials, Technology, AI Infra, Industrials, Defence, Auto, Healthcare, Real Estate, Crypto, FX, Consumer, Other>",
  "impact": "<one of: high, medium, low>",
  "affected": [
    {
      "ticker": "<ticker from the provided universe only>",
      "direction": "<one of: benefits, hurts, mixed>",
      "expected_move": "<one of: small, moderate, large> — small ~1-2%, moderate ~3-5%, large >5% over the horizon",
      "reason": "<one short sentence linking the news to this specific company>"
    }
  ],
  "scenarios": {
    "base": {
      "label": "<6-10 word label for the base case>",
      "condition": "<one sentence: what would have to hold>",
      "horizon": "<e.g. 'next 1-2 weeks' or 'next quarter'>",
      "likelihood": "<one of: most_likely, plausible, less_likely, tail_risk>",
      "likelihood_pct": "<integer 0-100, your rough estimate. The three scenarios' likelihood_pct values should sum to approximately 100.>",
      "tickers": [{"ticker": "...", "direction": "benefits|hurts|mixed", "expected_move": "small|moderate|large", "reason": "..."}]
    },
    "upside": { ... same shape ... },
    "downside": { ... same shape ... }
  }
}

EDITORIAL FOCUS — be generous with what you include:
This brief is for finance and economics students. Produce a card for almost every story that has ANY market angle. The reader filters by impact rating and theme on their end — your job is to surface, classify, and rate.

Rate impact honestly:
  - "high": material market-moving story (Fed decision, megacap earnings surprise, geopolitical shock, big M&A, major data print)
  - "medium": meaningful but not market-moving (sector trends, individual stock catalysts, secondary data)
  - "low": niche or low-stakes (single small-cap story, marginal earnings, weak signal)

Set theme to capture WHAT the story is about. Macro/Rates/Banks/Energy/Tech/Defence etc. are the meat — Consumer is fine when the story has a consumer angle, just rate impact accordingly.

ONLY skip these (set "skip": true):
  - Pure personal-finance advice ("5 money moves to retire by 40", "I inherited a house")
  - Celebrity gossip or lifestyle pieces with no public-company impact
  - Travel destination / dining / entertainment reviews
  - Articles that are pure list-bait with no specific market event (e.g. "JPMorgan's reading list", "best stocks to buy according to ChatGPT")
  - Stories you genuinely cannot link to ANY ticker in the universe with a real mechanism

Single-retailer corporate drama (CEO feuds, store closings) IS acceptable as a low-impact Consumer card — don't skip it.
Earnings, guidance changes, downgrades/upgrades on covered tickers ARE acceptable.

Hard rules:
1. ONLY use tickers from the provided universe. Never invent or recall tickers from memory.
2. Set "skip": true ONLY for the categories listed above. Default is to produce a card.
3. Each "affected" item and each ticker in scenarios MUST quote a specific mechanism (revenue, cost, geography, rates exposure, product line, policy channel). Vague ties like "benefits from the news" are not acceptable.
4. Keep scenarios concrete and falsifiable. Each scenario should name 2-4 tickers max.
5. plain_headline must be plain English. Strip jargon. A student should understand it.
6. Prefer macro/sector ETFs (TLT, XLF, XLE, KRE, SMH, GLD, UUP, VIX/VXX, EEM, FXI, KWEB, HYG, LQD) for systemic stories where individual-name attribution would be misleading.
7. Likelihood: the base case is usually (but not always) the most_likely. Be honest if upside or downside is actually more likely. likelihood_pct is your rough qualitative estimate, not a calibrated probability. The three should sum to ~100.
8. Expected_move: use "large" sparingly — only for stories where historical analogues suggest >5% moves are plausible (major earnings surprises, central bank surprises, geopolitical shocks). Most stories should be "small" or "moderate".
9. Do NOT include any text outside the JSON object. No markdown fences.
"""


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]+")


def _tokens(text: str):
    return set(_WORD_RE.findall(text.lower()))


def shortlist_tickers(item, universe, top_n=MAX_TICKERS_PER_PROMPT):
    """Cheap keyword/theme/sector scorer — pick tickers most likely to be relevant to the story.

    Avoids sending the full universe in every prompt (saves ~80% of tokens) while keeping
    the grounding-over-recall discipline intact.
    """
    text = f"{item['title']} {item['summary']}".lower()
    tokens = _tokens(text)

    scored = []
    for t in universe:
        score = 0
        ticker = t["ticker"]
        name = t["name"].lower()
        sector = t.get("sector", "").lower()
        themes = [th.lower() for th in t.get("themes", [])]
        desc_tokens = _tokens(t.get("description", ""))

        # direct mentions are strongest signal
        if ticker.lower() in tokens or any(part in tokens for part in name.split()):
            score += 10
        if name in text:
            score += 8
        # sector mention
        if sector and sector in text:
            score += 4
        # theme overlap
        for theme in themes:
            for piece in theme.split("-"):
                if len(piece) > 3 and piece in tokens:
                    score += 2
        # description-token overlap (weak but useful for macro stories)
        overlap = len(tokens & desc_tokens)
        if overlap >= 2:
            score += min(overlap, 5)

        scored.append((score, t))

    scored.sort(key=lambda x: x[0], reverse=True)
    picks = [t for score, t in scored if score > 0][:top_n]

    # ensure always-include macro/sector ETFs are present
    have = {t["ticker"] for t in picks}
    for t in universe:
        if t["ticker"] in ALWAYS_INCLUDE and t["ticker"] not in have:
            picks.append(t)

    return picks


def build_assess_prompt(item, universe):
    shortlist = shortlist_tickers(item, universe)
    universe_str = "\n".join(
        f"- {t['ticker']} ({t['sector']}): {t['description']}" for t in shortlist
    )
    return f"""You are a financial news analyst writing for a student audience.
Convert one news headline into a structured news card following the schema below.

NEWS ITEM:
Source: {item['source']}
Title: {item['title']}
Summary: {item['summary']}

TICKER UNIVERSE (you may ONLY reference these tickers):
{universe_str}

{ASSESS_SCHEMA_INSTRUCTIONS}
"""


def build_batch_prompt(items, universe):
    """Build a single prompt that asks Gemini to analyze multiple stories at once."""
    # Merge each item's shortlist into one combined universe for the batch
    seen = {}
    for item in items:
        for t in shortlist_tickers(item, universe, top_n=30):
            seen.setdefault(t["ticker"], t)
    universe_str = "\n".join(
        f"- {t['ticker']} ({t['sector']}): {t['description']}" for t in seen.values()
    )

    stories_str = "\n\n".join(
        f"STORY {i + 1}:\nSource: {item['source']}\nTitle: {item['title']}\nSummary: {item['summary']}"
        for i, item in enumerate(items)
    )

    return f"""You are a financial news analyst writing for a student audience.
Analyze each of the following news stories and produce a structured card for each.

NEWS STORIES (analyze each one separately, in order):
{stories_str}

TICKER UNIVERSE (you may ONLY reference these tickers):
{universe_str}

Return a single JSON object: {{"cards": [card_for_story_1, card_for_story_2, ...]}}
The cards array MUST have exactly {len(items)} entries, in the same order as the stories.
Each card follows this schema:

{ASSESS_SCHEMA_INSTRUCTIONS}
"""


SUMMARY_PROMPT = """You are writing the top-of-page hero summary for a daily financial news brief aimed at students.

Below are today's news cards (already analyzed). Write a 3-4 sentence paragraph that captures what a reader needs to know today. Plain English, no jargon, no tickers, no bullet points. Be specific (mention actual events, not "markets moved").

Return ONLY the paragraph text. No quotes, no JSON, no preamble.

CARDS:
{cards_json}
"""


# ----- main pipeline -----

def configure_models():
    """Return a list of (name, GenerativeModel) pairs available to use.

    Each Gemini model has its own free-tier quota bucket — when one hits its daily limit,
    we rotate to the next. Gives effective quota of ~4500 RPD across all three flash models.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        print("Get a free key at https://aistudio.google.com/app/apikey", file=sys.stderr)
        sys.exit(1)
    genai.configure(api_key=api_key)

    # Ordered by free-tier RPD ceiling (highest first), with the user's preferred at front if set
    preferred_order = [MODEL_NAME, "gemini-2.0-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-1.5-flash"]
    # dedupe while preserving order
    seen = set()
    ordered = []
    for name in preferred_order:
        if name not in seen:
            seen.add(name)
            ordered.append(name)

    try:
        avail_names = {m.name.split("/")[-1] for m in genai.list_models()
                       if "generateContent" in getattr(m, "supported_generation_methods", [])}
    except Exception as e:
        print(f"  ! Could not list models ({e}). Trying all candidates blindly.", file=sys.stderr)
        avail_names = set(ordered)

    models = []
    for name in ordered:
        if name in avail_names:
            models.append((name, genai.GenerativeModel(
                name,
                generation_config={"response_mime_type": "application/json", "temperature": 0.3},
            )))

    if not models:
        print(f"ERROR: none of {ordered} are available on this API key.", file=sys.stderr)
        print(f"Available models with generateContent:", file=sys.stderr)
        for n in avail_names:
            if "flash" in n or "pro" in n:
                print(f"   - {n}", file=sys.stderr)
        sys.exit(1)

    print(f"  -> {len(models)} models available: {', '.join(n for n, _ in models)}")
    return models


def configure_text_model(model_name):
    return genai.GenerativeModel(
        model_name,
        generation_config={"temperature": 0.4},
    )


class QuotaExhausted(Exception):
    pass


def _parse_retry_delay(error_str: str):
    """Extract a retry delay (in seconds) from a Gemini 429 error string. Returns None if not found."""
    m = re.search(r"retry_delay\s*\{\s*seconds:\s*(\d+)", error_str)
    if m:
        return int(m.group(1))
    m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", error_str)
    if m:
        return int(float(m.group(1)))
    return None


def _is_daily_quota(error_str: str) -> bool:
    s = error_str.lower()
    return any(needle in s for needle in ["perday", "requestsperday", "daily", "per day"])


def _handle_quota_error(e: Exception, attempt: int) -> int:
    """Decide what to do on a 429. Returns wait-time in seconds (then caller should retry),
    OR raises QuotaExhausted if we should give up entirely."""
    msg = str(e)
    delay = _parse_retry_delay(msg)
    if _is_daily_quota(msg):
        raise QuotaExhausted(msg)
    if delay is None:
        # ambiguous 429 — assume RPM if early attempt, daily otherwise
        if attempt < 2:
            return 65
        raise QuotaExhausted(msg)
    if delay > 300:
        # >5 min: probably daily, treat as exhausted
        raise QuotaExhausted(msg)
    return delay + 2  # buffer


def _call_model(model, prompt, label, max_retries=3):
    last_err = None
    for attempt in range(max_retries):
        try:
            resp = model.generate_content(prompt)
            text = resp.text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text, flags=re.MULTILINE).strip()
            return json.loads(text)
        except Exception as e:
            last_err = e
            msg_lower = str(e).lower()
            if "429" in msg_lower:
                wait = _handle_quota_error(e, attempt)  # may raise QuotaExhausted
                print(f"     RPM limit hit, waiting {wait}s before retry ({label})...", file=sys.stderr)
                time.sleep(wait)
                continue
            is_transient = any(s in msg_lower for s in ["503", "500", "deadline", "unavailable"])
            if attempt < max_retries - 1 and is_transient:
                backoff = 4 * (2 ** attempt)
                print(f"     transient error, retrying in {backoff}s ({label})...", file=sys.stderr)
                time.sleep(backoff)
                continue
            break
    raise RuntimeError(f"call failed for {label}: {str(last_err).split(chr(10))[0][:200]}")


def assess_one(model, item, universe):
    """Single-story assessment (kept for back-compat; main loop uses batches)."""
    try:
        return _call_model(model, build_assess_prompt(item, universe), label=item["title"][:50])
    except QuotaExhausted:
        raise
    except Exception as e:
        print(f"  ! assess failed: {e}", file=sys.stderr)
        return None


def assess_batch(model, items, universe):
    """Analyze a batch of stories in a single API call. Returns list of results aligned to items.
    Each entry is either a card dict (with possible skip:true) or None on parse failure."""
    try:
        data = _call_model(model, build_batch_prompt(items, universe), label=f"batch of {len(items)}")
    except QuotaExhausted:
        raise
    except Exception as e:
        print(f"  ! batch failed entirely: {e}", file=sys.stderr)
        return [None] * len(items)

    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, list):
        print(f"  ! batch returned unexpected shape (no 'cards' array)", file=sys.stderr)
        return [None] * len(items)

    # Pad or trim to match input length
    if len(cards) < len(items):
        cards = cards + [None] * (len(items) - len(cards))
    elif len(cards) > len(items):
        cards = cards[:len(items)]
    return cards


def deterministic_hero(cards, quota_hit=False):
    """Build a hero summary directly from the analyzed cards — no LLM call required.

    Used both as a fallback when the LLM hero call fails AND as a fully deterministic option
    when quota has been hit. Reads natural because it's stitching together actual headlines
    and themes from the cards.
    """
    if not cards:
        return "No stories made it through analysis today."

    impact_rank = {"high": 3, "medium": 2, "low": 1}
    sorted_cards = sorted(cards, key=lambda c: impact_rank.get(c["card"].get("impact", "low"), 1), reverse=True)

    # group by theme
    by_theme = {}
    for c in cards:
        theme = c["card"].get("theme", "Other")
        by_theme.setdefault(theme, []).append(c["card"].get("plain_headline", ""))

    parts = []

    # lead with the highest-impact story
    top = sorted_cards[0]["card"]
    parts.append(f"The biggest story today is on the {top.get('theme', 'markets')} side: {top.get('plain_headline', '')}.")

    # mention the next 1-2 high/medium impact stories briefly
    secondary = [c["card"] for c in sorted_cards[1:3] if c["card"].get("plain_headline")]
    if secondary:
        if len(secondary) == 1:
            parts.append(f"Also worth watching: {secondary[0].get('plain_headline', '').rstrip('.')}.")
        else:
            heads = " and ".join(s.get("plain_headline", "").rstrip(".") for s in secondary)
            parts.append(f"Also worth watching: {heads}.")

    # theme spread
    themes = list(by_theme.keys())
    if len(themes) > 1:
        parts.append(f"Overall the brief covers {len(cards)} stor{'y' if len(cards) == 1 else 'ies'} across {', '.join(themes[:-1])} and {themes[-1]}.")
    else:
        parts.append(f"All {len(cards)} stor{'y' if len(cards) == 1 else 'ies'} today center on {themes[0]}.")

    if quota_hit:
        parts.append("(Today's brief was capped early — the daily LLM quota ran out, so this is a partial slice of the news.)")

    return " ".join(parts)


def generate_hero(text_model, cards):
    """Try LLM-based hero summary; on any failure, return the deterministic version."""
    slim = [
        {"headline": c["card"]["plain_headline"], "theme": c["card"]["theme"]}
        for c in cards
    ]
    try:
        resp = text_model.generate_content(SUMMARY_PROMPT.format(cards_json=json.dumps(slim, indent=2)))
        return resp.text.strip().strip('"')
    except Exception as e:
        print(f"  ! hero gen failed, using deterministic fallback: {str(e).split(chr(10))[0][:120]}", file=sys.stderr)
        return deterministic_hero(cards)


def main():
    dry_run = "--dry-run" in sys.argv
    print("News pipeline starting..." + (" (DRY RUN — no API calls)" if dry_run else ""))
    print("\n[1/4] Fetching RSS feeds")
    items = fetch_news()
    print(f"  -> {len(items)} stories collected")

    if not items:
        print("No stories. Exiting.", file=sys.stderr)
        sys.exit(1)

    print("\n[2/4] Loading ticker universe")
    universe = load_universe()
    print(f"  -> {len(universe)} tickers loaded")

    if dry_run:
        print("\n[DRY RUN] Showing what the pre-filter would pick for each story:")
        for i, item in enumerate(items, 1):
            picks = shortlist_tickers(item, universe)
            tickers_str = ", ".join(t["ticker"] for t in picks)
            print(f"\n  [{i}/{len(items)}] {item['title'][:80]}")
            print(f"     source: {item['source']}")
            print(f"     -> {len(picks)} tickers: {tickers_str}")
        print("\nDry run complete. No API calls made. Remove --dry-run to actually generate the brief.")
        return

    print(f"\n[3/4] Calling Gemini in batches of {BATCH_SIZE}")
    models = configure_models()
    model_idx = 0
    print(f"  Starting with model: {models[model_idx][0]}")

    cards = []
    quota_hit = False
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]

    for bi, batch in enumerate(batches, 1):
        print(f"\n  Batch {bi}/{len(batches)} on {models[model_idx][0]} ({len(batch)} stories):")
        for item in batch:
            print(f"    - {item['title'][:75]}")

        # try the current model; on QuotaExhausted, rotate to the next model and retry the batch
        results = None
        while model_idx < len(models):
            try:
                results = assess_batch(models[model_idx][1], batch, universe)
                break
            except QuotaExhausted as e:
                print(f"  !! {models[model_idx][0]} daily quota exhausted.", file=sys.stderr)
                model_idx += 1
                if model_idx < len(models):
                    print(f"     Rotating to next model: {models[model_idx][0]}", file=sys.stderr)
                    time.sleep(2)
                else:
                    print(f"     All models exhausted. Stopping with {len(cards)} cards so far.", file=sys.stderr)
                    quota_hit = True

        if results is None:
            break

        for item, result in zip(batch, results):
            if result is None:
                continue
            if result.get("skip"):
                print(f"     skipped '{item['title'][:50]}': {result.get('skip_reason', 'no reason')}")
                continue
            cards.append({"source_item": item, "card": result})
        time.sleep(5)  # gentle pacing between batches

    if not cards:
        print("\n!! No cards produced. All models hit quota before any story succeeded.", file=sys.stderr)
        print("   Daily Gemini free-tier quota likely exhausted across all models for your project.", file=sys.stderr)
        print("   Quotas reset at midnight Pacific time (~8 AM UK). data.json was NOT overwritten.", file=sys.stderr)
        sys.exit(1)

    print(f"\n  -> {len(cards)} cards produced")

    print("\n[4/4] Writing hero summary and data.json")
    if quota_hit:
        # don't burn another LLM call we know will fail — build a real summary from the cards directly
        hero = deterministic_hero(cards, quota_hit=True)
    else:
        text_model = configure_text_model(models[model_idx][0])
        hero = generate_hero(text_model, cards)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hero_summary": hero,
        "cards": cards,
    }
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"  -> wrote {DATA_PATH} ({len(cards)} cards)")
    print("\nDone. Open index.html in your browser.")


if __name__ == "__main__":
    main()
