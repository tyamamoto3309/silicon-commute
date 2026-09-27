"""Step 2 — editorial: pick stories, gather facts, write the bilingual two-host script."""
from __future__ import annotations

import json
from datetime import datetime

from collect import fetch_article_text
from common import call_with_fallback, extract_json, gemini_client, get_logger, word_count

log = get_logger("editorial")


def _models(cfg: dict) -> list[str]:
    m = cfg["models"]
    return [m["text"], *m.get("text_fallbacks", [])]


def _json_call(cfg: dict, prompt: str, schema: dict | None, label: str) -> dict:
    from google.genai import types

    client = gemini_client()

    def run(model: str):
        conf = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=schema,
        )
        resp = client.models.generate_content(model=model, contents=prompt, config=conf)
        return extract_json(resp.text)

    data, _ = call_with_fallback(_models(cfg), run, label=label)
    return data


def _fmt_items(items: list[dict]) -> str:
    lines = []
    for it in items:
        comp = ", ".join(it.get("companies", [])[:3])
        pub = (it.get("published") or "")[:16].replace("T", " ")
        summ = f" — {it['summary']}" if it.get("summary") else ""
        also = f" (also: {', '.join(it['also'][:3])})" if it.get("also") else ""
        lines.append(f"[{it['id']}] {it['origin']} | {it['publisher']}{also} | {pub} | {comp} | {it['title']}{summ}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2a. select
# ---------------------------------------------------------------------------
SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "integer"}},
                    "companies": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                },
                "required": ["headline", "item_ids", "why"],
            },
        },
        "ceo_watch_item_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["stories", "ceo_watch_item_ids"],
}


def select_stories(cfg: dict, items: list[dict], now: datetime, hours: int, recent_titles: list[str]) -> dict:
    ep = cfg["episode"]
    recent = "\n".join(f"- {t}" for t in recent_titles) or "(none)"
    prompt = f"""You are the news editor of "The Silicon Commute", a weekday 10-minute English podcast about Big Tech (Alphabet/Google, Apple, Meta, Amazon, Microsoft) and the semiconductor industry.
The listener is a Japanese public-health policy researcher and government official in Kyoto who wants the strategically important moves, not gossip.

Today is {now:%A, %B %d, %Y} (Japan time). Candidates were published in the last {hours} hours.

CANDIDATE ITEMS (id | origin | publisher | published UTC | companies | title — summary):
{_fmt_items(items)}

RECENTLY COVERED (skip unless there is a material new development):
{recent}

Choose up to {ep['max_stories']} stories for today's episode.
Editorial priorities:
1. Strategic or market-moving news: earnings and guidance, AI capital spending, large deals and M&A, chip supply (TSMC, HBM memory, advanced packaging), export controls and tariffs, antitrust and regulation, major product or AI model launches.
2. What CEOs themselves said or posted (X posts, interviews, keynotes, memos).
3. Balance: at least 2 GAFAM stories and 2 semiconductor stories when material exists. Include a Japan/Asia angle only if genuinely relevant.
4. Merge items about the same event into one story (list all their ids). Prefer stories with enough factual detail in the candidates.
5. Skip trivia, shopping deals, game mods, single-source rumors and opinion pieces without news.

Also return "ceo_watch_item_ids": up to 3 item ids where a CEO's own words are the news (may overlap with stories).
Order stories from most to least important. Return JSON only."""
    data = _json_call(cfg, prompt, SELECT_SCHEMA, "select")
    valid = {it["id"] for it in items}
    for s in data.get("stories", []):
        s["item_ids"] = [i for i in s.get("item_ids", []) if i in valid]
    data["stories"] = [s for s in data.get("stories", []) if s["item_ids"]][: ep["max_stories"]]
    data["ceo_watch_item_ids"] = [i for i in data.get("ceo_watch_item_ids", []) if i in valid][:3]
    log.info("Selected %d stories: %s", len(data["stories"]), [s["headline"] for s in data["stories"]])
    return data


