"""Offline tests for the real (non-mock) code paths, with network and Gemini faked.

Run: python tests/test_offline.py
"""
import io
import json
import struct
import sys
import types as pytypes
import wave
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import collect  # noqa: E402
import common  # noqa: E402
import editorial  # noqa: E402
import tts  # noqa: E402
from google.genai import types  # noqa: E402

cfg = common.load_config()
NOW = datetime(2026, 9, 28, 4, 45, tzinfo=common.JST)

# ---------------------------------------------------------------------------
# 1. collection: feed parsing, keyword tagging, dedupe
# ---------------------------------------------------------------------------
GN = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Nvidia shares slip after AI slowdown debate - Reuters</title><link>https://news.google.com/rss/articles/abc</link>
<pubDate>Sun, 27 Sep 2026 10:00:00 GMT</pubDate><source url="https://www.reuters.com">Reuters</source></item>
<item><title>Old Nvidia story - CNBC</title><link>https://news.google.com/rss/articles/old</link>
<pubDate>Mon, 01 Sep 2026 10:00:00 GMT</pubDate><source url="https://www.cnbc.com">CNBC</source></item>
</channel></rss>"""
MEDIA = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Nvidia shares slip after AI slowdown debate</title><link>https://techcrunch.com/a</link>
<pubDate>Sun, 27 Sep 2026 09:00:00 GMT</pubDate><description>&lt;p&gt;Nvidia fell as investors digested calls to slow AI.&lt;/p&gt;</description></item>
<item><title>Best robot vacuums of 2026</title><link>https://techcrunch.com/b</link>
<pubDate>Sun, 27 Sep 2026 09:00:00 GMT</pubDate><description>Shopping guide.</description></item>
<item><title>Arm Holdings unveils new server core</title><link>https://techcrunch.com/c</link>
<pubDate>Sun, 27 Sep 2026 08:00:00 GMT</pubDate><description>Rene Haas said demand is strong.</description></item>
</channel></rss>"""


