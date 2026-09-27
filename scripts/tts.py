"""Step 3 — text-to-speech with Gemini multi-speaker TTS, then MP3 via ffmpeg.

Also estimates a start time for every transcript line so the app can
highlight the sentence being spoken and let you tap a line to jump there.
"""
from __future__ import annotations

import io
import re
import subprocess
import time
import wave
from pathlib import Path

from common import call_with_fallback, gemini_client, get_logger, word_count

log = get_logger("tts")
PAUSE_BETWEEN_CHUNKS = 0.45  # seconds


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------
def make_chunks(script: dict, max_words: int) -> list[list[tuple[int, int]]]:
    """Group (segment_idx, line_idx) pairs into chunks of <= max_words, never splitting a line."""
    chunks, cur, cur_words = [], [], 0
    for si, seg in enumerate(script["segments"]):
        for li, line in enumerate(seg["lines"]):
            w = word_count(line["en"])
            # prefer to start a new chunk at a segment boundary once reasonably full
            boundary = li == 0 and cur_words > max_words * 0.6
            if cur and (cur_words + w > max_words or boundary):
                chunks.append(cur)
                cur, cur_words = [], 0
            cur.append((si, li))
            cur_words += w
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------
def _speech_config(cfg: dict):
    from google.genai import types

    return types.SpeechConfig(
        multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
            speaker_voice_configs=[
                types.SpeakerVoiceConfig(
                    speaker=h["name"],
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=h["voice"])
                    ),
                )
                for h in cfg["hosts"][:2]
            ]
        )
    )


def _director_note(cfg: dict) -> str:
    a, b = cfg["hosts"][0], cfg["hosts"][1]
    return (
        "Read this morning tech-news podcast conversation aloud. "
        "Pace: moderate, slightly slower than a native news broadcast, with clear articulation for English learners, "
        "and natural short pauses between speakers. "
        f"{a['name']} sounds {a.get('style', 'calm')}; {b['name']} sounds {b.get('style', 'warm')}."
    )


def _pcm_from_part(part) -> tuple[bytes, int]:
    data = part.inline_data.data
    mime = (part.inline_data.mime_type or "").lower()
    if isinstance(data, str):
        import base64

        data = base64.b64decode(data)
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise ValueError(f"unexpected WAV format: {w.getsampwidth()*8}bit x{w.getnchannels()}")
            return w.readframes(w.getnframes()), w.getframerate()
    m = re.search(r"rate=(\d+)", mime)
    return data, int(m.group(1)) if m else 24000


PACE = "speaking at a moderate, slightly slow pace with clear articulation for English learners"
_MODE: dict[str, str] = {}  # model -> request format that worked ("parts" or "classic")


def synth_chunk(cfg: dict, lines: list[dict]) -> tuple[bytes, int]:
    from google.genai import types

    client = gemini_client()
    names = {h["name"] for h in cfg["hosts"][:2]}
    styles = {h["name"]: f"{h.get('style', '')}, {PACE}".strip(", ") for h in cfg["hosts"][:2]}
    transcript = "\n".join(f"{l['speaker']}: {l['en']}" for l in lines if l["speaker"] in names)
    models = [cfg["models"]["tts"], *cfg["models"].get("tts_fallbacks", [])]

    def request(model: str, mode: str, conf):
        if mode == "parts":
            # current format: one part per line with speech_metadata (speaker + style)
            parts = [
                types.Part(text=l["en"], speech_metadata=types.SpeechMetadata(speaker=l["speaker"], style=styles.get(l["speaker"]) or None))
                for l in lines
            ]
            return client.models.generate_content(model=model, contents=[types.Content(role="user", parts=parts)], config=conf)
        # classic format: director note + "Speaker: text" transcript (older TTS models)
        return client.models.generate_content(model=model, contents=f"{_director_note(cfg)}\n\n{transcript}", config=conf)

    def run(model: str):
        conf = types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=_speech_config(cfg))
        order = [_MODE[model]] if model in _MODE else (["classic", "parts"] if "2.5" in model else ["parts", "classic"])
        last = None
        for mode in order:
            try:
                resp = request(model, mode, conf)
                _MODE[model] = mode
                break
            except Exception as e:  # noqa: BLE001
                last = e
                if "400" not in str(e) and "INVALID_ARGUMENT" not in str(e):
                    raise
        else:
            raise last
        for part in resp.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                return _pcm_from_part(part)
        raise RuntimeError("TTS response contained no audio")

    (pcm, rate), model = call_with_fallback(models, run, label="tts", attempts_per_model=5)
    return pcm, rate


# ---------------------------------------------------------------------------
# full episode
# ---------------------------------------------------------------------------
def _assign_times(script: dict, chunk: list[tuple[int, int]], start: float, dur: float) -> None:
    weights = [len(script["segments"][si]["lines"][li]["en"]) + 25 for si, li in chunk]
    total = sum(weights) or 1
    t = start
    for (si, li), w in zip(chunk, weights):
        script["segments"][si]["lines"][li]["t"] = round(t, 2)
        t += dur * w / total


def synthesize(cfg: dict, script: dict, out_wav: Path, synth_fn=None) -> float:
    """Render all lines to one WAV; sets line/segment start times; returns duration in seconds."""
    synth_fn_is_real = synth_fn is None
    synth_fn = synth_fn or (lambda lines: synth_chunk(cfg, lines))
    chunks = make_chunks(script, int(cfg["episode"].get("tts_chunk_words", 380)))
    log.info("TTS: %d chunks", len(chunks))
    pcm_all = bytearray()
    rate_all = None
    t = 0.0
    min_gap = float(cfg["episode"].get("tts_min_interval_sec", 20))
    last_start = 0.0
    for n, chunk in enumerate(chunks, 1):
        lines = [script["segments"][si]["lines"][li] for si, li in chunk]
        # pace requests to stay under the free tier's requests-per-minute limit
        if synth_fn_is_real and last_start and time.time() - last_start < min_gap:
            time.sleep(min_gap - (time.time() - last_start))
        last_start = time.time()
        pcm, rate = synth_fn(lines)
        if rate_all is None:
            rate_all = rate
        elif rate != rate_all:
            raise RuntimeError(f"sample rate changed between chunks ({rate_all} → {rate})")
        dur = len(pcm) / 2 / rate
        _assign_times(script, chunk, t, dur)
        pcm_all += pcm
        t += dur
        if n < len(chunks):
            gap = int(PAUSE_BETWEEN_CHUNKS * rate) * 2
            pcm_all += b"\x00" * gap
            t += PAUSE_BETWEEN_CHUNKS
        log.info("TTS chunk %d/%d: %.1fs", n, len(chunks), dur)
    for seg in script["segments"]:
        seg["t"] = seg["lines"][0].get("t", 0.0)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate_all or 24000)
        w.writeframes(bytes(pcm_all))
    return t


def encode_mp3(wav: Path, mp3: Path, meta: dict) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ac", "1", "-ar", "44100", "-codec:a", "libmp3lame", "-b:a", "64k",
        "-id3v2_version", "3",
    ]
    for k, v in meta.items():
        cmd += ["-metadata", f"{k}={v}"]
    cmd.append(str(mp3))
    subprocess.run(cmd, check=True)


def media_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())