# ---------------------------------------------------------------------------
# 2b. gather facts (article text + optional Google Search grounding)
# ---------------------------------------------------------------------------
def gather_material(cfg: dict, items: list[dict], selection: dict, hours: int) -> tuple[str, list[dict]]:
    by_id = {it["id"]: it for it in items}
    blocks = []
    for n, s in enumerate(selection["stories"], 1):
        parts = [f"STORY {n}: {s['headline']}\nWhy it matters (editor): {s.get('why', '')}"]
        fetched = 0
        for i in s["item_ids"]:
            it = by_id[i]
            parts.append(f"  [{i}] {it['publisher']} ({(it.get('published') or '')[:10]}): {it['title']}. {it.get('summary', '')}")
            if fetched < 2:
                body = fetch_article_text(it["url"])
                if body:
                    parts.append(f"      Article text [{i}]: {body}")
                    fetched += 1
        blocks.append("\n".join(parts))
    ceo = [by_id[i] for i in selection.get("ceo_watch_item_ids", [])]
    if ceo:
        blocks.append("CEO WATCH MATERIAL:\n" + "\n".join(f"  [{c['id']}] {c['publisher']}: {c['title']}. {c.get('summary', '')}" for c in ceo))
    material = "\n\n".join(blocks)

    extra_sources: list[dict] = []
    mode = str(cfg["models"].get("search_grounding", "auto")).lower()
    if mode in ("auto", "on", "true") and selection["stories"]:
        try:
            brief, extra_sources = _grounded_research(cfg, selection, items, hours)
            material += "\n\nVERIFIED RESEARCH BRIEF (Google Search):\n" + brief
            next_id = max([it["id"] for it in items], default=0) + 1
            for s in extra_sources:
                s["id"] = next_id
                next_id += 1
            if extra_sources:
                material += "\n\nSEARCH SOURCES:\n" + "\n".join(f"  [{s['id']}] {s['publisher']}: {s['title']}" for s in extra_sources)
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:200]
            if mode == "on":
                raise
            log.info("Search grounding unavailable (free tier?) — continuing without it: %s", msg)
    return material, extra_sources


def _grounded_research(cfg: dict, selection: dict, items: list[dict], hours: int) -> tuple[str, list[dict]]:
    from google.genai import types

    client = gemini_client()
    by_id = {it["id"]: it for it in items}
    story_list = "\n".join(
        f"{n}. {s['headline']} — based on: " + "; ".join(f"{by_id[i]['publisher']}: {by_id[i]['title']}" for i in s["item_ids"][:4])
        for n, s in enumerate(selection["stories"], 1)
    )
    prompt = f"""You are a meticulous fact-checker for a tech news podcast.
For each story below, search the web and report what reputable outlets published in roughly the last {hours + 24} hours:
- what exactly happened, with key numbers (units, currency, period) and dates
- who said what (short exact quotes only if you find them, with attribution)
- market or competitor reaction, and why it matters for the industry
- any conflicting reports or corrections
Do not speculate. Write concise English bullet points per story and name the outlet for each fact.

STORIES:
{story_list}"""
    conf = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
    resp, _ = call_with_fallback(
        [cfg["models"]["text"]],
        lambda m: client.models.generate_content(model=m, contents=prompt, config=conf),
        label="research", attempts_per_model=2,
    )
    sources = []
    try:
        gm = resp.candidates[0].grounding_metadata
        seen = set()
        for ch in (gm.grounding_chunks or []):
            if ch.web and ch.web.uri and ch.web.uri not in seen:
                seen.add(ch.web.uri)
                sources.append({"origin": "search", "publisher": ch.web.title or "web", "title": ch.web.title or "", "url": ch.web.uri})
    except Exception:  # noqa: BLE001
        pass
    return resp.text or "", sources[:20]