class FakeResp:
    def __init__(self, body, status=200, ctype="application/rss+xml"):
        self.content = body.encode()
        self.text = body
        self.status_code = status
        self.headers = {"content-type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return json.loads(self.text)


def fake_get(url, **kw):
    if "news.google.com" in url:
        return FakeResp(GN if "Nvidia" in url else "<rss><channel></channel></rss>")
    if "techcrunch.com/feed" in url:
        return FakeResp(MEDIA)
    if url.startswith("https://techcrunch.com/"):
        return FakeResp("<html><body><article><p>" + "Nvidia details. " * 50 + "</p></article></body></html>", ctype="text/html")
    return FakeResp("<rss><channel></channel></rss>")


collect.requests.get = fake_get
items = collect.collect(cfg, NOW, 30)
titles = [i["title"] for i in items]
assert "Best robot vacuums of 2026" not in titles, "unrelated media item must be filtered"
assert "Old Nvidia story" not in titles, "old item must be dropped by cutoff"
assert any("Arm Holdings" in t for t in titles), "Arm item should match keyword"
nv = [i for i in items if "AI slowdown" in i["title"]]
assert len(nv) == 1 and nv[0]["origin"] == "media" and "Reuters" in nv[0].get("also", []), nv
assert [i["id"] for i in items] == list(range(1, len(items) + 1))
kw = collect.company_keywords(cfg)
assert collect.tag_companies("Rapidus starts 2nm pilot line", kw) == ["Japan chips (Rapidus / Tokyo Electron)"]
assert "Arm" not in " ".join(collect.tag_companies("an arm of the company", kw)), "lowercase 'arm' must not match"
print("✓ collect:", len(items), "items", titles)

# ---------------------------------------------------------------------------
# 2. editorial with a fake Gemini client
# ---------------------------------------------------------------------------
sample = json.loads((ROOT / "tests/fixtures/sample_script.json").read_text())
calls = []


class FakeModels:
    def generate_content(self, model, contents, config=None):
        calls.append((model, config))
        # validate the config object serialises like the SDK will send it
        if config is not None:
            config.model_dump(exclude_none=True)
        if config is not None and config.tools:
            raise RuntimeError("400 INVALID_ARGUMENT: Search grounding is not available on the free tier")
        text = contents if isinstance(contents, str) else ""
        if "news editor" in text:
            ids = [it["id"] for it in items]
            out = {"stories": [{"headline": "Nvidia slips", "item_ids": ids[:1] + [999], "why": "x"}], "ceo_watch_item_ids": [ids[-1], 12345]}
        else:
            out = dict(sample)
            out = json.loads(json.dumps(out))
            out["stories"][0]["source_ids"] = [1, 999]
            out["segments"][0]["lines"][0]["speaker"] = "ALEX (host)"  # should be normalised
        return pytypes.SimpleNamespace(text="```json\n" + json.dumps(out) + "\n```")


class FakeClient:
    models = FakeModels()


common._client = FakeClient()
sel = editorial.select_stories(cfg, items, NOW, 30, ["2026-09-25: old story"])
assert sel["stories"][0]["item_ids"] == [items[0]["id"]], "invalid ids must be dropped"
assert 12345 not in sel["ceo_watch_item_ids"]
material, extra = editorial.gather_material(cfg, items, sel, 30)
assert "STORY 1" in material and extra == []
assert calls[-1][1].tools, "grounding should have been attempted"
script = editorial.write_script(cfg, material, NOW, {it["id"] for it in items})
assert script["segments"][0]["lines"][0]["speaker"] in ("Alex", "Mika")
assert script["stories"][0]["source_ids"] == [1]
assert calls[-1][1].response_json_schema is not None
print("✓ editorial: select → material (%d chars) → script (%d words, %d model calls)" % (len(material), script["word_count"], len(calls)))

# ---------------------------------------------------------------------------
# 3. TTS: request building, WAV/PCM parsing, fallback to per-line parts
# ---------------------------------------------------------------------------
def pcm(seconds, rate=24000):
    return b"".join(struct.pack("<h", 0) for _ in range(int(seconds * rate)))


def wav_bytes(seconds, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm(seconds, rate))
    return buf.getvalue()


mode = {"n": 0}


class FakeTTSModels:
    def generate_content(self, model, contents, config=None):
        config.model_dump(exclude_none=True)
        sc = config.speech_config.multi_speaker_voice_config.speaker_voice_configs
        assert [s.speaker for s in sc] == ["Alex", "Mika"]
        assert sc[0].voice_config.prebuilt_voice_config.voice_name == "Charon"
        mode["n"] += 1
        if isinstance(contents, str):
            assert "Alex:" in contents and "Mika:" in contents or "Alex:" in contents or "Mika:" in contents
            if mode["n"] == 1:  # simulate the classic format being rejected once
                raise RuntimeError("400 INVALID_ARGUMENT: use speech_metadata")
            data, mime = wav_bytes(2.0), "audio/wav"
        else:
            parts = contents[0].parts
            assert parts[0].speech_metadata.speaker in ("Alex", "Mika")
            data, mime = pcm(3.0), "audio/L16;codec=pcm;rate=24000"
        part = types.Part(inline_data=types.Blob(data=data, mime_type=mime))
        return pytypes.SimpleNamespace(candidates=[pytypes.SimpleNamespace(content=pytypes.SimpleNamespace(parts=[part]))])


class FakeTTSClient:
    models = FakeTTSModels()


common._client = FakeTTSClient()
tts.gemini_client = lambda: FakeTTSClient()
out_wav = ROOT / "out" / "test_tts.wav"
dur = tts.synthesize(cfg, sample, out_wav)
chunks = tts.make_chunks(sample, cfg["episode"]["tts_chunk_words"])
assert all(sum(common.word_count(sample["segments"][s]["lines"][l]["en"]) for s, l in c) <= cfg["episode"]["tts_chunk_words"] for c in chunks)
ts = [l["t"] for s in sample["segments"] for l in s["lines"]]
assert ts == sorted(ts) and ts[0] == 0.0
expected = 3.0 + 2.0 * (len(chunks) - 1) + tts.PAUSE_BETWEEN_CHUNKS * (len(chunks) - 1)
assert abs(dur - expected) < 0.01, (dur, expected)
with wave.open(str(out_wav)) as w:
    assert abs(w.getnframes() / w.getframerate() - dur) < 0.01
out_wav.unlink()
print("✓ tts: %d chunks, %.1fs, fallback to speech_metadata parts worked" % (len(chunks), dur))
print("\nALL OFFLINE TESTS PASSED")
