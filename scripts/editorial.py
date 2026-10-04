"""Step 2 — editorial: pick stories, gather facts, write the bilingual two-host script.

Episode shape
  intro → tech stories (4–5) → world briefing (2) → [CEO watch] → phrase of the day → outro
  Every story segment is spoken in English and immediately followed by a short Japanese recap.
"""
from __future__ import annotations

import json
from datetime import datetime

from collect import fetch_article_text, similarity
from common import call_with_fallback, extract_json, gemini_client, get_logger, word_count

log = get_logger("editorial")
USED_MODELS: dict[str, str] = {}  # label -> model that actually answered


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

    data, model = call_with_fallback(_models(cfg), run, label=label)
    USED_MODELS[label] = model
    return data


def _fmt_items(items: list[dict]) -> str:
    lines = []
    for it in items:
        comp = ", ".join(it.get("companies", [])[:3])
        pub = (it.get("published") or "")[:16].replace("T", " ")
        summ = f" — {it['summary']}" if it.get("summary") else ""
        also = f" (also: {', '.join(it['also'][:3])})" if it.get("also") else ""
        lines.append(f"[{it['id']}] {it.get('section', 'tech')} | {it['publisher']}{also} | {pub} | {comp} | {it['title']}{summ}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2a. select
# ---------------------------------------------------------------------------
_STORY = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "section": {"type": "string", "enum": ["tech", "world"]},
        "topic_key": {"type": "string"},
        "item_ids": {"type": "array", "items": {"type": "integer"}},
        "companies": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
        "follow_up_of": {"type": "string"},
        "new_development": {"type": "string"},
    },
    "required": ["headline", "section", "topic_key", "item_ids", "why", "follow_up_of", "new_development"],
}
SELECT_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {"type": "array", "items": _STORY},
        "alternates": {"type": "array", "items": _STORY},
        "ceo_watch_item_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["stories", "alternates", "ceo_watch_item_ids"],
}


def _fmt_recent(recent: list[dict]) -> str:
    if not recent:
        return "(none)"
    return "\n".join(f"- {r['date']} [{r.get('topic_key') or '-'}] ({r.get('section', 'tech')}) {r['title']}" for r in recent)


def _is_repeat(story: dict, recent: list[dict]) -> bool:
    keys = {r.get("topic_key") for r in recent if r.get("topic_key")}
    if story.get("follow_up_of") or story.get("topic_key") in keys:
        return True
    return any(similarity(story["headline"], r["title"]) >= 0.5 for r in recent)