# ---------------------------------------------------------------------------
# 2c. write the bilingual script
# ---------------------------------------------------------------------------
LINE = {
    "type": "object",
    "properties": {
        "speaker": {"type": "string"},
        "en": {"type": "string"},
        "ja": {"type": "string"},
    },
    "required": ["speaker", "en", "ja"],
}
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title_en": {"type": "string"},
        "title_ja": {"type": "string"},
        "summary_ja": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["intro", "story", "ceo_watch", "phrase", "outro"]},
                    "heading_en": {"type": "string"},
                    "heading_ja": {"type": "string"},
                    "lines": {"type": "array", "items": LINE},
                },
                "required": ["kind", "heading_en", "heading_ja", "lines"],
            },
        },
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title_en": {"type": "string"},
                    "title_ja": {"type": "string"},
                    "summary_ja": {"type": "string"},
                    "why_ja": {"type": "string"},
                    "companies": {"type": "array", "items": {"type": "string"}},
                    "source_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["title_en", "title_ja", "summary_ja", "why_ja", "source_ids"],
            },
        },
        "vocabulary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "meaning_ja": {"type": "string"},
                    "note_ja": {"type": "string"},
                    "example_en": {"type": "string"},
                },
                "required": ["term", "meaning_ja", "example_en"],
            },
        },
        "phrase_of_the_day": {
            "type": "object",
            "properties": {
                "phrase": {"type": "string"},
                "meaning_ja": {"type": "string"},
                "example_en": {"type": "string"},
                "example_ja": {"type": "string"},
            },
            "required": ["phrase", "meaning_ja", "example_en", "example_ja"],
        },
    },
    "required": ["title_en", "title_ja", "summary_ja", "segments", "stories", "vocabulary", "phrase_of_the_day"],
}


def _writer_prompt(cfg: dict, material: str, now: datetime, target_words: int) -> str:
    ep, hosts = cfg["episode"], cfg["hosts"]
    a, b = hosts[0], hosts[1]
    friday = now.weekday() == 4
    monday = now.weekday() == 0
    minutes = ep["target_minutes"]
    return f"""You are the head writer of "The Silicon Commute", a weekday-morning English podcast about Big Tech (GAFAM) and semiconductors.

HOSTS
- {a['name']}: {a['persona']}
- {b['name']}: {b['persona']}

LISTENER
A Japanese professional (public-health policy researcher and prefectural government official in Kyoto) on a 20-minute train commute. Goals: (1) understand what Big Tech and chip companies did and why it matters; (2) train academic/business English listening.

TODAY: {now:%A, %B %-d, %Y}.{" It is Monday, so the material covers the weekend too." if monday else ""}

LENGTH — IMPORTANT
About {target_words} English words in total across all "en" lines (acceptable range {int(target_words*0.9)}–{int(target_words*1.1)}), i.e. about {minutes} minutes of audio.

STRUCTURE — segments in this order
1. kind "intro" (~90 words): {a['name']} greets ("Good morning, it's {now:%A, %B %-d}. This is The Silicon Commute."), {b['name']} previews the top three headlines in one short line each.
2. kind "story", one segment per story, most important first (each 170–260 words): what happened (facts, numbers, who), then why it matters (strategy, competition, supply chain, regulation). {b['name']} asks at least one question per story. Add a Japan/Asia angle only when the material supports it.
3. kind "ceo_watch" (80–150 words): what CEOs said or posted. Attribute precisely (e.g. "Satya Nadella wrote on X that ..."). If the material has no CEO statements, discuss a leadership angle from today's stories. Never invent quotes.
4. kind "phrase" (~70 words): "Phrase of the day" — {b['name']} picks one useful business/tech English expression that appeared in today's episode, explains it simply and gives one more example.
5. kind "outro" (~40 words): one-sentence takeaway and sign-off ("See you on tomorrow's commute."{' — it is Friday, so wish listeners a good weekend and say see you Monday' if friday else ''}).

SPOKEN-ENGLISH RULES for "en"
- {ep['english_style']}
- speaker must be exactly "{a['name']}" or "{b['name']}". Each line 1–3 sentences, max 60 words. Alternate naturally.
- Write numbers as a broadcaster reads them: "$4.2 billion" → "4.2 billion dollars", "Q3" → "the third quarter", "YoY" → "compared with a year earlier", "2nm" → "two-nanometer".
- No URLs, markdown, emojis, stage directions or sound effects. Plain spoken English only.
- ACCURACY FIRST: use only facts in the MATERIAL. If a number, date or quote is not in the material, do not state it. Attribute reporting ("according to Reuters"). Use names and job titles exactly as the material gives them — executives change, so do not rely on memory.
- Neutral and analytical. No investment advice.

JAPANESE
- "ja": a natural, accurate Japanese translation of that line in friendly spoken Japanese (ですます調で統一). Keep company, product and person names in their original Latin spelling (例: NVIDIA, TSMC, Satya Nadella).
- 読みにくい漢字の人名・地名には括弧で読み仮名を付ける（例: 菊陽町（きくようまち））。
- Headings: heading_en short English; heading_ja 日本語。

METADATA
- title_en: catchy episode title (max 70 chars); title_ja: 日本語タイトル; summary_ja: 3文の日本語要約。
- stories: one entry per story segment, same order. summary_ja 2–3文, why_ja 1–2文（産業・政策への示唆）, source_ids = the [id] numbers from the material that support the story.
- vocabulary: 8–12 useful terms or collocations that actually appear in the "en" lines (e.g. "capital expenditure", "ramp up production"). meaning_ja = 日本語の意味, note_ja = 使い方やニュアンス, example_en = the sentence from the script where it appears.
- phrase_of_the_day: the same phrase as the "phrase" segment.

MATERIAL
{material}

Return JSON only."""


