"""Step 4 — build the static site (PWA + podcast RSS feed) into _site/ for GitHub Pages."""
from __future__ import annotations

import html
import shutil
import sys
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import requests

from common import EPISODES_DIR, OUT_DIR, SITE_OUT, SITE_SRC, get_logger, load_config, read_json, repo_slug, site_url, write_json

log = get_logger("site")


def load_episodes() -> list[dict]:
    eps = []
    for p in sorted(EPISODES_DIR.glob("*.json"), reverse=True):
        ep = read_json(p)
        if ep and ep.get("date"):
            eps.append(ep)
    return eps


def _fmt_vtt_time(t: float) -> str:
    h, rem = divmod(max(t, 0.0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def make_vtt(ep: dict) -> str:
    lines = [l for seg in ep["segments"] for l in seg["lines"] if "t" in l]
    out = ["WEBVTT", ""]
    for i, l in enumerate(lines):
        end = lines[i + 1]["t"] if i + 1 < len(lines) else ep.get("duration") or l["t"] + 5
        text = l["ja"] if l.get("lang") == "ja" else l["en"]
        out += [f"{_fmt_vtt_time(l['t'])} --> {_fmt_vtt_time(end)}", f"<v {l['speaker']}>{text}", ""]
    return "\n".join(out)


def ensure_audio(ep: dict, dest_dir: Path) -> Path | None:
    audio = ep.get("audio")
    if not audio:
        return None
    dest = dest_dir / audio["file"]
    if dest.exists():
        return dest
    local = OUT_DIR / audio["file"]
    if local.exists():
        shutil.copy2(local, dest)
        return dest
    repo = repo_slug()
    if repo and audio.get("release_tag"):
        url = f"https://github.com/{repo}/releases/download/{audio['release_tag']}/{audio['file']}"
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)
            return dest
        except Exception as e:  # noqa: BLE001
            log.warning("could not fetch %s: %s", url, e)
    return None


def show_notes_html(ep: dict, base: str) -> str:
    h = [f"<p>{html.escape(ep.get('summary_ja', ''))}</p>", "<ol>"]
    for st in ep.get("stories", []):
        tag = "🌏 " if st.get("section") == "world" else "💻 "
        h.append(
            f"<li><b>{tag}{html.escape(st.get('title_ja', ''))}</b><br/>{html.escape(st.get('title_en', ''))}"
            f"<br/>{html.escape(st.get('summary_ja', ''))}</li>"
        )
    h.append("</ol>")
    p = ep.get("phrase_of_the_day") or {}
    if p.get("phrase"):
        h.append(f"<p>📘 Phrase of the day: <b>{html.escape(p['phrase'])}</b> — {html.escape(p.get('meaning_ja', ''))}</p>")
    h.append(f'<p>英日対訳スクリプト・単語リスト: <a href="{base}#/ep/{ep["date"]}">{base}#/ep/{ep["date"]}</a></p>')
    return "".join(h).replace("]]>", "]]&gt;")


def make_feed(cfg: dict, eps: list[dict], base: str, audio_info: dict[str, dict]) -> str:
    pc = cfg["podcast"]
    cover = base + "icons/cover.png"
    now = format_datetime(datetime.now().astimezone())
    items = []
    for ep in eps:
        a = audio_info.get(ep["date"])
        if not a:
            continue
        pub = datetime.fromisoformat(ep["published"])
        notes = show_notes_html(ep, base)
        items.append(
            f"""    <item>
      <title>{escape(pub.strftime('%b %-d') + ' · ' + ep.get('title_en', ''))}</title>
      <link>{escape(base + '#/ep/' + ep['date'])}</link>
      <guid isPermaLink="false">silicon-commute-{ep['date']}</guid>
      <pubDate>{format_datetime(pub)}</pubDate>
      <description><![CDATA[{notes}]]></description>
      <content:encoded><![CDATA[{notes}]]></content:encoded>
      <enclosure url={quoteattr(a['url'])} length="{a['bytes']}" type="audio/mpeg"/>
      <itunes:duration>{int(ep.get('duration') or 0)}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
      <itunes:explicit>false</itunes:explicit>
      <podcast:transcript url={quoteattr(base + 'vtt/' + ep['date'] + '.vtt')} type="text/vtt" language="en"/>
    </item>"""
        )
    block = "\n    <itunes:block>Yes</itunes:block>" if pc.get("private_feed", True) else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:podcast="https://podcastindex.org/namespace/1.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(pc['title'])}</title>
    <link>{escape(base)}</link>
    <atom:link href={quoteattr(base + 'feed.xml')} rel="self" type="application/rss+xml"/>
    <language>{escape(pc.get('language', 'en'))}</language>
    <description>{escape(pc['description'])}</description>
    <itunes:subtitle>{escape(pc.get('subtitle', ''))}</itunes:subtitle>
    <itunes:summary>{escape(pc['description'])}</itunes:summary>
    <itunes:author>{escape(pc.get('author', pc['title']))}</itunes:author>
    <itunes:image href={quoteattr(cover)}/>
    <image><url>{escape(cover)}</url><title>{escape(pc['title'])}</title><link>{escape(base)}</link></image>
    <itunes:category text="News"><itunes:category text="Tech News"/></itunes:category>
    <itunes:category text="Technology"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:type>episodic</itunes:type>{block}
    <lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""


def build() -> None:
    cfg = load_config()
    base = site_url(cfg)
    keep = int(cfg["podcast"].get("keep_on_site", 30))
    if SITE_OUT.exists():
        shutil.rmtree(SITE_OUT)
    shutil.copytree(SITE_SRC, SITE_OUT)
    (SITE_OUT / ".nojekyll").write_text("")
    for d in ("audio", "vtt", "data/episodes"):
        (SITE_OUT / d).mkdir(parents=True, exist_ok=True)

    eps = load_episodes()
    audio_info: dict[str, dict] = {}
    index = []
    for n, ep in enumerate(eps):
        audio_url = None
        if n < keep:
            f = ensure_audio(ep, SITE_OUT / "audio")
            if f:
                audio_url = base + "audio/" + f.name
                audio_info[ep["date"]] = {"url": audio_url, "bytes": f.stat().st_size, "rel": "audio/" + f.name}
        if not audio_url and ep.get("audio") and repo_slug():
            a = ep["audio"]
            audio_url = f"https://github.com/{repo_slug()}/releases/download/{a['release_tag']}/{a['file']}"
        if any("t" in l for s in ep["segments"] for l in s["lines"]):
            (SITE_OUT / "vtt" / f"{ep['date']}.vtt").write_text(make_vtt(ep), encoding="utf-8")
        page = dict(ep)
        page["audio_url"] = audio_info.get(ep["date"], {}).get("rel") or audio_url
        write_json(SITE_OUT / "data" / "episodes" / f"{ep['date']}.json", page)
        index.append({
            "date": ep["date"],
            "published": ep.get("published"),
            "title_en": ep.get("title_en", ""),
            "title_ja": ep.get("title_ja", ""),
            "summary_ja": ep.get("summary_ja", ""),
            "duration": ep.get("duration"),
            "audio_url": page["audio_url"],
            "stories": [{"title_en": s.get("title_en", ""), "title_ja": s.get("title_ja", ""), "section": s.get("section", "tech")} for s in ep.get("stories", [])],
        })

    write_json(SITE_OUT / "data" / "index.json", {
        "podcast": {k: cfg["podcast"][k] for k in ("title", "title_ja", "subtitle")},
        "feed_url": base + "feed.xml",
        "hosts": [{"name": h["name"]} for h in cfg["hosts"][:2]],
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "episodes": index,
    })
    (SITE_OUT / "feed.xml").write_text(make_feed(cfg, eps, base, audio_info), encoding="utf-8")

    sw = SITE_OUT / "sw.js"
    sw.write_text(sw.read_text(encoding="utf-8").replace("__BUILD__", datetime.now().strftime("%Y%m%d%H%M%S")), encoding="utf-8")
    log.info("Site built: %d episodes (%d with audio) → %s  [%s]", len(eps), len(audio_info), SITE_OUT, base)


if __name__ == "__main__":
    sys.exit(build())