def select_stories(cfg: dict, items: list[dict], now: datetime, hours: int, recent: list[dict]) -> dict:
    ep = cfg["episode"]
    n_tech, n_world = int(ep.get("tech_stories", 5)), int(ep.get("world_stories", 2))
    prompt = f"""You are the news editor of "The Silicon Commute", a weekday-morning English podcast.
Its core is technology (Big Tech, semiconductors, AI and the wider tech industry); it also covers the most important world news of the day.
The listener is a Japanese public-health policy researcher and government official in Kyoto who wants the strategically important developments, not gossip.

Today is {now:%A, %B %d, %Y} (Japan time). Candidates were published in the last {hours} hours.

CANDIDATE ITEMS (id | section | publisher | published UTC | companies | title — summary):
{_fmt_items(items)}

ALREADY COVERED IN RECENT EPISODES (date [topic_key] (section) title):
{_fmt_recent(recent)}

Choose {n_tech} TECH stories and {n_world} WORLD stories, plus up to 4 alternates (mixed), in order of importance.

TECH (section "tech") — keep it broad and varied:
- Big Tech (Alphabet/Google, Apple, Meta, Amazon, Microsoft), semiconductors (TSMC, NVIDIA, memory, equipment), and AI,
  but ALSO the wider tech world: cybersecurity incidents, platform regulation and antitrust, telecom, EVs and batteries,
  space, robotics, digital health, chips policy and supply chains, major startups and funding.
- At most ONE story per company, and at most TWO stories that are mainly about AI models or AI products.
- Prefer strategic or market-moving news: earnings and guidance, big deals, launches, regulation, supply-chain shifts, executive statements.

WORLD (section "world") — the most consequential international news of the day:
- geopolitics, conflicts and diplomacy, elections and major policy decisions, the global economy and markets (central banks, trade, energy),
  climate and major disasters, public health. Prefer stories with broad global impact or a clear link to Japan and Asia.
- Skip celebrity, sports, crime stories without wider significance, and local US politics unless globally important.

AVOID REPEATS — very important:
- Do not choose a story whose topic was already covered in the recent episodes above. Ongoing topics (e.g. a company's planned factory,
  a model launch, a court case) may return ONLY if there is a concrete new development (new decision, new numbers, official confirmation).
  In that case set "follow_up_of" to the earlier topic_key and describe the new development in "new_development". At most one follow-up.
- For new topics set "follow_up_of" and "new_development" to "".
- "topic_key": a short, stable kebab-case label for the underlying topic (e.g. "tsmc-texas-fab", "apple-taction-patent-verdict"),
  reused across days for the same topic.

OTHER RULES
- Merge items about the same event into one story (list all their ids). Prefer stories with enough factual detail in the candidates.
- Source quality: prefer established outlets (Reuters, Bloomberg, AP, BBC, CNBC, FT, WSJ, Nikkei Asia, NPR, Al Jazeera, The Guardian,
  The Verge, TechCrunch, Tom's Hardware, company newsrooms). Never pick a story whose only sources are aggregators or unknown sites.
- Skip trivia, shopping deals, game mods, single-source rumors and opinion pieces without news.

Also return "ceo_watch_item_ids": up to 3 item ids where a tech CEO's own words are the news (may overlap with stories).
Return JSON only."""
    data = _json_call(cfg, prompt, SELECT_SCHEMA, "select")
    valid = {it["id"] for it in items}
    pool = []
    for s in data.get("stories", []) + data.get("alternates", []):
        s["item_ids"] = [i for i in s.get("item_ids", []) if i in valid]
        if s["item_ids"]:
            pool.append(s)

    chosen: dict[str, list[dict]] = {"tech": [], "world": []}
    want = {"tech": n_tech, "world": n_world}
    follow_ups = 0
    used_ids: set[int] = set()
    for s in pool:
        sec = s.get("section") if s.get("section") in want else "tech"
        if len(chosen[sec]) >= want[sec] or used_ids & set(s["item_ids"]):
            continue
        if _is_repeat(s, recent):
            if not (s.get("new_development") or "").strip() or follow_ups >= 1:
                log.info("Skip repeat: %s", s["headline"])
                continue
            follow_ups += 1
        s["section"] = sec
        chosen[sec].append(s)
        used_ids |= set(s["item_ids"])
    data["stories"] = chosen["tech"] + chosen["world"]
    data["ceo_watch_item_ids"] = [i for i in data.get("ceo_watch_item_ids", []) if i in valid][:3]
    log.info(
        "Selected %d tech + %d world: %s", len(chosen["tech"]), len(chosen["world"]),
        [s["headline"] for s in data["stories"]],
    )
    return data


# ---------------------------------------------------------------------------
# 2b. gather facts (article text + optional Google Search grounding)
# ---------------------------------------------------------------------------
def gather_material(cfg: dict, items: list[dict], selection: dict, hours: int) -> tuple[str, list[dict]]:
    by_id = {it["id"]: it for it in items}
    blocks = []
    for n, s in enumerate(selection["stories"], 1):
        parts = [f"STORY {n} ({s['section'].upper()}, topic_key: {s.get('topic_key', '')}): {s['headline']}\nWhy it matters (editor): {s.get('why', '')}"]
        if s.get("new_development"):
            parts.append(f"Follow-up of an earlier story — NEW DEVELOPMENT to focus on: {s['new_development']}")
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
    prompt = f"""You are a meticulous fact-checker for a news podcast.
For each story below, search the web and report what reputable outlets published in roughly the last {hours + 24} hours:
- what exactly happened, with key numbers (units, currency, period) and dates
- who said what (short exact quotes only if you find them, with attribution)
- reactions, and why it matters
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
RECAP_LINE = {
    "type": "object",
    "properties": {"speaker": {"type": "string"}, "ja": {"type": "string"}},
    "required": ["speaker", "ja"],
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
                    "kind": {"type": "string", "enum": ["intro", "story", "world", "ceo_watch", "phrase", "outro"]},
                    "heading_en": {"type": "string"},
                    "heading_ja": {"type": "string"},
                    "lines": {"type": "array", "items": LINE},
                    "recap_ja": {"type": "array", "items": RECAP_LINE},
                },
                "required": ["kind", "heading_en", "heading_ja", "lines", "recap_ja"],
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
                    "section": {"type": "string", "enum": ["tech", "world"]},
                    "topic_key": {"type": "string"},
                    "companies": {"type": "array", "items": {"type": "string"}},
                    "source_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["title_en", "title_ja", "summary_ja", "why_ja", "section", "topic_key", "source_ids"],
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


def _writer_prompt(cfg: dict, material: str, now: datetime, target_words: int, selection: dict) -> str:
    ep, hosts = cfg["episode"], cfg["hosts"]
    a, b = hosts[0], hosts[1]
    friday = now.weekday() == 4
    monday = now.weekday() == 0
    minutes = ep["target_minutes"]
    recap = int(ep.get("recap_ja_chars", 190))
    n_tech = sum(1 for s in selection["stories"] if s["section"] == "tech")
    n_world = sum(1 for s in selection["stories"] if s["section"] == "world")
    has_ceo = bool(selection.get("ceo_watch_item_ids"))
    return f"""You are the head writer of "The Silicon Commute", a weekday-morning podcast. Its core is technology news; it also covers the day's most important world news.