def _count_words(script: dict) -> int:
    return sum(word_count(l["en"]) for s in script.get("segments", []) for l in s.get("lines", []))


def _normalise(cfg: dict, script: dict, valid_ids: set[int]) -> dict:
    names = [h["name"] for h in cfg["hosts"]]
    for seg in script.get("segments", []):
        prev = names[1]
        clean_lines = []
        for line in seg.get("lines", []):
            en = (line.get("en") or "").strip()
            if not en:
                continue
            sp = line.get("speaker", "").strip()
            if sp not in names:
                sp = names[0] if prev == names[1] else names[1]
            line["speaker"], line["en"], line["ja"] = sp, en, (line.get("ja") or "").strip()
            prev = sp
            clean_lines.append(line)
        seg["lines"] = clean_lines
    script["segments"] = [s for s in script.get("segments", []) if s["lines"]]
    for st in script.get("stories", []):
        st["source_ids"] = [i for i in st.get("source_ids", []) if i in valid_ids]
    return script


def write_script(cfg: dict, material: str, now: datetime, valid_ids: set[int]) -> dict:
    ep = cfg["episode"]
    target = int(ep["target_minutes"] * ep["words_per_minute"])
    prompt = _writer_prompt(cfg, material, now, target)
    script = _normalise(cfg, _json_call(cfg, prompt, SCRIPT_SCHEMA, "write"), valid_ids)
    words = _count_words(script)
    log.info("Script draft: %d words (target %d)", words, target)
    if words < target * 0.8 or words > target * 1.25:
        direction = "longer: deepen the 'why it matters' analysis and add one more exchange per story" if words < target else "shorter: tighten each story"
        revise = (
            f"The draft below has {words} English words but the target is {target} (±10%). "
            f"Rewrite it to be {direction}. Keep the same JSON structure, facts, stories and sources. "
            f"Do not add facts that are not in the material.\n\nMATERIAL:\n{material}\n\nDRAFT JSON:\n{json.dumps(script, ensure_ascii=False)}\n\nReturn the full revised JSON only."
        )
        try:
            revised = _normalise(cfg, _json_call(cfg, revise, SCRIPT_SCHEMA, "revise"), valid_ids)
            w2 = _count_words(revised)
            log.info("Revised script: %d words", w2)
            if abs(w2 - target) < abs(words - target):
                script, words = revised, w2
        except Exception as e:  # noqa: BLE001
            log.warning("Revision failed, keeping draft: %s", e)
    script["word_count"] = words
    return script
