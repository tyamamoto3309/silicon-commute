"""Daily pipeline: collect → select → gather facts → write script → TTS → MP3 → episode JSON.

Usage:
  python scripts/run_daily.py                 # normal run (needs GEMINI_API_KEY)
  python scripts/run_daily.py --force         # regenerate today's episode
  python scripts/run_daily.py --date 2026-09-28
  python scripts/run_daily.py --mock          # offline test: no API calls, fake audio
  python scripts/run_daily.py --audio-only    # add audio to recent episodes that were published text-only

If today's episode already exists, a normal run adds audio to (at most) one recent
episode that is still missing it, so a failed TTS day heals itself on a later run.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime, time, timedelta

from common import EPISODES_DIR, JST, OUT_DIR, get_logger, load_config, now_jst, read_json, write_json

log = get_logger("daily")


def set_output(**kw) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kw.items():
                f.write(f"{k}={v}\n")


def recent_history(today: str, days: int) -> tuple[list[dict], dict]:
    """Stories and source articles from episodes in the last `days` days (to avoid repeats)."""
    start = (datetime.fromisoformat(today) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent: list[dict] = []
    urls: set[str] = set()
    titles: list[str] = []
    for p in sorted(EPISODES_DIR.glob("*.json"), reverse=True):
        d = p.stem
        if d >= today or d < start:
            continue
        ep = read_json(p, {}) or {}
        for s in ep.get("stories", []):
            recent.append({"date": d, "title": s.get("title_en", ""), "topic_key": s.get("topic_key", ""), "section": s.get("section", "tech")})
            for src in s.get("sources", []):
                urls.add(src.get("url", ""))
                titles.append(src.get("title", ""))
    return recent, {"urls": urls, "titles": titles}


def lookback_hours(cfg: dict, now: datetime, today: str) -> int:
    """Cover everything since the previous episode was generated (+2h overlap), within sensible bounds."""
    ep_cfg = cfg["episode"]
    default = int(ep_cfg["monday_lookback_hours"] if now.weekday() == 0 else ep_cfg["lookback_hours"])
    for p in sorted(EPISODES_DIR.glob("*.json"), reverse=True):
        if p.stem >= today:
            continue
        prev = (read_json(p, {}) or {}).get("published")
        if not prev:
            break
        gap = (now - datetime.fromisoformat(prev)).total_seconds() / 3600
        return int(min(max(gap + 2, 24), 84))
    return default


def models_used(cfg: dict) -> dict:
    out = {}
    try:
        from editorial import USED_MODELS

        out.update(USED_MODELS)
    except Exception:  # noqa: BLE001
        pass
    try:
        from tts import USED_TTS_MODELS

        out["tts"] = ", ".join(sorted(USED_TTS_MODELS)) or cfg["models"]["tts"]
    except Exception:  # noqa: BLE001
        out["tts"] = cfg["models"]["tts"]
    return out


def make_audio(cfg: dict, script: dict, date: str, now: datetime, mock_tts: bool) -> tuple[dict | None, float | None]:
    """TTS → WAV → MP3. Sets line/segment start times in `script`. Returns (audio, duration), or (None, None) on failure."""
    from tts import encode_mp3, media_duration, synthesize

    wav, mp3 = OUT_DIR / f"{date}.wav", OUT_DIR / f"{date}.mp3"
    try:
        synth_fn = None
        if mock_tts:
            import mock

            synth_fn = mock.fake_tts
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
        return audio, duration
    except Exception:  # noqa: BLE001
        log.error("Audio generation failed for %s.\n%s", date, traceback.format_exc())
        return None, None


def write_release_notes(episode: dict) -> Path:
    lines = [f"## {episode['title_en']}", f"**{episode['title_ja']}**", "", episode.get("summary_ja", ""), ""]
    lines += [f"- {s.get('title_ja', '')} / {s.get('title_en', '')}" for s in episode.get("stories", [])]
    text = "\n".join(lines)
    path = OUT_DIR / f"release_notes_{episode['date']}.md"
    path.write_text(text, encoding="utf-8")
    (OUT_DIR / "release_notes.md").write_text(text, encoding="utf-8")  # name used by the older workflow file
    return path


def missing_audio(today: str, days: int) -> list[Path]:
    """Episodes from the last `days` days (today included) that were published without audio, newest first."""
    start = (datetime.fromisoformat(today) - timedelta(days=days)).strftime("%Y-%m-%d")
    return [
        p for p in sorted(EPISODES_DIR.glob("*.json"), reverse=True)
        if start <= p.stem <= today and not (read_json(p, {}) or {}).get("audio")
    ]


def backfill_audio(cfg: dict, today: str, limit: int, mock_tts: bool) -> list[dict]:
    """Add audio to up to `limit` recent text-only episodes from their saved scripts. Stops at the first failure."""
    days = int(cfg["episode"].get("audio_retry_days", 14))
    todo = missing_audio(today, days)
    if not todo:
        log.info("Every episode from the last %d days has audio", days)
        return []
    log.info("Episodes without audio: %s (this run: up to %d)", ", ".join(p.stem for p in todo), limit)
    done: list[dict] = []
    for p in todo[:limit]:
        ep = read_json(p, {})
        date = ep["date"]
        log.info("Adding audio to %s from its saved script", date)
        script = {"title_en": ep.get("title_en", ""), "segments": ep["segments"]}
        now = datetime.combine(datetime.fromisoformat(date).date(), time(6, 0), JST)
        audio, duration = make_audio(cfg, script, date, now, mock_tts)
        if not audio:
            log.warning("Still no audio for %s — a later run will try again", date)
            break  # most likely quota or an outage; don't burn more requests now
        ep["segments"] = script["segments"]  # now with line/segment start times for the player
        ep["audio"], ep["duration"] = audio, duration
        if not mock_tts:
            ep.setdefault("models", {})["tts"] = models_used(cfg)["tts"]
        write_json(p, ep)
        done.append({"date": date, "tag": audio["release_tag"], "mp3": str(OUT_DIR / audio["file"]), "notes": str(write_release_notes(ep))})
    return done


def report(*, created: dict | None, uploads: list[dict], commit_msg: str, date: str, audio_ok: bool = True) -> None:
    """GitHub Actions step outputs, plus out/uploads.tsv: one MP3 per line to store in GitHub Releases."""
    OUT_DIR.mkdir(exist_ok=True)
    with open(OUT_DIR / "uploads.tsv", "w", encoding="utf-8") as f:
        for u in uploads:
            f.write(f"{u['tag']}\t{u['mp3']}\t{u['date']}\t{u['notes']}\n")
    first = uploads[0] if uploads else {}
    set_output(
        date=date,
        created=str(created is not None).lower(),
        audio_ok=str(audio_ok).lower(),
        changed=str(created is not None or bool(uploads)).lower(),
        uploads=str(len(uploads)),
        commit_msg=commit_msg,
        tag=first.get("tag", f"ep-{date}"),
        mp3=first.get("mp3", str(OUT_DIR / f"{date}.mp3")),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="episode date (JST) YYYY-MM-DD; default = today in Japan")
    ap.add_argument("--force", action="store_true", help="overwrite an existing episode for the date")
    ap.add_argument("--mock", action="store_true", help="no network/API: fixture news + fake audio")
    ap.add_argument("--no-tts", action="store_true", help="skip audio generation")
    ap.add_argument("--audio-only", action="store_true", help="only add audio to recent text-only episodes; never create a new one")
    args = ap.parse_args()

    cfg = load_config()
    now = now_jst()
    if args.date:
        now = datetime.combine(datetime.fromisoformat(args.date).date(), time(6, 0), JST)
    date = now.strftime("%Y-%m-%d")
    ep_path = EPISODES_DIR / f"{date}.json"
    if args.audio_only or (ep_path.exists() and not args.force):
        if not args.audio_only:
            log.info("Episode %s already exists (use --force to regenerate)", date)
        per_run = int(cfg["episode"].get("audio_backfill_per_run", 5)) if args.audio_only else 1
        uploads = [] if args.no_tts else backfill_audio(cfg, date, limit=per_run, mock_tts=args.mock)
        if not uploads:
            log.info("No audio added this run")
        report(
            created=None, uploads=uploads, date=uploads[0]["date"] if uploads else date,
            commit_msg="Add audio: " + ", ".join(u["date"] for u in uploads) if uploads else "",
        )
        return 0

    ep_cfg = cfg["episode"]
    hours = lookback_hours(cfg, now, date)
    OUT_DIR.mkdir(exist_ok=True)

    # 1. collect -------------------------------------------------------------
    if args.mock:
        import mock

        items = mock.items()
    else:
        from collect import collect

        recent, history = recent_history(date, int(ep_cfg.get("avoid_repeat_days", 7)))
        items = collect(cfg, now, hours, history=history)
    write_json(OUT_DIR / "items.json", items)
    if len(items) < 3:
        log.error("Only %d news items collected — aborting", len(items))
        return 2

    # 2. editorial -------------------------------------------------------------
    if args.mock:
        selection = mock.select(items)
        material, extra = mock.material(items, selection), []
        from editorial import finalize

        script = finalize(cfg, mock.script(cfg, items, selection, now), {it["id"] for it in items})
    else:
        from editorial import gather_material, select_stories, write_script

        selection = select_stories(cfg, items, now, hours, recent)
        if not selection["stories"]:
            log.error("Editor selected no stories — aborting")
            return 2
        material, extra = gather_material(cfg, items, selection, hours)
        write_json(OUT_DIR / "material.json", {"material": material})
        valid = {it["id"] for it in items} | {s["id"] for s in extra}
        script = write_script(cfg, material, now, valid, selection)

    pool = {it["id"]: it for it in items + extra}
    for st in script.get("stories", []):
        st["sources"] = [
            {"publisher": pool[i]["publisher"], "title": pool[i]["title"], "url": pool[i]["url"]}
            for i in st.pop("source_ids", []) if i in pool
        ]

    # 3. audio ---------------------------------------------------------------
    audio = duration = None
    if not args.no_tts:
        audio, duration = make_audio(cfg, script, date, now, mock_tts=args.mock)
        if not audio:
            log.error("Publishing text only — a later run will try to add the audio again.")

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
        "ja_chars": script.get("ja_chars"),
        "hosts": [h["name"] for h in cfg["hosts"][:2]],
        "models": models_used(cfg) if not args.mock else {"mock": True},
        "segments": script["segments"],
        "stories": script.get("stories", []),
        "vocabulary": script.get("vocabulary", []),
        "phrase_of_the_day": script.get("phrase_of_the_day", {}),
    }
    write_json(ep_path, episode)
    log.info("Saved %s", ep_path.relative_to(ep_path.parent.parent))

    notes_path = write_release_notes(episode)
    uploads = [{"date": date, "tag": audio["release_tag"], "mp3": str(OUT_DIR / audio["file"]), "notes": str(notes_path)}] if audio else []
    report(created=episode, uploads=uploads, commit_msg=f"Episode {date}", date=date, audio_ok=bool(audio))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(__file__))
    sys.exit(main())