The conversation is in English; after EACH story, {b['name']} gives a short recap in Japanese.

HOSTS
- {a['name']}: {a['persona']}
- {b['name']}: {b['persona']} {b['name']} is fluent in Japanese and delivers the Japanese recaps.

LISTENER
A Japanese professional (public-health policy researcher and prefectural government official in Kyoto) on a 20-minute train commute.
Goals: (1) understand what happened and why it matters; (2) train academic/business English listening, using the Japanese recap to check understanding.

TODAY: {now:%A, %B %-d, %Y}.{" It is Monday, so the material covers the weekend too." if monday else ""}

LENGTH — IMPORTANT
- English: about {target_words} words in total across all "en" lines (acceptable {int(target_words*0.9)}–{int(target_words*1.1)}), about {minutes} minutes.
- Japanese recaps: about {recap} Japanese characters per story (30–40 seconds when read aloud).

STRUCTURE — segments in this order (the material has {n_tech} tech stories, then {n_world} world stories)
1. kind "intro" (~70 words): {a['name']} greets ("Good morning, it's {now:%A, %B %-d}. This is The Silicon Commute."), {b['name']} previews three headlines, including one world story. recap_ja: [].
2. kind "story" — one segment per TECH story, most important first (each 150–210 words): what happened (facts, numbers, who), then why it matters. {b['name']} asks at least one question.
3. kind "world" — one segment per WORLD story (each 110–160 words). The first world segment opens with a short transition such as "Now, a look at the world beyond tech."
4. kind "ceo_watch" (60–100 words) — {"what tech CEOs said or posted, attributed precisely; never invent quotes" if has_ceo else "SKIP this segment (no CEO material today)"}. recap_ja: [].
5. kind "phrase" (~60 words): "Phrase of the day" — {b['name']} picks one useful English expression that appeared today, explains it simply, gives one more example. recap_ja: [].
6. kind "outro" (~35 words): one-sentence takeaway and sign-off ("See you on tomorrow's commute."{' — it is Friday: wish listeners a good weekend, see you Monday' if friday else ''}). recap_ja: one short Japanese sign-off line by {b['name']}.

JAPANESE RECAP ("recap_ja", required for every "story" and "world" segment)
- 1–3 lines, speaker "{b['name']}", natural spoken Japanese in ですます調, like an NHK radio news summary: 何が起きたか → なぜ重要か.
- The first recap of the episode starts with 「日本語でおさらいします。」; later ones may start directly.
- Do not add facts that are not in the English segment. Numbers must match exactly (million = 100万, billion = 10億, trillion = 1兆; e.g. sixty-four billion dollars = 640億ドル).
- Company, product and person names in their usual Latin spelling (NVIDIA, TSMC, Satya Nadella). 読みにくい漢字の人名・地名には括弧で読み仮名（例: 菊陽町（きくようまち））。

SPOKEN-ENGLISH RULES for "en"
- {ep['english_style']}
- speaker must be exactly "{a['name']}" or "{b['name']}". Each line 1–3 sentences, max 60 words. Alternate naturally.
- Write numbers as a broadcaster reads them: "$4.2 billion" → "4.2 billion dollars", "Q3" → "the third quarter", "2nm" → "two-nanometer".
- No URLs, markdown, emojis, stage directions or sound effects.
- ACCURACY FIRST: use only facts in the MATERIAL. If a number, date or quote is not in the material, do not state it. Attribute reporting
  ("according to Reuters"). Use names and job titles exactly as the material gives them. For follow-up stories, focus on the new development.
- Neutral and analytical. No investment advice. On conflicts and politics, report facts and attributed positions without taking sides.

LINE TRANSLATIONS
- Every English line also has "ja": a natural, accurate Japanese translation for the on-screen transcript (ですます調). Same number and name rules as above.
- Headings: heading_en short English; heading_ja 日本語。

METADATA
- title_en: catchy episode title (max 70 chars); title_ja: 日本語タイトル; summary_ja: 3文の日本語要約（テックと世界の両方に触れる）。
- stories: one entry per "story"/"world" segment, same order. section ("tech"/"world") and topic_key copied from the material;
  summary_ja 2–3文, why_ja 1–2文（産業・政策・日本への示唆）, source_ids = the [id] numbers from the material that support the story.
