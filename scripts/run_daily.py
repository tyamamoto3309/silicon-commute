"""Daily pipeline: collect → select → gather facts → write script → TTS → MP3 → episode JSON.

Usage:
  python scripts/run_daily.py                 # normal run (needs GEMINI_API_KEY)
  python scripts/run_daily.py --force         # regenerate today's episode
  python scripts/run_daily.py --date 2026-09-28
  python scripts/run_daily.py --mock          # offline test: no API calls, fake audio
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, time

from common import EPISODES_DIR, JST, OUT_DIR, get_logger, load_config, now_jst, read_json, write_json

log = get_logger("daily")


def set_output(**kw) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kw.items():
                f.write(f"{k}={v}\n")


def recent_story_titles(limit_eps: int = 3) -> list[str]:
    titles = []
    for p in sorted(EPISODES_DIR.glob("*.json"), reverse=True)[:limit_eps]:
        ep = read_json(p, {}) or {}
        titles += [f"{ep.get('date')}: {s.get('title_en', '')}" for s in ep.get("stories", [])]
    return titles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="episode date (JST) YYYY-MM-DD; default = today in Japan")
    ap.add_argument("--force", action="store_true", help="overwrite an existing episode for the date")
    ap.add_argument("--mock", action="store_true", help="no network/API: fixture news + fake audio")
    ap.add_argument("--no-tts", action="store_true", help="skip audio generation")
    args = ap.parse_args()

    cfg = load_config()
    now = now_jst()
    if args.date:
        now = datetime.combine(datetime.fromisoformat(args.date).date(), time(6, 0), JST)
    date = now.strftime("%Y-%m-%d")
    ep_path = EPISODES_DIR / f"{date}.json"
    if ep_path.exists() and not args.force:
        log.info("Episode %s already exists — nothing to do (use --force to regenerate)", date)
        set_output(date=date, created="false", audio_ok="true")
        return 0

    ep_cfg = cfg["episode"]
    hours = int(ep_cfg["monday_lookback_hours"] if now.weekday() == 0 else ep_cfg["lookback_hours"])
    OUT_DIR.mkdir(exist_ok=True)

    # 1. collect -------------------------------------------------------------
    if args.mock:
        import mock

        items = mock.items()
    else:
        from collect import collect

        items = collect(cfg, now, hours)
    write_json(OUT_DIR / "items.json", items)
    if len(items) < 3:
        log.error("Only %d news items collected — aborting", len(items))
        return 2

    # 2. editorial -------------------------------------------------------------
    if args.mock:
        selection = mock.select(items)
        material, extra = mock.material(items, selection), []
        script = mock.script(cfg, items, selection, now)
    else:
        from editorial import gather_material, select_stories, write_script

        selection = select_stories(cfg, items, now, hours, recent_story_titles())
        if not selection["stories"]:
            log.error("Editor selected no stories — aborting")
            return 2
        material, extra = gather_material(cfg, items, selection, hours)
        write_json(OUT_DIR / "material.json", {"material": material})
        valid = {it["id"] for it in items} | {s["id"] for s in extra}
        script = write_script(cfg, material, now, valid)

    pool = {it["id"]: it for it in items + extra}
    for st in script.get("stories", []):
        st["sources"] = [
            {"publisher": pool[i]["publisher"], "title": pool[i]["title"], "url": pool[i]["url"]}
            for i in st.pop("source_ids", []) if i in pool
        ]

    # 3. audio ---------------------------------------------------------------
    audio = None
    duration = None
    if not args.no_tts:
        from tts import encode_mp3, media_duration, synthesize

        wav, mp3 = OUT_DIR / f"{date}.wav", OUT_DIR / f"{date}.mp3"
        try:
            synth_fn = mock.fake_tts if args.mock else None
            synthesize(cfg, script, wav, synth_fn=synth_fn)
            encode_mp3(wav, mp3, {
                "title": f"{now:%b %-d} · {script.get('title_en', '')}",
                "artist": cfg["podcast"]["title"],
                "album": cfg["podcast"]["title"],
                "date": date,
                "genre": "Podcast",
            })
            duration = round(media_duration(mp3), 1)
            audio = {"file": mp3.name, "bytes": mp3.stat().st_size, "release_tag": f"ep-{date}"}
            log.info("MP3 ready: %s (%.1f min, %.1f MB)", mp3.name, duration / 60, audio["bytes"] / 1e6)
        except Exception:  # noqa: BLE001
            log.error("Audio generation failed — publishing text only.\n%s", traceback.format_exc())

    # 4. save episode --------------------------------------------------------
    episode = {
        "date": date,
        "published": (now if not args.date else now.replace(hour=5, minute=0)).replace(microsecond=0).isoformat(),
        "title_en": script.get("title_en", ""),
        "title_ja": script.get("title_ja", ""),
        "summary_ja": script.get("summary_ja", ""),
        "duration": duration,
        "audio": audio,
        "word_count": script.get("word_count"),
        "hosts": [h["name"] for h in cfg["hosts"][:2]],
        "models": {"text": cfg["models"]["text"], "tts": cfg["models"]["tts"]} if not args.mock else {"mock": True},
        "segments": script["segments"],
        "stories": script.get("stories", []),
        "vocabulary": script.get("vocabulary", []),
        "phrase_of_the_day": script.get("phrase_of_the_day", {}),
    }
    write_json(ep_path, episode)
    log.info("Saved %s", ep_path.relative_to(ep_path.parent.parent))

    notes = [f"## {episode['title_en']}", f"**{episode['title_ja']}**", "", episode["summary_ja"], ""]
    notes += [f"- {s.get('title_ja', '')} / {s.get('title_en', '')}" for s in episode["stories"]]
    (OUT_DIR / "release_notes.md").write_text("\n".join(notes), encoding="utf-8")
    set_output(
        date=date, created="true", audio_ok=str(bool(audio)).lower(),
        tag=f"ep-{date}", mp3=str(OUT_DIR / f"{date}.mp3"), title=episode["title_en"].replace("\n", " "),
    )
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    sys.exit(main())
