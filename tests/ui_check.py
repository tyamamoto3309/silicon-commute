"""Headless UI smoke test: serve _site/ and exercise the PWA like a phone user would."""
import http.server
import json
import shutil
import socketserver
import sys
import threading
from functools import partial
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "_site"
SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "out" / "shots")
SHOTS.mkdir(parents=True, exist_ok=True)


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static server with HTTP Range support (like GitHub Pages) so audio seeking works."""

    def log_message(self, *a):
        pass

    def send_head(self):
        rng = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if not rng or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        start_s, _, end_s = rng.replace("bytes=", "").partition("-")
        start = int(start_s) if start_s else 0
        end = min(int(end_s) if end_s else size - 1, size - 1)
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        rem = getattr(self, "_remaining", None)
        if rem is None:
            return super().copyfile(source, outputfile)
        while rem > 0:
            chunk = source.read(min(65536, rem))
            if not chunk:
                break
            try:
                outputfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            rem -= len(chunk)
        self._remaining = None


def serve():
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 8765), partial(Handler, directory=str(SITE)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    httpd = serve()
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for scheme in ("light", "dark"):
            ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme=scheme,
                                is_mobile=True, has_touch=True, locale="ja-JP")
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto("http://127.0.0.1:8765/")
            page.wait_for_selector(".hero")
            page.screenshot(path=str(SHOTS / f"1_home_{scheme}.png"))
            if scheme == "dark":
                ctx.close()
                continue
            # open episode
            page.click("text=スクリプトを読む")
            page.wait_for_selector(".line")
            n_lines = page.locator(".line").count()
            assert n_lines > 30, n_lines
            assert page.is_visible("#player"), "player should be visible"
            # tap a line in the Micron story → seek & play
            target = page.locator(".segment").nth(2).locator(".line").nth(1)
            t = float(target.get_attribute("data-t"))
            target.click()
            page.wait_for_timeout(1500)
            cur = page.evaluate("document.getElementById('audio').currentTime")
            assert abs(cur - t) < 3, (cur, t)
            active = page.evaluate("document.querySelector('.line.active')?.dataset.i")
            assert active is not None, "no active line"
            page.evaluate("document.getElementById('audio').pause()")
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / "2_script.png"))
            # speed
            page.click("#p-speed")
            assert page.text_content("#p-speed").strip() == "1.1×", page.text_content("#p-speed")
            # quiz mode
            page.click("#btn-settings")
            page.click("text=和訳を隠す")
            page.screenshot(path=str(SHOTS / "3_settings.png"))
            page.click("dialog >> text=閉じる")
            assert page.evaluate("document.body.dataset.mode") == "quiz"
            first_ja_visible = page.locator(".line .ja").first.is_visible()
            assert not first_ja_visible, "ja should be hidden in quiz mode"
            page.locator(".line .reveal").first.click()
            assert page.locator(".line .ja").first.is_visible()
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=str(SHOTS / "4_quiz.png"))
            # stories tab
            page.click(".tabs >> text=ニュース")
            page.wait_for_selector("[data-panel=stories] .card")
            page.screenshot(path=str(SHOTS / "5_stories.png"))
            page.click("text=▶ この話を聴く >> nth=1")
            page.wait_for_timeout(800)
            assert page.evaluate("document.querySelector('[data-panel=script]').hidden") is False
            page.evaluate("document.getElementById('audio').pause()")
            # vocab tab
            page.click(".tabs >> text=単語")
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=str(SHOTS / "6_vocab.png"))
            # no horizontal overflow
            ow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            assert ow <= 0, f"horizontal overflow {ow}px"
            ctx.close()
        b.close()
    httpd.shutdown()
    bad = [e for e in errors if "favicon" not in e]
    print("console issues:", json.dumps(bad, ensure_ascii=False, indent=1) if bad else "none")
    print("UI checks passed; screenshots in", SHOTS)


if __name__ == "__main__":
    main()
