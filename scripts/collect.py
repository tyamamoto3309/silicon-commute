"""Step 1 — collect candidate news items.

Sources
  * Google News RSS search (one query per company)
  * Tech-media RSS feeds with summaries (filtered by company keywords)
  * Official company newsrooms
  * X (Twitter) posts by CEOs / official accounts — only if X_BEARER_TOKEN is set
"""
from __future__ import annotations

import html
import math
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import feedparser
import requests

from common import ROOT, get_logger, read_json, write_json

log = get_logger("collect")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15 SiliconCommuteBot/1.0 (personal podcast)"
TIMEOUT = 20
X_CACHE = ROOT / "state" / "x_users.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _clean(text: str, limit: int = 420) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def _get_feed(url: str):
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, */*"}, timeout=TIMEOUT)
    r.raise_for_status()
    return feedparser.parse(r.content)


def company_keywords(cfg: dict) -> dict[str, re.Pattern]:
    """Build a case-sensitive keyword regex per company from its Google News query."""
    out = {}
    for c in cfg["companies"]:
        q = c.get("query", "")
        phrases = re.findall(r'"([^"]+)"', q)
        bare = re.sub(r'"[^"]+"', " ", q)
        tokens = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9\.\-&]+", bare) if t not in {"OR", "AND", "NOT"} and t[0].isupper()]
        words = phrases + tokens
        if c.get("ceo"):
            words.append(c["ceo"].split(" (")[0])
        if words:
            out[c["name"]] = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b")
    return out


def tag_companies(text: str, kw: dict[str, re.Pattern]) -> list[str]:
    return [name for name, pat in kw.items() if pat.search(text)]


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
def google_news(cfg: dict, cutoff: datetime, hours: int) -> list[dict]:
    gn = cfg.get("google_news", {})
    if not gn.get("enabled", True):
        return []
    days = max(1, math.ceil(hours / 24))
    items = []
    for c in cfg["companies"]:
        q = f'{c["query"]} when:{days}d'
        url = (
            "https://news.google.com/rss/search?q=" + quote_plus(q)
            + f"&hl={gn.get('hl', 'en-US')}&gl={gn.get('gl', 'US')}&ceid={quote_plus(gn.get('ceid', 'US:en'))}"
        )
        try:
            feed = _get_feed(url)
        except Exception as e:  # noqa: BLE001
            log.warning("Google News failed for %s: %s", c["name"], e)
            continue
        n = 0
        for e in feed.entries:
            t = _entry_time(e)
            if t and t < cutoff:
                continue
            title = e.get("title", "")
            publisher = (e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else ""
            if publisher and title.endswith(" - " + publisher):
                title = title[: -(len(publisher) + 3)]
            items.append({
                "origin": "google_news",
                "publisher": publisher or "Google News",
                "title": title.strip(),
                "url": e.get("link", ""),
                "published": t.isoformat() if t else None,
                "summary": "",
                "companies": [c["name"]],
                "section": "tech",
            })
            n += 1
            if n >= gn.get("max_items_per_query", 6):
                break
        log.info("Google News %-40s %d items", c["name"], n)

    # top stories by topic (world, business, technology, science)
    for topic, section in (gn.get("topics") or {}).items():
        url = (
            f"https://news.google.com/rss/headlines/section/topic/{topic}"
            + f"?hl={gn.get('hl', 'en-US')}&gl={gn.get('gl', 'US')}&ceid={quote_plus(gn.get('ceid', 'US:en'))}"
        )
        try:
            feed = _get_feed(url)
        except Exception as e:  # noqa: BLE001
            log.warning("Google News topic %s failed: %s", topic, e)
            continue
        n = 0
        for e in feed.entries:
            t = _entry_time(e)
            if t and t < cutoff:
                continue
            title = e.get("title", "")
            publisher = (e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else ""
            if publisher and title.endswith(" - " + publisher):
                title = title[: -(len(publisher) + 3)]
            items.append({
                "origin": "google_news", "publisher": publisher or "Google News", "title": title.strip(),
                "url": e.get("link", ""), "published": t.isoformat() if t else None, "summary": "",
                "companies": [], "section": section,
            })
            n += 1
            if n >= gn.get("max_items_per_topic", 25):
                break
        log.info("Google News topic %-28s %d items", topic, n)
    return items


def rss_feeds(feeds: list[dict], origin: str, cutoff: datetime, kw: dict, require_match: bool,
              section: str = "tech", cap: int = 30) -> list[dict]:
    items = []
    for f in feeds:
        try:
            feed = _get_feed(f["url"])
        except Exception as e:  # noqa: BLE001
            log.warning("%s feed failed: %s", f["name"], e)
            continue
        n = 0
        for e in feed.entries:
            t = _entry_time(e)
            if t and t < cutoff:
                continue
            title = _clean(e.get("title", ""), 240)
            summary = _clean(e.get("summary", "") or e.get("description", ""))
            comps = tag_companies(f"{title} {summary}", kw)
            if require_match and not comps:
                continue
            items.append({
                "origin": origin,
                "publisher": f["name"],
                "title": title,
                "url": e.get("link", ""),
                "published": t.isoformat() if t else None,
                "summary": summary,
                "companies": comps,
                "section": f.get("section", section),
            })
            n += 1
            if n >= f.get("cap", cap):
                break
        log.info("%-12s %-22s %d items", origin, f["name"], n)
    return items


def x_posts(cfg: dict, cutoff: datetime) -> list[dict]:
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not token:
        log.info("X: X_BEARER_TOKEN not set — skipping X (optional)")
        return []
    xcfg = cfg.get("x", {})
    handles: list[tuple[str, str]] = []
    for c in cfg["companies"]:
        for h in c.get("x", []) or []:
            handles.append((h, c["name"]))
    for h in xcfg.get("extra_accounts", []) or []:
        handles.append((h, ""))
    if not handles:
        return []
    headers = {"Authorization": f"Bearer {token}", "User-Agent": UA}
    cache: dict = read_json(X_CACHE, {}) or {}
    missing = [h for h, _ in handles if h.lower() not in cache]
    # user lookup is billed per user, so resolve once and cache ids in the repo
    for i in range(0, len(missing), 100):
        batch = missing[i : i + 100]
        try:
            r = requests.get(
                "https://api.x.com/2/users/by",
                params={"usernames": ",".join(batch), "user.fields": "name"},
                headers=headers, timeout=TIMEOUT,
            )
            r.raise_for_status()
            for u in r.json().get("data", []):
                cache[u["username"].lower()] = {"id": u["id"], "name": u["name"], "username": u["username"]}
        except Exception as e:  # noqa: BLE001
            log.warning("X user lookup failed: %s", e)
    write_json(X_CACHE, cache)

    items = []
    start = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    for handle, company in handles:
        u = cache.get(handle.lower())
        if not u:
            continue
        try:
            r = requests.get(
                f"https://api.x.com/2/users/{u['id']}/tweets",
                params={
                    "max_results": max(5, min(100, int(xcfg.get("max_posts_per_account", 5)))),
                    "start_time": start,
                    "exclude": "retweets,replies",
                    "tweet.fields": "created_at,public_metrics,note_tweet",
                },
                headers=headers, timeout=TIMEOUT,
            )
            r.raise_for_status()
            posts = r.json().get("data", []) or []
        except Exception as e:  # noqa: BLE001
            log.warning("X timeline failed for @%s: %s", handle, e)
            continue
        for p in posts:
            text = (p.get("note_tweet") or {}).get("text") or p.get("text", "")
            m = p.get("public_metrics", {})
            items.append({
                "origin": "x",
                "publisher": f"X @{u['username']} ({u['name']})",
                "title": _clean(text, 280),
                "url": f"https://x.com/{u['username']}/status/{p['id']}",
                "published": p.get("created_at"),
                "summary": f"likes {m.get('like_count', 0)}, reposts {m.get('retweet_count', 0)}",
                "companies": [company] if company else [],
            })
        log.info("X @%-16s %d posts", handle, len(posts))
    return items


# ---------------------------------------------------------------------------
# dedupe & rank
# ---------------------------------------------------------------------------
_ORIGIN_RANK = {"official": 0, "x": 1, "media": 2, "world": 3, "google_news": 4}
_STOP = set("the and for with its new says said over after from into amid than that this will would could about more what why how are has have was were been their they them his her who not but all can may also just one two three year years week day today report reports".split())


def content_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and w not in _STOP}


def similarity(a: str, b: str) -> float:
    wa, wb = content_words(a), content_words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _norm_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2}


def dedupe(items: list[dict]) -> list[dict]:
    items = sorted(items, key=lambda i: (_ORIGIN_RANK.get(i["origin"], 9), -len(i.get("summary", ""))))
    kept: list[dict] = []
    seen_urls: set[str] = set()
    for it in items:
        if not it["title"] or it["url"] in seen_urls:
            continue
        w = _norm_words(it["title"])
        dup = None
        for k in kept:
            kw = _norm_words(k["title"])
            if w and kw and len(w & kw) / len(w | kw) >= 0.6:
                dup = k
                break
        if dup:
            dup["companies"] = sorted(set(dup["companies"]) | set(it["companies"]))
            dup.setdefault("also", []).append(it["publisher"])
            continue
        kept.append(it)
        seen_urls.add(it["url"])
    return kept


def collect(cfg: dict, now: datetime, hours: int, max_items: int = 280, history: dict | None = None) -> list[dict]:
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=hours)
    kw = company_keywords(cfg)
    items: list[dict] = []
    items += rss_feeds(cfg.get("official_feeds", []), "official", cutoff, kw, require_match=False, cap=10)
    items += rss_feeds(cfg.get("media_feeds", []), "media", cutoff, kw, require_match=False, cap=30)
    items += rss_feeds(cfg.get("world_feeds", []), "world", cutoff, kw, require_match=False, section="world", cap=25)
    items += x_posts(cfg, cutoff)
    items += google_news(cfg, cutoff, hours)
    for it in items:
        it.setdefault("section", "tech")
        if not it.get("companies"):
            it["companies"] = tag_companies(f"{it['title']} {it.get('summary', '')}", kw)
    # drop articles already used as sources in recent episodes
    if history:
        used_urls = history.get("urls", set())
        used_titles = history.get("titles", [])
        before = len(items)
        items = [
            i for i in items
            if i["url"] not in used_urls and not any(similarity(i["title"], t) >= 0.6 for t in used_titles)
        ]
        log.info("Dropped %d items already used in recent episodes", before - len(items))
    blocked = [b.lower() for b in cfg.get("blocked_publishers", []) or []]
    if blocked:
        before = len(items)
        items = [i for i in items if not any(b in i["publisher"].lower() for b in blocked)]
        log.info("Dropped %d items from blocked publishers", before - len(items))
    items = dedupe(items)
    # newest first, then (stable) group by origin rank, then cap
    items.sort(key=lambda i: i.get("published") or "", reverse=True)
    items.sort(key=lambda i: _ORIGIN_RANK.get(i["origin"], 9))
    if len(items) > max_items:
        # keep all official/x posts; split the rest so world news keeps roughly a third of the pool
        head = [i for i in items if i["origin"] in ("official", "x")]
        world = [i for i in items if i not in head and i["section"] == "world"]
        tech = [i for i in items if i not in head and i["section"] != "world"]
        room = max(0, max_items - len(head))
        n_world = min(len(world), max(room // 3, room - len(tech)))
        items = head + tech[: room - n_world] + world[:n_world]
    for n, it in enumerate(items, 1):
        it["id"] = n
    log.info(
        "Collected %d unique candidate items (tech %d, world %d)", len(items),
        sum(i["section"] != "world" for i in items), sum(i["section"] == "world" for i in items),
    )
    return items


def fetch_article_text(url: str, limit: int = 3500) -> str:
    """Best-effort full-text extraction for direct publisher links."""
    if not url or "news.google.com" in url or "x.com/" in url:
        return ""
    try:
        import trafilatura

        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return ""
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    except Exception as e:  # noqa: BLE001
        log.debug("article fetch failed %s: %s", url, e)
        return ""
