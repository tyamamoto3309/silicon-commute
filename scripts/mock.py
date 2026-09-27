"""Offline test doubles (python scripts/run_daily.py --mock). No network, no API key."""
from __future__ import annotations

import math
import struct

from common import ROOT, read_json, word_count

FIX = ROOT / "tests" / "fixtures"


def items() -> list[dict]:
    return read_json(FIX / "items.json", [])


def select(items_: list[dict]) -> dict:
    return {"stories": [{"headline": i["title"], "item_ids": [i["id"]], "why": ""} for i in items_[:5]], "ceo_watch_item_ids": []}


def material(items_: list[dict], selection: dict) -> str:
    return "\n".join(s["headline"] for s in selection["stories"])


def script(cfg: dict, items_: list[dict], selection: dict, now) -> dict:
    sample = read_json(FIX / "sample_script.json")
    if sample:
        sample["word_count"] = sum(word_count(l["en"]) for s in sample["segments"] for l in s["lines"])
        return sample
    raise FileNotFoundError("tests/fixtures/sample_script.json missing")


def fake_tts(lines: list[dict], rate: int = 24000) -> tuple[bytes, int]:
    """Quiet tones whose length matches ~140 wpm speech, so timings behave realistically."""
    out = bytearray()
    for l in lines:
        secs = max(1.0, word_count(l["en"]) / 140 * 60)
        freq = 220 if l["speaker"] == "Alex" else 330
        n = int(secs * rate)
        for i in range(n):
            env = min(1.0, i / 800, (n - i) / 800)
            out += struct.pack("<h", int(1800 * env * math.sin(2 * math.pi * freq * i / rate)))
    return bytes(out), rate