- vocabulary: 8–12 useful terms or collocations that actually appear in the "en" lines, from both tech and world stories.
  meaning_ja, note_ja (使い方やニュアンス), example_en = the sentence from the script where it appears.
- phrase_of_the_day: the same phrase as the "phrase" segment.

MATERIAL
{material}

Return JSON only."""


def _count_words(script: dict) -> int:
    return sum(word_count(l["en"]) for s in script.get("segments", []) if s.get("lang") != "ja" for l in s.get("lines", []))


def _count_ja_chars(script: dict) -> int:
    return sum(len(l["ja"]) for s in script.get("segments", []) if s.get("lang") == "ja" for l in s.get("lines", []))


def finalize(cfg: dict, script: dict, valid_ids: set[int] | None = None) -> dict:
    """Clean speakers/lines and expand each segment's recap_ja into a following Japanese segment."""
    names = [h["name"] for h in cfg["hosts"]]
    recap_speaker = names[1] if len(names) > 1 else names[0]
    out_segments = []
    for seg in script.get("segments", []):
        if seg.get("lang") == "ja":  # already expanded (e.g. re-finalising)
            out_segments.append(seg)
            continue
        prev = names[1]
        clean = []
        for line in seg.get("lines", []):
            en = (line.get("en") or "").strip()
            if not en:
                continue
            sp = (line.get("speaker") or "").strip()
            if sp not in names:
                sp = names[0] if prev == names[1] else names[1]
            clean.append({"speaker": sp, "en": en, "ja": (line.get("ja") or "").strip()})
            prev = sp
        if not clean:
            continue
        recap = [
            {"speaker": (r.get("speaker") if r.get("speaker") in names else recap_speaker), "ja": (r.get("ja") or "").strip(), "en": "", "lang": "ja"}
            for r in seg.pop("recap_ja", []) or [] if (r.get("ja") or "").strip()
        ]
        seg["lines"] = clean
        seg["lang"] = "en"
        out_segments.append(seg)
        if recap:
            out_segments.append({
                "kind": "recap", "lang": "ja", "of": seg.get("kind"),
                "heading_en": "Japanese recap" if seg.get("kind") in ("story", "world") else "Japanese",
                "heading_ja": "日本語でおさらい" if seg.get("kind") in ("story", "world") else "日本語",
                "lines": recap,
            })
    script["segments"] = out_segments
    if valid_ids is not None:
        for st in script.get("stories", []):
            st["source_ids"] = [i for i in st.get("source_ids", []) if i in valid_ids]
    script["word_count"] = _count_words(script)
    script["ja_chars"] = _count_ja_chars(script)
    return script


def write_script(cfg: dict, material: str, now: datetime, valid_ids: set[int], selection: dict) -> dict:
    ep = cfg["episode"]
    target = int(ep["target_minutes"] * ep["words_per_minute"])
    prompt = _writer_prompt(cfg, material, now, target, selection)
    raw = _json_call(cfg, prompt, SCRIPT_SCHEMA, "write")
    script = finalize(cfg, json.loads(json.dumps(raw)), valid_ids)
    words = script["word_count"]
    log.info("Script draft: %d English words (target %d), %d Japanese recap chars", words, target, script["ja_chars"])
    if words < target * 0.88 or words > target * 1.2:
        direction = "longer: deepen the 'why it matters' analysis and add one more exchange per story" if words < target else "shorter: tighten each story"
        revise = (
            f"The draft below has {words} English words but the target is {target} (±10%). "
            f"Rewrite it to be {direction}. Keep the same JSON structure (including recap_ja for every story/world segment), facts, stories and sources. "
            f"Do not add facts that are not in the material.\n\nMATERIAL:\n{material}\n\nDRAFT JSON:\n{json.dumps(raw, ensure_ascii=False)}\n\nReturn the full revised JSON only."
        )
        try:
            revised = finalize(cfg, _json_call(cfg, revise, SCRIPT_SCHEMA, "revise"), valid_ids)
            log.info("Revised script: %d words", revised["word_count"])
            if abs(revised["word_count"] - target) < abs(words - target):
                script = revised
        except Exception as e:  # noqa: BLE001
            log.warning("Revision failed, keeping draft: %s", e)
    # carry the editor's labels onto the stories (used to avoid repeats tomorrow)
    for st, sel in zip(script.get("stories", []), selection["stories"]):
        st.setdefault("section", sel["section"])
        st["topic_key"] = st.get("topic_key") or sel.get("topic_key", "")
    return script
