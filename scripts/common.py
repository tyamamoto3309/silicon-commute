"""Shared helpers: config, paths, dates, logging, Gemini client with retries."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EPISODES_DIR = ROOT / "episodes"
OUT_DIR = ROOT / "out"
SITE_SRC = ROOT / "site"
SITE_OUT = ROOT / "_site"
JST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


log = get_logger("common")


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["companies"] = [c for c in cfg.get("companies", []) if c.get("enabled", True)]
    return cfg


def now_jst() -> datetime:
    return datetime.now(JST)


def site_url(cfg: dict) -> str:
    url = (cfg.get("podcast", {}).get("site_url") or "").strip()
    if not url:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            if name.lower() == f"{owner.lower()}.github.io":
                url = f"https://{owner.lower()}.github.io/"
            else:
                url = f"https://{owner.lower()}.github.io/{name}/"
        else:
            url = "http://localhost:8000/"
    return url if url.endswith("/") else url + "/"


def repo_slug() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-\.%$]*", text))


# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------
_client = None


def gemini_client():
    global _client
    if _client is None:
        from google import genai

        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY が設定されていません（GitHub の Settings → Secrets に登録してください）"
            )
        _client = genai.Client(api_key=key)
    return _client


def _retry_delay_seconds(err: Exception, attempt: int) -> float:
    m = re.search(r"retry(?:Delay)?[^0-9]{0,20}(\d+(?:\.\d+)?)s", str(err), re.I)
    if m:
        return min(float(m.group(1)) + 2, 90)
    return min(10 * (2**attempt), 90)


def is_retryable(err: Exception) -> bool:
    s = str(err)
    return any(code in s for code in ("429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE"))


def call_with_fallback(models: list[str], fn, *, label: str, attempts_per_model: int = 3):
    """Call fn(model) trying each model in order, with retries on transient errors."""
    last_err: Exception | None = None
    for model in models:
        for attempt in range(attempts_per_model):
            try:
                t0 = time.time()
                result = fn(model)
                log.info("%s: %s ok (%.1fs)", label, model, time.time() - t0)
                return result, model
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e).replace("\n", " ")[:300]
                if is_retryable(e) and attempt < attempts_per_model - 1:
                    wait = _retry_delay_seconds(e, attempt)
                    log.warning("%s: %s transient error, retry in %.0fs: %s", label, model, wait, msg)
                    time.sleep(wait)
                    continue
                log.warning("%s: %s failed: %s", label, model, msg)
                break
    raise RuntimeError(f"{label}: all models failed. Last error: {last_err}")


def extract_json(text: str):
    """Parse JSON even if the model wrapped it in ```json fences."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
